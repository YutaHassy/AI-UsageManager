/**
 * 使用状況を表示するエディタタブ (Webview)。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' だけで、
 * node_modules は使わない。**このファイルがそのまま vsix に入る原本です。**
 *
 * 画面に出す値は、バックエンドが判定を済ませたものをそのまま webview へ
 * 転送します。ここでは色や文言を決めません (media/main.js も同じです)。
 */

'use strict';

const vscode = require('vscode');

const { bundleLiteral, language, t, webviewBundle } = require('./i18n');

/** @returns {string} */
function nonce() {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let text = '';
    for (let i = 0; i < 32; i++) {
        text += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return text;
}

/**
 * HTML の中へ文字列を置くための逃がし。
 *
 * 訳文は翻訳者が書くものなので、記号が混ざらない保証はありません。
 * この画面は取得先サーバーの応答を出すため textContent で徹底的に
 * 組んでいます (media/main.js 冒頭)。**訳文を入れるためにここへ穴を
 * 開けては、その徹底が意味を失います。**
 *
 * @param {string} value
 * @returns {string}
 */
function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/**
 * 追加・編集のフォームの状態。
 *
 * **これが載っている間、画面はフォームを最前面に出します。** null なら
 * 開いていません (media/main.js の render)。
 *
 * @typedef {object} FormState
 * @property {string} token 開くたびに変わる印。webview はこれが変わったとき
 *   **だけ**入力欄へ値を流し込みます (media/main.js の updateForm)。毎回
 *   入れ直すと、打っている最中に自動更新が走った瞬間に入力が消えます。
 * @property {'add'|'edit'} mode
 * @property {string} accountId 編集の対象 (追加なら空)
 * @property {import('./backend').ProviderInfo[]} providers 選べる取得先
 * @property {boolean} hasCredential 保存済みの資格情報があるか。**中身は
 *   決して送りません** (backend/cli.py の account_payload)。画面が要るのは
 *   「空のまま保存すれば前のものが残る」と案内できることだけです。
 * @property {{provider: string, name: string, extra: string, budget: number,
 *   enabled: boolean}} values 開いた時点の値。**以後こちらは更新しません。**
 * @property {boolean} busy 保存の応答を待っているか
 * @property {string|null} error 保存できなかった理由
 * @property {string[]|null} confirm 承知のうえで保存するか尋ねる内容
 */

/**
 * 使用状況を表示する Webview パネル。
 *
 * パネルは**同時に1つだけ**にします。複数開けると、どれが最新なのか
 * 分からないうえに、更新のたびに全部へ同じ内容を配ることになります。
 */
class UsagePanel {
    /** package.json の activationEvents と揃えること。 */
    static viewType = 'aiUsageManager.usage';

    /** @type {UsagePanel|undefined} */
    static current = undefined;

    /**
     * @param {vscode.WebviewPanel} panel
     * @param {vscode.Uri} extensionUri
     * @param {import('./store').UsageStore} store
     * @param {(message: string) => void} [trace] 起動からの経過時間を付けて
     *   ログへ1行書きます (extension.js が渡します)。
     */
    constructor(panel, extensionUri, store, trace = () => {}) {
        this.panel = panel;
        this.extensionUri = extensionUri;
        this.store = store;
        // **trace は省略できます (既定は何もしない関数)。** 渡し忘れると落ちる
        // 作りにはしません。これは計測のためだけの引数で、渡し忘れた呼び出しが
        // 1つ増えただけでタブが出なくなるのは、いま直している不具合と同じ形です。
        // 診断の道具が表示を巻き添えにしてはいけません。呼び出し元 (extension.js
        // の2箇所) は必ず渡すので、実際に空になることはありません。
        this.trace = trace;

        // 追加・編集のフォームを開いている間だけ入ります。**開いていなければ
        // null です。**
        //
        // **入力された値はここに持ちません。** 打っている最中の文字は webview
        // 側の DOM にあり、こちらへ届くのは保存を押した瞬間だけです。post() は
        // 定期取得のたびに状態をまるごと送り直すので、そこへ入力値を混ぜると
        // 打っている途中で上書きされて消えます。
        /** @type {FormState|null} */
        this._form = null;

        /** @type {vscode.Disposable[]} */
        this.disposables = [];

        // 復元されたパネルは options を持たないことがあるので、必ず入れ直す。
        // 入れ忘れるとスクリプトが動かず、白紙のタブになる。
        this.panel.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
        };
        this.panel.webview.html = this.html();

        this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
        this.panel.webview.onDidReceiveMessage(
            (message) => this.onMessage(message), null, this.disposables);
        this.disposables.push(this.store.onDidChange(() => this.post()));
    }

    /**
     * @param {vscode.Uri} extensionUri
     * @param {import('./store').UsageStore} store
     * @param {(message: string) => void} [trace] constructor の説明を参照。
     * @returns {UsagePanel}
     */
    static show(extensionUri, store, trace = () => {}) {
        if (UsagePanel.current) {
            // **列を指定せずに前へ出します。** reveal() に列を渡すと、その
            // タブが今いるエディタグループから指定した列へ**動きます**。
            // ここへ渡していたのは「今アクティブなテキストエディタの列、
            // 無ければ 1」でしたが、**webview はテキストエディタではない**
            // ので、この画面を触っている間は activeTextEditor が undefined
            // です。つまり画面の中の「追加」「編集」を押すたびに、必ず
            // 「列 1」= 左端へ引き戻されていました。右側へ寄せて使っている
            // 人には、押すたびにタブが飛ぶように見えます。
            //
            // 引数なしの reveal() は「列は変えずに前へ出す」です。
            UsagePanel.current.panel.reveal();
            UsagePanel.current.post();
            return UsagePanel.current;
        }

        // **新しく作るときだけ、置き場所を決めます。** 今見ているものの隣に
        // 出したいので、アクティブなエディタの列を使います。
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        const panel = vscode.window.createWebviewPanel(
            UsagePanel.viewType,
            t('AI Usage'),
            column,
            {
                enableScripts: true,
                // 隠したときに作り直すと、開き直すたびに取得結果の描画が
                // 一瞬空になる。状態は拡張側が持っているので保持で構わない。
                retainContextWhenHidden: true,
                localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
            },
        );
        panel.iconPath = vscode.Uri.joinPath(extensionUri, 'media', 'icon.png');

        UsagePanel.current = new UsagePanel(panel, extensionUri, store, trace);
        return UsagePanel.current;
    }

    static get isOpen() {
        return UsagePanel.current !== undefined;
    }

    /**
     * ウィンドウの再読み込みで復元されたタブを引き取ります。
     *
     * **これを登録しないと、再読み込みのたびにタブが消えます。** VSCode は
     * 開いていた webview のタブ自体は覚えていますが、中身を作り直せるのは
     * 拡張だけなので、名乗り出る先が無ければタブごと捨てられます。
     *
     * 位置 (エディタグループ) は VSCode が復元したものをそのまま使います。
     * こちらで開き直すと、別のグループへ動かしていた場合に元へ戻せません。
     *
     * **取得の完了を待ってから返してはいけません。** 下の deserializeWebviewPanel
     * を参照。
     *
     * @param {vscode.ExtensionContext} context
     * @param {import('./store').UsageStore} store
     * @param {(message: string) => void} [trace] constructor の説明を参照。
     * @returns {vscode.Disposable}
     */
    static register(context, store, trace = () => {}) {
        return vscode.window.registerWebviewPanelSerializer(UsagePanel.viewType, {
            // async のままなのは API が Thenable<void> を求めるためで、中では待ちません。
            async deserializeWebviewPanel(panel) {
                trace('復元されたタブを引き取りました (取得は待ちません)');
                if (UsagePanel.current) {
                    // 二重に持たない。復元は1つだけ引き取り、残りは閉じる。
                    panel.dispose();
                    return;
                }
                panel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'icon.png');
                UsagePanel.current = new UsagePanel(panel, context.extensionUri, store, trace);

                // 再読み込みでプロセスごと作り直されているため、取得結果は
                // 手元に残っていない。何もしないと全部「未取得」のまま出て、
                // 壊れたように見える。開いたときと同じ扱いで1回取り直す。
                const refreshOnOpen = vscode.workspace
                    .getConfiguration('aiUsageManager')
                    .get('refreshOnOpen', true);
                if (refreshOnOpen) {
                    // **await しないこと。** deserializeWebviewPanel が返した Promise が
                    // 解決するまで、VSCode は復元した webview をエディタへ貼りません
                    // (WebviewInput.resolve() がここを待ち、claimWebview まで進みません)。
                    // ここで取得の完了を待つと、Python 拡張の activate + バックエンドの起動 +
                    // 全件取得 (実測 5.8〜20 秒) のあいだタブが空のままになります。しかも
                    // media/main.js が用意している「読み込んでいます...」(main.js:379-382) は、
                    // 画面が貼られていないので一度も見えません。
                    // 投げっぱなしにして、結果は store.onDidChange -> post() で届けます。
                    // 二重に取得が走らないことは store.refreshAll の refreshing 判定と
                    // store.reload の相乗り (store.js) が保証します。
                    void vscode.commands.executeCommand('aiUsageManager.refreshAll');
                }
            },
        });
    }

    dispose() {
        UsagePanel.current = undefined;
        this.panel.dispose();
        for (const d of this.disposables) {
            d.dispose();
        }
        this.disposables.length = 0;
    }

    /**
     * 画面を作り直します。**表示言語が変わったときに呼びます。**
     *
     * 訳文は HTML に埋め込んであるので (html() の window.__l10n)、入れ直さない
     * 限り前の言語のまま残ります。postMessage では届けられません。
     *
     * **html を入れ直すと webview は作り直され、main.js は最初から動きます。**
     * つまり向こうの state は空に戻るので、送り直さなければ「読み込んでいます...」
     * のまま止まります。ただし main.js は 'ready' を送ってくるので、そこで
     * post() が走ります。ここで先に post() しても、まだ受け手がいないため
     * 届きません。**待つのが正しい**ということを、忘れて足さないように書いておきます。
     * (選択中のアカウントは main.js が vscode.getState() から復元します。)
     */
    refreshHtml() {
        this.panel.title = t('AI Usage');
        this.panel.webview.html = this.html();
    }

    /** 現在の状態をまるごと送ります (差分は取りません)。 */
    post() {
        const snapshot = this.store.snapshot;
        /** @type {Record<string, unknown>} */
        const entries = {};
        for (const account of this.store.accounts) {
            entries[account.id] = this.store.entry(account.id);
        }
        void this.panel.webview.postMessage({
            type: 'state',
            accounts: this.store.accounts,
            fetchable: snapshot?.fetchable ?? [],
            entries,
            refreshing: this.store.isRefreshing,
            // 一覧をまだ読めていない間は「0件」と言い切らない。復元直後は
            // 読み込みが終わる前に描画が走るので、ここを常に true にすると
            // 「アカウントがありません」が一瞬出る。
            loaded: snapshot !== undefined,
            configPath: snapshot?.configPath ?? '',
            loadError: snapshot?.loadError ?? null,
            encryptionAvailable: snapshot?.encryptionAvailable ?? true,
            // 追加・編集のフォーム。開いていなければ null です。
            form: this._form,
        });
    }

    /**
     * 追加・編集のフォームを開きます。
     *
     * **別のウィンドウは出しません。** 以前はここで PySide6 製のアプリ
     * ウィンドウを起動していましたが、それは QtWebEngine の入った Python を
     * 要求するもので、API キーを貼るだけの人にまでブラウザエンジンの導入を
     * 強いていました。資格情報を取ってくるのは**利用者の既定ブラウザ**の
     * 仕事にして (onMessage の openManual)、入力はこの画面の中で受けます。
     *
     * @param {'add'|'edit'} mode
     * @param {string} [accountId] mode が 'edit' のときの対象
     * @returns {Promise<void>}
     */
    async openForm(mode, accountId = '') {
        const account = mode === 'edit'
            ? this.store.accounts.find((a) => a.id === accountId)
            : undefined;
        if (mode === 'edit' && !account) {
            void vscode.window.showErrorMessage(t('The account was not found.'));
            return;
        }

        // **いま使っている取得先は、一覧から取り下げていても取り寄せます。**
        // 返らないと、その取得先のままのアカウントを開いた編集画面が、
        // ラベルも入力例も出せません (backend/cli.py の do_list_providers)。
        let providerList;
        try {
            providerList = await this.store.listProviders(
                account ? [account.provider] : []);
        } catch (e) {
            void vscode.window.showErrorMessage(
                t('Could not load the list of providers: {reason}',
                    { reason: /** @type {Error} */ (e).message }));
            return;
        }

        // 選択肢に出すのは、現役で実装済みのものだけ。**ただし編集中の
        // アカウントが使っているものは、取り下げ済みでも残します。** 外すと、
        // 名前を直したいだけの人が取得先まで変えさせられます。
        const usable = providerList.filter((p) =>
            (p.implemented && !p.retired) || (account && p.id === account.provider));
        if (!usable.length) {
            void vscode.window.showErrorMessage(
                t('No provider is available.'));
            return;
        }

        const first = usable[0];
        this._form = {
            token: `${mode}:${account ? account.id : ''}:${Date.now()}`,
            mode,
            accountId: account ? account.id : '',
            providers: usable,
            hasCredential: account ? Boolean(account.hasCredential) : false,
            values: {
                provider: account ? account.provider : first.id,
                name: account ? account.name : '',
                extra: account ? (account.extra || '') : '',
                budget: account ? account.budget : first.defaultBudget,
                enabled: account ? account.enabled : true,
            },
            busy: false,
            error: null,
            confirm: null,
        };
        this.post();
    }

    /** フォームを閉じます。**入力は捨てます。** */
    closeForm() {
        this._form = null;
        this.post();
    }

    /**
     * フォームの内容を保存します。
     *
     * **保存できたときだけ閉じます。** 弾かれたときに閉じると、打ち直す先が
     * 消えて、利用者は最初から入力し直すことになります。長い Cookie を貼り
     * 直させるのは、その一度で諦める理由になります。
     *
     * @param {any} message webview からの saveAccount
     * @returns {Promise<void>}
     */
    async saveForm(message) {
        const form = this._form;
        // 二重に押されたもの、閉じたあとに届いたものは捨てます。
        if (!form || form.busy) {
            return;
        }

        const values = message?.values ?? {};
        /** @type {Record<string, unknown>} */
        const changes = {
            provider: String(values.provider ?? ''),
            name: String(values.name ?? ''),
            enabled: Boolean(values.enabled),
            confirmed: Boolean(message?.confirmed),
        };
        // **載せなかったキーは「触らない」という意味です** (backend/cli.py の
        // update_account)。取得先が使わない欄まで送ると既定値へ戻す指示に
        // なり、資格情報なら空文字が「消してください」になります。webview は
        // その取得先が実際に使う欄だけ、資格情報は入力されたときだけ載せます。
        if (values.extra !== undefined) {
            changes.extra = String(values.extra);
        }
        if (values.budget !== undefined) {
            // **数値へ直しません。** 打たれた文字をそのまま渡します。ここで
            // Number() に通すと「1,000」や全角の数字が NaN になり、JSON へ
            // 載せる段で null に化けて、**利用者が打った内容が消えます。**
            // 「数値ではありません」と答える用意はバックエンドにあるので
            // (cli.py の update_account)、判定はあちらに任せます。
            changes.budget = values.budget;
        }
        if (values.credential !== undefined) {
            changes.credential = String(values.credential);
        }

        form.busy = true;
        form.error = null;
        form.confirm = null;
        this.post();

        /** @type {string|null} */
        let error = null;
        /** @type {string[]|null} */
        let confirm = null;
        let savedId = '';
        try {
            if (form.mode === 'add') {
                const result = await this.store.createAccount(changes);
                confirm = result.confirm ?? null;
                savedId = result.accountId ?? '';
            } else {
                changes.accountId = form.accountId;
                const asked = await this.store.updateAccount(
                    /** @type {any} */ (changes));
                confirm = asked.length ? asked : null;
                savedId = confirm ? '' : form.accountId;
            }
        } catch (e) {
            error = /** @type {Error} */ (e).message;
        }

        // **待っている間に別のフォームへ移っているかもしれません** (中止して
        // 開き直した、パネルを閉じた)。そのときは今の画面に口を出しません。
        if (this._form !== form) {
            return;
        }

        form.busy = false;
        form.error = error;
        form.confirm = confirm;
        if (error || confirm) {
            this.post();
            return;
        }

        // 保存できました。閉じると、追加なら一覧へ、編集ならそのアカウントの
        // 画面へ戻ります (どちらを見ていたかは webview が覚えています)。
        this._form = null;
        this.post();

        // **続けて1回取り直します。** 資格情報を貼り直した直後に赤いままだと、
        // 直ったのかどうか分かりません。取得できる状態になっていないもの
        // (無効にした、資格情報が空のまま) には通信を足しません。
        if (savedId && this.store.fetchableAccounts().some((a) => a.id === savedId)) {
            try {
                await this.store.refreshOne(savedId);
            } catch (e) {
                // **保存は済んでいます。** 取り直しに失敗しても、それは
                // 取得の失敗であって保存の失敗ではありません。画面のその
                // アカウントの欄に理由が出るので、ここでは黙ります。
                this.trace(`保存後の取り直しに失敗しました: ${
                    /** @type {Error} */ (e).message}`);
            }
        }
    }

    /**
     * @param {any} message
     * @returns {Promise<void>}
     */
    async onMessage(message) {
        switch (message?.type) {
            case 'ready':
                // ここが「画面が実際に動き始めた瞬間」です。復元からここまでの
                // 経過時間が、利用者が白紙を見ていた時間そのものになります。
                this.trace('画面の準備ができました');
                this.post();
                return;
            case 'refreshAll':
                await vscode.commands.executeCommand('aiUsageManager.refreshAll');
                return;
            case 'refreshOne':
                await this.store.refreshOne(String(message.accountId));
                return;
            case 'setEnabled':
                try {
                    await this.store.setEnabled(String(message.accountId), Boolean(message.enabled));
                } catch (e) {
                    void vscode.window.showErrorMessage(
                        t('Could not change the setting: {reason}',
                            { reason: /** @type {Error} */ (e).message }));
                }
                return;
            case 'addAccount':
                await vscode.commands.executeCommand('aiUsageManager.addAccount');
                return;
            case 'editAccount':
                await vscode.commands.executeCommand(
                    'aiUsageManager.editAccount', String(message.accountId));
                return;
            case 'relogin':
                // **「ログインし直す」の行き先も、いまはこのフォームです。**
                // 以前はアプリ内ブラウザを開いてログインさせ、Cookie を自動で
                // 回収していました。その道は畳んだので、期限切れの人に必要な
                // のは新しい資格情報を貼る場所です。編集画面がそれで、取り方の
                // 手順もそこに出ます。
                await this.openForm('edit', String(message.accountId));
                return;
            case 'saveAccount':
                await this.saveForm(message);
                return;
            case 'cancelForm':
                this.closeForm();
                return;
            case 'openManual': {
                // **既定のブラウザで開きます。** アプリ内ブラウザは畳みました。
                // 認証側が埋め込みブラウザを拒むことがあり (Google がそうです)、
                // それは埋め込んだ側がパスワード入力を覗けるという理由で存在
                // する保護です。偽装して通すのではなく、普段お使いのブラウザで
                // 取ってきてもらうのが筋で、そちらは大抵ログイン済みでもあります。
                //
                // **行き先は確かめます。** この URL は webview を経由して
                // きます。あちらには取得先サーバーの返した文字列も出るので、
                // 渡されたものをそのまま開く作りにはしません。
                const url = String(message.url ?? '');
                if (/^https:\/\/[^\s]+$/.test(url)) {
                    await vscode.env.openExternal(vscode.Uri.parse(url));
                }
                return;
            }
            case 'deleteAccount':
                await vscode.commands.executeCommand(
                    'aiUsageManager.deleteAccount', String(message.accountId));
                return;
            case 'openSettings':
                await vscode.commands.executeCommand('aiUsageManager.openSettings');
                return;
            case 'selectLanguage':
                await vscode.commands.executeCommand('aiUsageManager.selectLanguage');
                return;
        }
    }

    /** @returns {string} */
    html() {
        const webview = this.panel.webview;
        const media = vscode.Uri.joinPath(this.extensionUri, 'media');
        const script = webview.asWebviewUri(vscode.Uri.joinPath(media, 'main.js'));
        const style = webview.asWebviewUri(vscode.Uri.joinPath(media, 'main.css'));
        const id = nonce();

        // 既定では何も読み込ませず、必要なものだけを許可する。
        // 取得結果には取得先のサーバーが返した文字列が混ざるので、
        // 万一の混入で外部へ通信が飛ばないようにしておく。
        const csp = [
            "default-src 'none'",
            `style-src ${webview.cspSource}`,
            `script-src 'nonce-${id}'`,
            `img-src ${webview.cspSource}`,
        ].join('; ');

        // **訳文は main.js より前に置きます。** あちらは最初の state が届く前に
        // 一度 render() するので (main.js 末尾)、そのときに辞書が無いと
        // 「読み込んでいます...」だけが英語で出ます。postMessage では間に合いません。
        const strings = bundleLiteral(webviewBundle(this.extensionUri));

        return `<!DOCTYPE html>
<html lang="${escapeHtml(language())}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${style}" rel="stylesheet">
<title>${escapeHtml(t('AI Usage'))}</title>
</head>
<body>
<header class="toolbar">
    <div class="tabs">
        <button id="tab-summary" class="tab" type="button">${escapeHtml(t('All accounts'))}</button>
        <span id="tab-detail-wrap" class="hidden"><button id="tab-detail" class="tab" type="button"></button></span>
    </div>
    <div class="spacer"></div>
    <span id="progress" class="progress hidden"></span>
    <button id="language" class="icon-button" type="button" title="${escapeHtml(t('Language'))}">🌐</button>
    <button id="settings" class="icon-button" type="button" title="${escapeHtml(t('Open Settings'))}">⚙</button>
    <button id="refresh" class="primary" type="button">${escapeHtml(t('Refresh'))}</button>
</header>
<main id="content" class="content">
    <div id="panel-host"></div>
</main>
<footer class="statusline"><span id="status"></span></footer>
<script nonce="${id}">window.__l10n = ${strings};</script>
<script nonce="${id}" src="${script}"></script>
</body>
</html>`;
    }
}

module.exports = { UsagePanel };
