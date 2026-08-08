/**
 * アクティビティバーのアイコンを「エディタタブを開くボタン」にする仕掛け。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' だけで、
 * node_modules は使わない。**このファイルがそのまま vsix に入る原本です。**
 */

'use strict';

const vscode = require('vscode');

const { language, t } = require('./i18n');

/**
 * アクティビティバーのアイコンを「エディタタブを開くボタン」にするための仕掛け。
 *
 * **アクティビティバーに出すには view の登録が必須です。** VSCode はアイコンの
 * クリックを拡張へ直接渡さず、「その view コンテナを開く」という経路しか
 * 用意していません。コマンドを直接割り当てることはできません。
 *
 * この拡張の画面は横に広い表なので、細いサイドバーに収めると枠の名前と
 * バーが潰れて読めません。そこで view が見えた瞬間にエディタタブを開き、
 * サイドバーは畳みます。結果として、アイコンを押すとタブが開きます。
 *
 * @implements {vscode.WebviewViewProvider}
 */
class LauncherViewProvider {
    static viewType = 'aiUsageManager.launcher';

    /** @param {vscode.WebviewView} view */
    resolveWebviewView(view) {
        view.webview.options = { enableScripts: false };
        // 畳むまでの一瞬だけ見える。何が起きているか分かる文言を置く。
        view.webview.html = `<!DOCTYPE html>
<html lang="${language()}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<style>
body {
    margin: 0;
    padding: 12px;
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-descriptionForeground);
}
</style>
</head>
<body>${t('Opening in a tab...').replace(/</g, '&lt;')}</body>
</html>`;

        // 隠したときに view が破棄されなかった場合、次に開いても
        // resolveWebviewView は呼ばれない。再表示でも動くようにしておく。
        view.onDidChangeVisibility(() => {
            if (view.visible) {
                void this.launch();
            }
        });
        void this.launch();
    }

    /** @returns {Promise<void>} */
    async launch() {
        await vscode.commands.executeCommand('aiUsageManager.open');
        // 用が済んだサイドバーは畳む。開いたままにすると、狭い枠に
        // 「タブで開いています...」とだけ書かれた無駄な領域が残る。
        await vscode.commands.executeCommand('workbench.action.closeSidebar');
    }
}

module.exports = { LauncherViewProvider };
