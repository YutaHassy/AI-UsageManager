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
        const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (UsagePanel.current) {
            UsagePanel.current.panel.reveal(column);
            UsagePanel.current.post();
            return UsagePanel.current;
        }

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
            guiBusy: this.store.guiBusy ?? null,
            // 一覧をまだ読めていない間は「0件」と言い切らない。復元直後は
            // 読み込みが終わる前に描画が走るので、ここを常に true にすると
            // 「アカウントがありません」が一瞬出る。
            loaded: snapshot !== undefined,
            configPath: snapshot?.configPath ?? '',
            loadError: snapshot?.loadError ?? null,
            encryptionAvailable: snapshot?.encryptionAvailable ?? true,
        });
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
                await vscode.commands.executeCommand(
                    'aiUsageManager.relogin', String(message.accountId));
                return;
            case 'deleteAccount':
                await vscode.commands.executeCommand(
                    'aiUsageManager.deleteAccount', String(message.accountId));
                return;
            case 'reload':
                try {
                    await this.store.reload();
                } catch (e) {
                    void vscode.window.showErrorMessage(
                        t('Could not load the settings: {reason}',
                            { reason: /** @type {Error} */ (e).message }));
                }
                return;
            case 'openConfig':
                if (this.store.snapshot?.configPath) {
                    await vscode.commands.executeCommand(
                        'revealFileInOS', vscode.Uri.file(this.store.snapshot.configPath));
                }
                return;
            case 'openSettings':
                await vscode.commands.executeCommand('aiUsageManager.openSettings');
                return;
            case 'selectLanguage':
                await vscode.commands.executeCommand('aiUsageManager.selectLanguage');
                return;
            case 'showLog':
                await vscode.commands.executeCommand('aiUsageManager.showLog');
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
    <div id="banner" class="banner hidden"><span id="banner-text"></span></div>
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
