/**
 * AI-UsageManager — VSCode 拡張機能の入口。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' と Node 組み込み、
 * それと同じフォルダの .js だけで、node_modules は使わない。
 * **このファイルがそのまま vsix に入る原本です。**
 *
 * 判定 (しきい値・色・文言) は Python 側 (services/usage_status.py) にあります。
 * ここに残しているのは、VSCode 拡張ホストでしかできないこと
 * — コマンドの登録、通知、ステータスバー、Webview — だけです。
 */

'use strict';

const vscode = require('vscode');

const { Backend } = require('./backend');
const {
    formatList, GUI_OPERATIONS, init: initI18n, invalidate: invalidateI18n,
    configuredLanguage, LANGUAGES, t,
} = require('./i18n');
const { LauncherViewProvider } = require('./launcher');
const { UsagePanel } = require('./panel');
const { UsageStore } = require('./store');

/** @type {Backend} */
let backend;
/** @type {UsageStore} */
let store;
/** @type {vscode.OutputChannel} */
let log;
/** @type {vscode.StatusBarItem} */
let statusBarItem;
/** @type {NodeJS.Timeout|undefined} */
let autoRefreshTimer;

/**
 * この拡張が読み込まれた時刻。
 *
 * **出力チャンネルは行に時刻を付けません。** 「ウィンドウを再読み込みしてから
 * 画面が出るまで何ミリ秒かかったか」を後から追う手段が、これまで一つも
 * ありませんでした。起動が遅いという報告は今後も出るので、そのたびに
 * 計測用のログを足して消す、ということをしないで済むように常設します。
 */
const ACTIVATED_AT = Date.now();

/**
 * 起動からの経過時間を付けて、ログへ1行書きます。
 *
 * @param {string} message
 */
function trace(message) {
    // log を作るのは activate() の中です。呼ぶのは activate 以降だけのはず
    // ですが、**診断のための1行が拡張を落とす**のは本末転倒なので確かめます。
    log?.appendLine(`[+${Date.now() - ACTIVATED_AT}ms] ${message}`);
}

/** @param {vscode.ExtensionContext} context */
function activate(context) {
    // **何よりも先に呼びます。** i18n.js は l10n/*.json を自分で読むので、
    // 拡張がどこにあるかを知らないうちは訳が当たらず英語のまま出ます。
    // ここから下は t() を使うので、順番を入れ替えないでください。
    initI18n(context.extensionUri);

    log = vscode.window.createOutputChannel('AI-UsageManager');
    // trace() は log へ書くので、チャンネルを作った直後が最初の1行になります。
    trace('activate() が始まりました');
    backend = new Backend(context.extensionPath, log);
    store = new UsageStore(backend);

    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'aiUsageManager.open';

    context.subscriptions.push(log, backend, store, statusBarItem);

    context.subscriptions.push(
        vscode.commands.registerCommand('aiUsageManager.open', () => openPanel(context)),
        vscode.commands.registerCommand('aiUsageManager.refreshAll', () => refreshAll()),
        vscode.commands.registerCommand('aiUsageManager.addAccount', () => addAccount()),
        vscode.commands.registerCommand('aiUsageManager.editAccount',
            (accountId) => editAccount(accountId)),
        vscode.commands.registerCommand('aiUsageManager.relogin',
            (accountId) => relogin(accountId)),
        vscode.commands.registerCommand('aiUsageManager.deleteAccount',
            (accountId) => deleteAccount(accountId)),
        // 標準の設定 UI を、この拡張の項目だけに絞って開く。独自の設定画面を
        // 作らないのは、ワークスペース単位の上書き・設定の同期・検索が
        // 標準 UI には最初から付いてくるため。
        vscode.commands.registerCommand('aiUsageManager.openSettings', () =>
            vscode.commands.executeCommand('workbench.action.openSettings', 'aiUsageManager')),
        vscode.commands.registerCommand('aiUsageManager.selectLanguage', () => selectLanguage()),
        vscode.commands.registerCommand('aiUsageManager.showLog', () => log.show(true)),
        vscode.commands.registerCommand('aiUsageManager.restartBackend', async () => {
            backend.restart();
            // **必ず投げ直します (相乗りさせません)。** restart() は待機中の
            // 要求を全部 reject するので、進行中の読み込みに相乗りすると、
            // たった今殺された要求の拒否を受け取ることになります。
            // 「再起動しました」と「読み込めませんでした」が同時に出ます。
            await reload(true);
            vscode.window.setStatusBarMessage(
                t('AI-UsageManager: the backend has been restarted'), 4000);
        }),
    );

    // ウィンドウを再読み込みしたときに、開いていたタブを元の位置へ戻す。
    context.subscriptions.push(UsagePanel.register(context, store, trace));

    // アクティビティバーのアイコン。押すとエディタタブが開く (launcher.js 参照)。
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            LauncherViewProvider.viewType,
            new LauncherViewProvider(),
            // 畳んだら捨てる。残しても「タブで開いています...」を持ち続けるだけ。
            { webviewOptions: { retainContextWhenHidden: false } },
        ),
    );

    context.subscriptions.push(store.onDidChange(() => updateStatusBar()));

    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('aiUsageManager.pythonPath')) {
                // インタープリタが変わったのに前のプロセスを使い続けると、
                // 設定を直したのに直らない、という状態になる。
                // restartBackend と同じ理由でここも投げ直す (相乗りさせない)。
                backend.restart();
                void reload(true);
            }
            if (e.affectsConfiguration('aiUsageManager.language')) {
                applyLanguageChange();
            }
            if (e.affectsConfiguration('aiUsageManager.autoRefreshMinutes')) {
                scheduleAutoRefresh();
            }
            if (e.affectsConfiguration('aiUsageManager.showStatusBar')) {
                updateStatusBar();
            }
        }),
    );

    // 起動直後は静かにしておく。ステータスバーに出す情報が要るので
    // 一覧だけは読みますが、取得 (通信) は利用者が開くまで行いません。
    void reload().then(() => scheduleAutoRefresh());
}

function deactivate() {
    if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = undefined;
    }
}

/**
 * アカウント一覧を読み直し、失敗しても黙って続けます。
 *
 * **force は store.reload() へそのまま渡すためだけのものです** (意味は
 * store.reload() の説明を参照)。バックエンドを再起動した直後の呼び出し元が
 * `store.reload(true)` を直接呼ばずにここを通るのは、ここにしかない
 * 「失敗をログだけに留める・ステータスバーを直す」後始末を捨てないためです。
 * 直接呼ぶと、Python を入れていない環境で再起動を押しただけで
 * 「コマンドの実行に失敗しました」が出るようになります。
 *
 * @param {boolean} [force] 進行中の読み込みに相乗りせず、必ず投げ直す。
 * @returns {Promise<void>}
 */
async function reload(force = false) {
    trace(force ? 'アカウント一覧を読み直します (投げ直し)' : 'アカウント一覧を読みます');
    try {
        await store.reload(force);
        trace(`アカウント一覧を読み終えました (${store.accounts.length} 件)`);
        updateStatusBar();
    } catch (e) {
        const message = /** @type {Error} */ (e).message;
        trace(`アカウント一覧を読めませんでした: ${message}`);
        log.appendLine(`[extension] アカウントを読み込めませんでした: ${message}`);
        statusBarItem.hide();
        // 起動時に毎回ダイアログを出すと、Python を入れていない利用者に
        // とっては開くたびの邪魔にしかならない。ここでは黙ってログに残し、
        // 実際に画面を開こうとしたときに理由を見せる。
    }
}

/**
 * @param {vscode.ExtensionContext} context
 * @returns {Promise<void>}
 */
async function openPanel(context) {
    try {
        if (!store.snapshot) {
            await store.reload();
        }
    } catch (e) {
        await showBackendProblem(/** @type {Error} */ (e).message);
        return;
    }

    const wasOpen = UsagePanel.isOpen;
    UsagePanel.show(context.extensionUri, store, trace);

    const refreshOnOpen = vscode.workspace
        .getConfiguration('aiUsageManager')
        .get('refreshOnOpen', true);
    if (!wasOpen && refreshOnOpen) {
        await refreshAll();
    }
}

/**
 * 表示言語を選ばせて、設定 aiUsageManager.language に書きます。
 *
 * 独自の設定画面は作らない方針ですが (openSettings のコメント参照)、言語だけは
 * 例外にしています。**読めない言語で表示されている状態から、設定画面を探して
 * たどり着くのは難しい**からです。画面のツールバーの 🌐 からも、ここへ来ます。
 *
 * 書き込み先を Global にしているのは、言語が「この人が読める言語」であって
 * ワークスペースの性質ではないためです。
 *
 * @returns {Promise<void>}
 */
async function selectLanguage() {
    const current = configuredLanguage();
    // **選択肢のラベルは訳しません。** 読めない言語で選択肢を出したら選べません
    // (i18n.js の LANGUAGES 参照)。自動だけは説明が要るので t() を通します。
    const items = [
        {
            label: t('Same as VS Code ({language})', { language: vscode.env.language }),
            tag: 'auto',
            description: current === 'auto' ? '$(check)' : undefined,
        },
        ...LANGUAGES.map((entry) => ({
            label: entry.label,
            tag: entry.tag,
            description: current === entry.tag ? '$(check)' : undefined,
        })),
    ];

    const choice = await vscode.window.showQuickPick(items, {
        placeHolder: t('Select the display language'),
        // **ここに制約を書いておきます。** コマンドパレットのコマンド名と
        // 設定画面の説明文は package.nls.*.json にあり、VSCode が VSCode 自身の
        // 表示言語で解決します。拡張からは差し替えられません。書いておかないと
        // 「設定したのに変わらない」と受け取られます。
        title: t('Command names and settings descriptions follow the VS Code display language.'),
    });
    if (!choice || choice.tag === current) {
        return;
    }

    await vscode.workspace
        .getConfiguration('aiUsageManager')
        .update('language', choice.tag, vscode.ConfigurationTarget.Global);
    // 反映そのものは onDidChangeConfiguration が拾います (設定画面から直接
    // 変えられたときも同じ道を通す必要があるため、ここではやりません)。
}

/**
 * 表示言語の設定が変わったときに、いま出ているものを全部作り直します。
 *
 * **拡張ホスト・webview・バックエンドの3つが、それぞれ別に言語を抱えています。**
 * どれか1つでも作り直し忘れると、画面の一部だけ前の言語のまま残ります。
 */
function applyLanguageChange() {
    // 1. 拡張ホスト。読んである訳文を捨てます。i18n.js のキャッシュは言語タグ
    //    ごとなので、実のところ捨てなくても新しい言語で引き直されます。それでも
    //    呼ぶのは、**「設定が変わったらここで捨てる」という道筋を1本残しておく**
    //    ためです。キャッシュの持ち方を変えた人が、ここを探さずに済みます。
    invalidateI18n();

    // 2. バックエンド (Python)。言語は**起動時の環境変数で1回だけ**決まります
    //    (services/i18n.py の冒頭参照)。再起動しない限り前の言語のままです。
    //    restartBackend と同じ理由で、読み込みは投げ直します (相乗りさせない)。
    backend.restart();
    void reload(true);

    // 3. webview。訳文は HTML に埋めてあるので、作り直さないと変わりません。
    UsagePanel.current?.refreshHtml();

    // ステータスバーの文字列もここで作られています。
    updateStatusBar();

    vscode.window.setStatusBarMessage(t('The display language has been changed.'), 4000);
}

/**
 * @param {string} message
 * @returns {Promise<void>}
 */
async function showBackendProblem(message) {
    const openSettings = t('Open Settings');
    const showLog = t('Show Log');
    const choice = await vscode.window.showErrorMessage(
        t('AI-UsageManager could not start.\n{reason}', { reason: message }),
        openSettings, showLog,
    );
    if (choice === openSettings) {
        await vscode.commands.executeCommand(
            'workbench.action.openSettings', 'aiUsageManager.pythonPath');
    } else if (choice === showLog) {
        log.show(true);
    }
}

/** @returns {Promise<void>} */
async function refreshAll() {
    trace('全件の取得を始めます');
    try {
        if (!store.snapshot) {
            // 進行中の読み込みがあれば相乗りします (store.reload の説明を参照)。
            await store.reload();
        }
    } catch (e) {
        trace('全件の取得を中止しました (一覧を読めませんでした)');
        await showBackendProblem(/** @type {Error} */ (e).message);
        return;
    }

    const targets = store.fetchableAccounts();
    if (targets.length === 0) {
        trace('全件の取得は行いませんでした (対象が 0 件)');
        vscode.window.setStatusBarMessage(
            t('AI-UsageManager: no account can be refreshed '
                + '(disabled, unconfigured and unimplemented ones are skipped)'), 6000);
        return;
    }

    await vscode.window.withProgress(
        {
            location: UsagePanel.isOpen
                ? vscode.ProgressLocation.Window
                : vscode.ProgressLocation.Notification,
            title: t('Refreshing AI usage ({count})', { count: targets.length }),
        },
        () => store.refreshAll(),
    );
    trace(`全件の取得が終わりました (${targets.length} 件)`);

    // 失敗したものはまとめて1回だけ知らせる。1件ずつダイアログを出すと、
    // 放置している間に通知が積み上がる。
    const failed = targets.filter((a) => store.entry(a.id).state === 'error');
    if (failed.length > 0) {
        const names = formatList(failed.map((a) => a.name));
        const showLog = t('Show Log');
        // **単数と複数で鍵を分けます。** 対応する4言語のうち数で形が変わるのは
        // 英語だけなので、Intl.PluralRules を持ち出すほどのことはありません
        // (ja / zh-cn / ko はどちらの鍵も同じ訳になります)。
        const message = failed.length === 1
            ? t('Could not refresh 1 account: {names}', { names })
            : t('Could not refresh {count} accounts: {names}',
                { count: failed.length, names });
        const choice = await vscode.window.showWarningMessage(message, showLog);
        if (choice === showLog) {
            log.show(true);
        }
    }
}

// ================= アカウントの操作 (ログイン画面を伴う) =================

/**
 * 対象アカウントを決めます。
 *
 * コマンドパレットから呼ばれると引数が来ないので、そのときだけ選ばせます。
 * 画面のボタンからは対象が決まっているため、余計な一手間を挟みません。
 *
 * @param {string|undefined} accountId
 * @param {string} placeHolder
 * @param {(account: import('./backend').AccountInfo) => boolean} [filter]
 * @returns {Promise<import('./backend').AccountInfo|undefined>}
 */
async function resolveTarget(accountId, placeHolder, filter) {
    try {
        if (!store.snapshot) {
            await store.reload();
        }
    } catch (e) {
        await showBackendProblem(/** @type {Error} */ (e).message);
        return undefined;
    }

    if (accountId) {
        const found = store.accounts.find((a) => a.id === accountId);
        if (!found) {
            void vscode.window.showWarningMessage(t('Account not found.'));
        }
        return found;
    }

    const candidates = filter ? store.accounts.filter(filter) : store.accounts;
    if (candidates.length === 0) {
        void vscode.window.showInformationMessage(t('No account is eligible.'));
        return undefined;
    }

    const picked = await vscode.window.showQuickPick(
        candidates.map((account) => ({
            label: account.name,
            description: `[${account.providerLabel}]`,
            account,
        })),
        { placeHolder },
    );
    return picked?.account;
}

/**
 * 「別ウィンドウが出ていないかもしれません」と案内し始めるまでの時間 (ミリ秒)。
 *
 * PySide6 の起動と QtWebEngine の初期化には、初回や仮想環境では数秒かかります。
 * 5 秒では正常な起動でも案内が出てしまい、毎回オオカミ少年になります。逆に
 * 1 分では、利用者が「壊れている」と結論を出したあとに出ることになります。
 * 20 秒は、正常なら窓が出ているのに十分で、まだ諦められていない頃合いです。
 */
const GUI_STUCK_HINT_MS = 20000;

/**
 * 中止を頼んでから、待つのをやめて通知を閉じるまでの猶予 (ミリ秒)。
 *
 * ふつうは、子プロセスを終了させた時点でバックエンドが「中止されました」と
 * 返してくるので、ここまで待つことはありません。**それでも要ります。**
 * 中止が要る状況は「何かが詰まっている」状況そのもので、バックエンド自身が
 * 応答しない可能性を見込んでおかないと、中止を押しても通知が消えない =
 * 結局ウィンドウの再読み込みしか手が無い、という元の症状に戻ります。
 */
const CANCEL_GRACE_MS = 5000;

/**
 * ログイン画面を伴う操作を走らせます。
 *
 * ダイアログは VSCode とは別のウィンドウに出ます。押した本人は VSCode を
 * 見ているので、**どこを見ればよいかを必ず知らせます。** 黙って待つと
 * 「押しても何も起きない」ようにしか見えません。
 *
 * **必ず中止できるようにしておくこと (cancellable: true)。** 子プロセスが
 * ウィンドウを一つも作らないまま固まる例が出ており、中止できないと利用者に
 * 残る手段は VSCode の再読み込みだけになります。
 *
 * @param {string} operation i18n.GUI_OPERATIONS の値 (英語の原文)。**訳す前の
 *   ものを渡してください。** 画面に出すぶんはここで t() を通し、ログには
 *   原文のまま残します。ログを読むのは開発者なので、利用者の表示言語によって
 *   検索できる語が変わるのは困ります。
 * @param {() => Promise<void>} action
 * @returns {Promise<boolean>} 最後まで実行できたか (中止・失敗なら false)
 */
async function runGuiCommand(operation, action) {
    const title = t(operation);
    // 利用者自身が中止したか。中止はエラーではないので、この場合は
    // 「完了できませんでした」というダイアログを出しません。
    let cancelled = false;
    // 中止を頼んだのに応答が返らず、待つのをやめたか。
    let abandoned = false;

    // 急かさないこと。**ログインに時間がかかるのは正常です。** ここで
    // 「失敗しました」と決めつけると、まだ操作している最中の利用者に
    // 誤った結論を押し付けることになります。
    const stuckHint = t('If you cannot find the separate window, check the taskbar. '
        + 'Use the × on this notification to cancel.');

    try {
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title,
                cancellable: true,
            },
            async (progress, token) => {
                /** @type {vscode.Disposable[]} */
                const subscriptions = [];
                /** @type {NodeJS.Timeout[]} */
                const timers = [];

                // 起動する前から「別ウィンドウで操作してください」と出すと、
                // まだ無い窓を探させることになる。バックエンドが子プロセスを
                // 起こした合図 (gui_started) を受けてから進めます。
                progress.report({ message: t('Preparing the window') });
                subscriptions.push(backend.onGuiStarted(() => {
                    progress.report({ message: t('Continue in the separate window') });
                }));

                timers.push(setTimeout(
                    () => progress.report({ message: stuckHint }), GUI_STUCK_HINT_MS));

                const givenUp = new Promise((resolve) => {
                    subscriptions.push(token.onCancellationRequested(() => {
                        cancelled = true;
                        progress.report({ message: t('Cancelling') });
                        // **通知を閉じるだけでは終わりません。** 実際に子プロセスを
                        // 終了させないと、固まったプロセスが残り、バックエンド側の
                        // 錠も握られたままになります。
                        void store.cancelGui().catch((e) => {
                            log.appendLine(
                                `[extension] 中止を伝えられませんでした: ${e.message}`);
                        });
                        timers.push(setTimeout(() => {
                            abandoned = true;
                            resolve(undefined);
                        }, CANCEL_GRACE_MS));
                    }));
                });

                try {
                    // 見捨てた側 (action) が後から失敗しても未処理の拒否には
                    // なりません。race がすでに両方に handler を付けています。
                    await Promise.race([action(), givenUp]);
                } finally {
                    for (const timer of timers) {
                        clearTimeout(timer);
                    }
                    for (const subscription of subscriptions) {
                        subscription.dispose();
                    }
                }
            },
        );
    } catch (e) {
        const message = /** @type {Error} */ (e).message;
        log.appendLine(`[extension] ${operation} に失敗: ${message}`);
        if (cancelled) {
            // 中止した結果の失敗は利用者に見せません。頼んだとおりに
            // 終わったものを「エラー」として出しても混乱するだけです。
            return false;
        }
        const showLog = t('Show Log');
        const openSettings = t('Open Settings');
        // PySide6 が無い場合は、設定で別の Python を指せることを併せて出す。
        // これが一番よくある詰まりどころ。
        const actions = message.includes('PySide6')
            ? [openSettings, showLog]
            : [showLog];
        const choice = await vscode.window.showErrorMessage(
            t('{operation} could not be completed.\n{reason}',
                { operation: title, reason: message }),
            ...actions);
        if (choice === showLog) {
            log.show(true);
        } else if (choice === openSettings) {
            await vscode.commands.executeCommand(
                'workbench.action.openSettings', 'aiUsageManager.guiPythonPath');
        }
        return false;
    }

    if (abandoned) {
        // 中止は伝えたが、バックエンドが応答しないまま通知を閉じた。何も
        // 言わずに閉じると「消えたから終わったのだろう」と受け取られるので、
        // 残っているかもしれないものと、次の一手を知らせます。
        const restart = t('Restart Backend');
        const showLog = t('Show Log');
        const choice = await vscode.window.showWarningMessage(
            t('{operation}: cancellation was requested, but there was no response.\n'
                + 'Close the separate window if it is still open. '
                + 'If you still cannot go on, restart the backend.',
                { operation: title }),
            restart, showLog);
        if (choice === restart) {
            await vscode.commands.executeCommand('aiUsageManager.restartBackend');
        } else if (choice === showLog) {
            log.show(true);
        }
        return false;
    }
    if (cancelled) {
        vscode.window.setStatusBarMessage(
            t('AI-UsageManager: {operation} was cancelled', { operation: title }), 5000);
        return false;
    }

    updateStatusBar();
    return true;
}

/**
 * アカウントを追加します。
 *
 * アプリ内ブラウザ (PySide6 + QtWebEngine) のログイン画面を別ウィンドウで
 * 開きます。**PySide6 が入っていない環境ではここで失敗します。** その場合は
 * runGuiCommand が pip のコマンドと `guiPythonPath` の設定を案内するので、
 * 押す前に確かめる (= 追加のたびに一手間を挟む) ことはしません。
 *
 * @returns {Promise<void>}
 */
async function addAccount() {
    try {
        if (!store.snapshot) {
            await store.reload();
        }
    } catch (e) {
        await showBackendProblem(/** @type {Error} */ (e).message);
        return;
    }

    const before = new Set(store.accounts.map((a) => a.id));
    if (!await runGuiCommand(GUI_OPERATIONS.add, () => store.addAccount())) {
        return;
    }

    // 増えていたら、そのまま1回取得して結果を見せます。登録直後に「未取得」と
    // 出ていると、うまくいったのか分からないためです。
    //
    // 戻ってきた時点で store は新しい一覧に入れ替わっています
    // (runGuiOperation がスナップショットを差し替える) ので、読み直しは不要。
    // ヘルパーは追加した ID を返さないため、差分で探します。
    const added = store.accounts.find((a) => !before.has(a.id));
    if (!added) {
        return;
    }

    vscode.window.setStatusBarMessage(
        t("Added '{name}'", { name: added.name }), 5000);
    if (store.fetchableAccounts().some((a) => a.id === added.id)) {
        await store.refreshOne(added.id);
    }
}

/**
 * @param {string} [accountId]
 * @returns {Promise<void>}
 */
async function editAccount(accountId) {
    const account = await resolveTarget(
        accountId, t('Choose the account to edit'));
    if (!account) {
        return;
    }
    // 中止・失敗したときは取得しません。触れていないアカウントに通信を
    // 1回足すだけで、画面に出る内容は変わりません。
    if (!await runGuiCommand(GUI_OPERATIONS.edit, () => store.editAccount(account.id))) {
        return;
    }
    if (store.fetchableAccounts().some((a) => a.id === account.id)) {
        await store.refreshOne(account.id);
    }
}

/**
 * @param {string} [accountId]
 * @returns {Promise<void>}
 */
async function relogin(accountId) {
    const account = await resolveTarget(
        accountId, t('Choose the account to sign in again'), (a) => a.canRelogin);
    if (!account) {
        return;
    }
    if (!account.canRelogin) {
        void vscode.window.showInformationMessage(
            t('{provider} does not support signing in through a browser. '
                + 'Set {credential} from "Edit Account".',
                { provider: account.providerLabel, credential: account.credentialLabel }));
        return;
    }
    if (!await runGuiCommand(GUI_OPERATIONS.relogin, () => store.relogin(account.id))) {
        return;
    }
    if (store.fetchableAccounts().some((a) => a.id === account.id)) {
        await store.refreshOne(account.id);
    }
}

/**
 * @param {string} [accountId]
 * @returns {Promise<void>}
 */
async function deleteAccount(accountId) {
    const account = await resolveTarget(
        accountId, t('Choose the account to delete'));
    if (!account) {
        return;
    }

    // 確認はここで済ませる。バックエンド側でもう一度聞くと、別ウィンドウが
    // 出るだけで二度手間になる。
    const remove = t('Delete');
    const choice = await vscode.window.showWarningMessage(
        t("Delete the account '{name}'?\n"
            + 'The saved sign-in state (including cookies) is deleted with it.',
            { name: account.name }),
        { modal: true }, remove);
    if (choice !== remove) {
        return;
    }

    // 中止された可能性があるので、結果を見てから知らせます。中止したのに
    // 「削除しました」と出ると、消えていないものを消えたと思わせます。
    if (!await runGuiCommand(GUI_OPERATIONS.remove, () => store.deleteAccount(account.id))) {
        return;
    }
    vscode.window.setStatusBarMessage(
        t("Deleted '{name}'", { name: account.name }), 5000);
}

function scheduleAutoRefresh() {
    if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = undefined;
    }
    const minutes = vscode.workspace
        .getConfiguration('aiUsageManager')
        .get('autoRefreshMinutes', 0);
    if (!minutes || minutes <= 0) {
        log.appendLine('[extension] 自動更新は無効です。');
        return;
    }
    log.appendLine(`[extension] 自動更新を ${minutes} 分間隔で有効にしました。`);
    autoRefreshTimer = setInterval(() => {
        // 前回が終わっていなければ見送る。重ねて走らせると、同じ取得先へ
        // 同時にリクエストが飛ぶ。
        if (!store.isRefreshing) {
            // **catch を付けること。** store.refreshAll() は一覧をまだ読めて
            // いなければ中で reload() を試みるので、Python を用意できていない
            // 環境では拒否が返ります。void のままだと未処理の拒否になり、
            // 利用者には何も出ないまま自動更新だけが黙って効かなくなります。
            store.refreshAll().catch((e) => {
                log.appendLine(`[extension] 自動更新に失敗しました: ${e.message}`);
            });
        }
    }, minutes * 60 * 1000);
}

function updateStatusBar() {
    const show = vscode.workspace
        .getConfiguration('aiUsageManager')
        .get('showStatusBar', true);
    if (!show || store.accounts.length === 0) {
        statusBarItem.hide();
        return;
    }

    const worst = store.worst();
    if (!worst) {
        statusBarItem.text = `$(pulse) ${t('AI Usage')}`;
        statusBarItem.tooltip = t('Nothing fetched yet. Click to open.');
        statusBarItem.show();
        return;
    }

    // 丸印はバックエンドが判定したものをそのまま出す (store.worst 参照)。
    statusBarItem.text =
        `${worst.dot} ${worst.account.name} ${worst.utilization.toFixed(1)}%`;
    statusBarItem.tooltip = t(
        'Closest to its limit: {name} [{provider}]\nClick to open the list.',
        { name: worst.account.name, provider: worst.account.providerLabel });
    statusBarItem.show();
}

module.exports = { activate, deactivate };
