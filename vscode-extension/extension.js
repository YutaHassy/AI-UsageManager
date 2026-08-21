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
    formatList, ACCOUNT_OPERATIONS, init: initI18n, invalidate: invalidateI18n,
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
/**
 * この拡張の置き場所。**画面を開くのに要ります。**
 *
 * activate() が受け取る context を毎回引き回さずに済ませるためのものです。
 * 追加・編集は画面の中のフォームで受けるようになったので、コマンドパレット
 * から呼ばれたときも画面を開く必要があり、その呼び出しが増えました。
 *
 * @type {vscode.Uri}
 */
let extensionUri;
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
    extensionUri = context.extensionUri;

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
        vscode.commands.registerCommand('aiUsageManager.setProxyPassword', () => setProxyPassword()),
        vscode.commands.registerCommand('aiUsageManager.testProxy', () => testProxy()),
        vscode.commands.registerCommand('aiUsageManager.importProxyFromEnvironment',
            () => importProxyFromEnvironment()),
        vscode.commands.registerCommand('aiUsageManager.openConfigFile',
            () => openConfigFile()),
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
            if (e.affectsConfiguration('aiUsageManager.proxy')) {
                // パスワードはここに含まれない (aiUsageManager.proxy.* に
                // パスワードの設定キーは無い。setProxyPassword コマンド経由の
                // RPC でだけ渡す)。バックエンドの再起動は要らない — 通信の
                // たびに proxy_manager.py が現在値を見るだけなので、
                // set_proxy を送るだけで次の通信から効く。
                void pushProxySettings();
            }
        }),
    );

    // 前の版で無くなった設定が残っていれば片付けます。**待ちません** —
    // 設定ファイルの書き換えに、画面が出るのを待たせる理由がありません。
    void pruneRemovedSettings();

    // 起動直後は静かにしておく。ステータスバーに出す情報が要るので
    // 一覧だけは読みますが、取得 (通信) は利用者が開くまで行いません。
    void reload().then(() => {
        scheduleAutoRefresh();
        // 拡張の設定が真であり、バックエンドの config.json はそれを写した
        // ものになる。起動のたびに1回、現在値で必ず上書きする。
        void pushProxySettings();
    });
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

    // 4. 取得済みの結果。**これを忘れないこと。** 枠のラベルも状態の要約も
    //    エラー文もバックエンドが訳して返したもので、こちらに残っている
    //    のは前の言語の文字列です。捨てるだけでは、自動更新を切っている
    //    画面が「未取得」のまま止まるので、取り直すところまでやります。
    store.forgetUsage();
    void store.refreshAll();

    // ステータスバーの文字列もここで作られています。
    updateStatusBar();

    vscode.window.setStatusBarMessage(t('The display language has been changed.'), 4000);
}

/**
 * 追加・編集のフォームを、使用状況の画面の中に開きます。
 *
 * **別のウィンドウは出しません。** ここは以前 PySide6 製のアプリ
 * ウィンドウを起動していました。あちらが最初に出すのはただのフォームで、
 * ブラウザはその中の「ログイン」を押して初めて出てきます。**押さない人に
 * まで QtWebEngine の導入を強いていた**わけです。
 *
 * 項目を1つずつ選ばせる形 (QuickPick) も通りました。窓が要らない点では
 * 正しかったのですが、**いま何が設定されているのかを一望できません**。
 * 登録内容は互いに関係するもの (取得先が決まって初めて資格情報の意味が
 * 決まる) なので、並べて見せるほうが分かります。
 *
 * @param {'add'|'edit'} mode
 * @param {string} [accountId]
 * @returns {Promise<void>}
 */
async function openAccountForm(mode, accountId = '') {
    try {
        if (!store.snapshot) {
            await store.reload();
        }
    } catch (e) {
        await showBackendProblem(/** @type {Error} */ (e).message);
        return;
    }

    // **画面が閉じていても開きます。** コマンドパレットから呼ばれたときに
    // 何も起きないと、コマンドが壊れているようにしか見えません。
    const panel = UsagePanel.show(extensionUri, store, trace);
    await panel.openForm(mode, accountId);
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

// ===================== アカウントの操作 =====================

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
 * アカウントを追加します。**画面の中のフォームで受けます。**
 *
 * @returns {Promise<void>}
 */
async function addAccount() {
    await openAccountForm('add');
}

/**
 * アカウントの登録内容を書き換えます。
 *
 * @param {string} [accountId] 画面のボタンからは決まっています。
 *   コマンドパレットから呼ばれたときだけ選ばせます。
 * @returns {Promise<void>}
 */
async function editAccount(accountId) {
    const account = await resolveTarget(
        accountId, t('Choose the account to edit'));
    if (!account) {
        return;
    }
    await openAccountForm('edit', account.id);
}

/**
 * ログインし直します。**行き先は編集のフォームです。**
 *
 * 以前はアプリ内ブラウザを開いてログインさせ、Cookie を自動で回収して
 * いました。その道は畳んだので、期限切れの人に要るのは新しい資格情報を
 * 貼る場所です。取り方の手順もそこに出ます。
 *
 * **入口は残します。** 期限が切れたアカウントの画面に出るのはこのボタン
 * で、押した先が編集画面であることに不都合はありません。無くすと、切れた
 * ことに気づいた人が次にどうすればよいのかを示すものが消えます。
 *
 * @param {string} [accountId]
 * @returns {Promise<void>}
 */
async function relogin(accountId) {
    const account = await resolveTarget(
        accountId, t('Choose the account to sign in again'),
        (a) => a.needsCredential);
    if (!account) {
        return;
    }
    await openAccountForm('edit', account.id);
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

    // 確認はここで済ませる。バックエンドは尋ね返さずに実行する。
    const remove = t('Delete');
    const choice = await vscode.window.showWarningMessage(
        t("Delete the account '{name}'?\n"
            + 'The saved sign-in state (including cookies) is deleted with it.',
            { name: account.name }),
        { modal: true }, remove);
    if (choice !== remove) {
        return;
    }

    // **別ウィンドウは出ません** (backend/cli.py の do_delete_account を
    // 参照)。ここを PySide6 製のログイン画面へ通していたために、あれが
    // 入っていない環境では削除すらできませんでした。
    try {
        // 進み具合は**通知ではなくウィンドウ左下**に出します。ふつうは一瞬で
        // 終わりますが、保存されたログイン状態 (ブラウザのキャッシュを含む)
        // を消すのに数秒かかることがあり、その間まったく無反応に見えるのは
        // 「押しても何も起きない」と同じです。通知にしないのは、一瞬で
        // 消える通知が毎回点滅するだけになるためです。
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Window,
                title: t(ACCOUNT_OPERATIONS.remove),
            },
            () => store.deleteAccount(account.id),
        );
    } catch (e) {
        const message = /** @type {Error} */ (e).message;
        log.appendLine(`[extension] ${ACCOUNT_OPERATIONS.remove} に失敗: ${message}`);
        const showLog = t('Show Log');
        const picked = await vscode.window.showErrorMessage(
            t('{operation} could not be completed.\n{reason}', {
                operation: t(ACCOUNT_OPERATIONS.remove), reason: message,
            }),
            showLog);
        if (picked === showLog) {
            log.show(true);
        }
        return;
    }

    vscode.window.setStatusBarMessage(
        t("Deleted '{name}'", { name: account.name }), 5000);
}

// ================= プロキシの設定 =================
//
// バックエンドの services/config_manager.py には proxy_mode / proxy_host /
// proxy_port / proxy_username / proxy_password が今も生きており、
// services/proxy_manager.py が実際にそれを使って通信する。ところが VSCode
// 拡張側には設定する手段が無く、ui/account_dialog.py の案内が「設定で
// ユーザー名とパスワードを入力してください」と言うだけで案内先が存在しない
// 状態だった。ここでその案内先を作る。
//
// **パスワードだけは設定キーにしない。** settings.json は平文で保存され、
// Settings Sync で他端末にも複製される。バックエンドは secret_store.py の
// DPAPI で暗号化して保存する設計なので (config_manager.py の
// SECRET_SETTINGS)、その設計を拡張側から壊さないよう、パスワードは
// setProxyPassword コマンド経由の RPC でだけ渡す (get_proxy もパスワード
// そのものは返さない — hasPassword の真偽だけ)。

/**
 * この拡張が持たなくなった設定の一覧。**消した版と一緒に、ここへ足します。**
 *
 * 設定を manifest から消しても、利用者の settings.json に書かれた値は
 * 残ります。残ったものは VSCode が「不明な設定です」と警告する対象になり、
 * **消し方を知らなければ、その警告は永久に消えません。** 消した側が
 * 片付けるのが筋です。
 *
 * @type {{key: string, since: string}[]}
 */
const REMOVED_SETTINGS = [
    // PySide6 入りの Python を指すための設定。アプリ内ブラウザのログイン画面
    // ごと畳んだので、指す先がありません。
    { key: 'guiPythonPath', since: '1.7.0' },
];

/**
 * 無くなった設定を、利用者の settings.json から取り除きます。
 *
 * **触るのは、この拡張が持っていた設定だけです** (aiUsageManager.*)。しかも
 * REMOVED_SETTINGS に名指しで並べたものだけで、書かれている値は見ません。
 * 他人の設定ファイルを書き換える以上、対象は広げないこと。
 *
 * **黙って消しません。** 消したことを1度だけ知らせます。設定ファイルは
 * 利用者のものなので、勝手に変えたなら、何を変えたかは言うべきです。
 * (知らせが出るのは消せたときだけ＝ふつうは一生に一度です。)
 *
 * **書けなくても止まりません。** 設定ファイルが読み取り専用だったり、
 * 手で書いた JSON が壊れていて VSCode が書き戻せないことがあります。
 * そのときはログに残して素通りします — 片付けそこねただけで拡張が
 * 立ち上がらなくなるのは、直そうとしている不便より重い。
 *
 * @returns {Promise<void>}
 */
async function pruneRemovedSettings() {
    const config = vscode.workspace.getConfiguration('aiUsageManager');
    /** @type {string[]} */
    const removed = [];

    for (const { key, since } of REMOVED_SETTINGS) {
        // **inspect は manifest に無いキーでも答えます。** VSCode は
        // settings.json に書かれた値を、schema に載っているかどうかとは
        // 別に持っているためです。ここが効かなければ何も起きないだけで、
        // 壊れはしません。
        const found = config.inspect(key);
        if (!found) {
            continue;
        }
        /** @type {[unknown, vscode.ConfigurationTarget][]} */
        const scopes = [
            [found.globalValue, vscode.ConfigurationTarget.Global],
            [found.workspaceValue, vscode.ConfigurationTarget.Workspace],
            [found.workspaceFolderValue, vscode.ConfigurationTarget.WorkspaceFolder],
        ];
        let gone = false;
        for (const [value, target] of scopes) {
            if (value === undefined) {
                continue;
            }
            try {
                await config.update(key, undefined, target);
                gone = true;
                log.appendLine(
                    `[extension] 無くなった設定を消しました: aiUsageManager.${key}`
                    + ` (${since} で削除, scope=${target})`);
            } catch (e) {
                log.appendLine(
                    `[extension] 設定 aiUsageManager.${key} を消せませんでした`
                    + ` (scope=${target}): ${/** @type {Error} */ (e).message}`);
            }
        }
        if (gone) {
            removed.push(`aiUsageManager.${key}`);
        }
    }

    if (removed.length) {
        void vscode.window.showInformationMessage(
            t('These settings no longer exist and have been removed from '
                + 'your settings file: {names}',
                { names: formatList(removed) }));
    }
}

/**
 * VSCode 設定のプロキシ項目 (パスワードを除く) をバックエンドへ反映します。
 *
 * **拡張の設定が真で、バックエンドの config.json はそれを写したものです。**
 * 呼ぶのは (1) 拡張の起動時に1回、(2) aiUsageManager.proxy 配下の設定が
 * 変わったとき、の2箇所だけです (activate() 参照)。
 *
 * **失敗しても利用者には出しません。** 起動のたびに必ず1回叩くので、
 * Python を入れていない環境や pythonPath が未設定の環境では毎回失敗します。
 * それを毎回ダイアログで見せると、この拡張を使うたびの邪魔になります。
 *
 * @returns {Promise<void>}
 */
async function pushProxySettings() {
    const config = vscode.workspace.getConfiguration('aiUsageManager.proxy');
    try {
        await backend.setProxy({
            mode: config.get('mode', 'system'),
            host: config.get('host', ''),
            port: config.get('port', 8080),
            username: config.get('username', ''),
        });
        trace('プロキシ設定をバックエンドへ反映しました');
    } catch (e) {
        const message = /** @type {Error} */ (e).message;
        log.appendLine(`[extension] プロキシ設定を反映できませんでした: ${message}`);
    }
}

/**
 * 設定ファイル (config.json) をエディタで開きます。
 *
 * **VSCode の設定に出ていない項目が、ここにはあります。** 例えば
 * aoai_allowed_hosts (API キーの送信を許すドメイン) は config.json に
 * しか無く、取得先のエラーはそこへ足すよう案内します。**開く手立てが
 * 無いまま案内していたので、言われたとおりにしようがありませんでした。**
 *
 * 拡張の設定へ持ち上げないのは、**こちらが唯一の主だから**です。持ち上げると
 * プロキシ設定と同じように拡張側から書き写すことになり、空の既定値で
 * 上書きした瞬間に、手で書いた送信先の制限が黙って消えます。消えたことは
 * 画面のどこにも出ません。
 *
 * @returns {Promise<void>}
 */
async function openConfigFile() {
    try {
        if (!store.snapshot) {
            await store.reload();
        }
    } catch (e) {
        await showBackendProblem(/** @type {Error} */ (e).message);
        return;
    }

    const path = store.snapshot?.configPath;
    if (!path) {
        void vscode.window.showWarningMessage(
            t('The settings file has not been read yet.'));
        return;
    }

    const document = await vscode.workspace.openTextDocument(
        vscode.Uri.file(path));
    await vscode.window.showTextDocument(document);
    // **開いて終わりにしないこと。** 直しても、読み直すまでは効きません
    // (バックエンドは起動時と reload のときにしか読みません)。
    const restart = t('Restart Backend');
    const choice = await vscode.window.showInformationMessage(
        t('Changes to this file take effect after the backend is restarted.'),
        restart);
    if (choice === restart) {
        await vscode.commands.executeCommand('aiUsageManager.restartBackend');
    }
}

/**
 * プロキシのパスワードを聞いて、バックエンドへ送ります。
 *
 * **空文字で確定するとパスワードを消します。** キャンセル (Esc) と空文字の
 * 確定を区別する必要があるため、showInputBox の戻り値が undefined
 * (キャンセル) かどうかで先に分岐します。
 *
 * @returns {Promise<void>}
 */
async function setProxyPassword() {
    // すでに保存されているかを先に読みます。**入っているのかどうかが
    // 分からないと、空欄で確定して消してしまったことにも気づけません。**
    // 返るのは有無だけで、パスワードそのものは返りません
    // (cli.py の do_get_proxy を参照)。
    let hasPassword = false;
    try {
        ({ hasPassword } = await backend.getProxy());
    } catch (e) {
        // 読めなくても入力は続けられます。案内が1行減るだけなので、
        // ここで止めません。
        log.appendLine(
            `[extension] プロキシ設定を読めませんでした: ${/** @type {Error} */ (e).message}`);
    }

    const password = await vscode.window.showInputBox({
        title: t('Set the proxy password'),
        password: true,
        ignoreFocusOut: true,
        prompt: hasPassword
            ? t('A password is already saved. Enter a new one to replace it, or leave it empty and press Enter to clear it.')
            : t('Leave it empty and press Enter to clear the saved password.'),
        placeHolder: t('Proxy password'),
    });
    if (password === undefined) {
        // Esc で閉じた。空文字を確定した場合と違い、何もしない。
        return;
    }

    try {
        await backend.setProxyPassword(password);
        vscode.window.setStatusBarMessage(
            password
                ? t('AI-UsageManager: the proxy password has been saved')
                : t('AI-UsageManager: the proxy password has been cleared'),
            4000);
    } catch (e) {
        const message = /** @type {Error} */ (e).message;
        log.appendLine(`[extension] プロキシパスワードを保存できませんでした: ${message}`);
        const showLog = t('Show Log');
        const choice = await vscode.window.showErrorMessage(
            t('Could not save the proxy password.\n{reason}', { reason: message }),
            showLog);
        if (choice === showLog) {
            log.show(true);
        }
    }
}

/**
 * いまの設定でプロキシへの接続を試します (旧版 settings_dialog.py の
 * ConnectionTestWorker に相当)。
 *
 * **result.message は t() を通しません。** バックエンドがすでに表示用に
 * 翻訳して返します (cli.py の test_proxy が services/i18n.py 経由で
 * 組み立てる)。ここで訳し直すと二重変換になり、英語の原文キーとして
 * 一致しないぶん、かえって英語のまま出てしまいます。
 *
 * @returns {Promise<void>}
 */
async function testProxy() {
    await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: t('Testing the proxy connection'),
        },
        async () => {
            let result;
            try {
                result = await backend.testProxy();
            } catch (e) {
                const message = /** @type {Error} */ (e).message;
                log.appendLine(`[extension] 接続テストに失敗しました: ${message}`);
                void vscode.window.showErrorMessage(
                    t('Could not test the proxy connection.\n{reason}', { reason: message }));
                return;
            }

            if (result.reachable) {
                void vscode.window.showInformationMessage(result.message);
                return;
            }
            const showLog = t('Show Log');
            const choice = await vscode.window.showWarningMessage(result.message, showLog);
            if (choice === showLog) {
                log.show(true);
            }
        },
    );
}

/**
 * 環境変数 (HTTP_PROXY / HTTPS_PROXY) からプロキシを検出し、設定へ書きます
 * (旧版 settings_dialog.py の「環境変数から取り込む」ボタンに相当)。
 *
 * **書くのはホストとポートだけです。** detect_proxy はユーザーID・パスワード
 * を返しません (RPC の戻り値に含まれない)。環境変数に入っている認証情報は
 * たいてい社内 AD のものなので、それを settings.json (平文・同期対象) へ
 * 複製しないための設計です。ユーザーIDは aiUsageManager.proxy.username に
 * 手で、パスワードは setProxyPassword コマンドで、それぞれ利用者自身に
 * 入れてもらいます。
 *
 * **mode は変えません。** ここで manual へ勝手に切り替えると、system モード
 * を意図的に選んでいた利用者の挙動まで変えてしまいます。値を埋めるだけに
 * 留め、使うかどうかは利用者が aiUsageManager.proxy.mode を自分で manual に
 * します。
 *
 * @returns {Promise<void>}
 */
async function importProxyFromEnvironment() {
    let detected;
    try {
        detected = await backend.detectProxy();
    } catch (e) {
        const message = /** @type {Error} */ (e).message;
        log.appendLine(`[extension] 環境変数からのプロキシ検出に失敗しました: ${message}`);
        void vscode.window.showErrorMessage(
            t('Could not detect a proxy from environment variables.\n{reason}',
                { reason: message }));
        return;
    }

    if (!detected.found) {
        void vscode.window.showInformationMessage(
            t('No proxy was detected from environment variables.'));
        return;
    }

    const config = vscode.workspace.getConfiguration('aiUsageManager');
    await config.update('proxy.host', detected.host, vscode.ConfigurationTarget.Global);
    await config.update('proxy.port', detected.port, vscode.ConfigurationTarget.Global);
    // バックエンドへの反映そのものは onDidChangeConfiguration が拾う
    // (pushProxySettings を呼ぶ)。ここでは重ねて呼びません。

    void vscode.window.showInformationMessage(
        t('Imported the proxy {host}:{port} from environment variables. '
            + 'Set "aiUsageManager.proxy.mode" to "manual" to use it.',
            { host: detected.host, port: detected.port }));
}

function scheduleAutoRefresh() {
    if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = undefined;
    }
    const minutes = vscode.workspace
        .getConfiguration('aiUsageManager')
        // 既定値は package.json が持っています。ここの第2引数は
        // マニフェストに登録があれば使われませんが、**2つ目の数字を
        // 書けば必ず食い違います。** 揃えておきます。
        .get('autoRefreshMinutes', 1);
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
