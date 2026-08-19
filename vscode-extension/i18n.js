/**
 * 拡張ホスト側の翻訳と、webview へ訳文を渡す仕掛け。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' と Node 組み込み
 * だけで、node_modules は使わない。**このファイルがそのまま vsix に入る原本です。**
 *
 * **ソースに書く文字列は英語で、それがそのままキーです。** 訳文は
 * `l10n/bundle.l10n.<言語>.json` に置きます。訳が無ければ原文が出ます
 * (だから原文は英語でなければなりません)。バックエンド (services/i18n.py) も
 * 同じ規約にしてあります。
 *
 * **表示言語は設定 aiUsageManager.language で選べます。** そのため t() は
 * `vscode.l10n.t()` を使いません。**あれは VSCode 自身の表示言語に固定されていて、
 * 拡張の都合で切り替えられないからです。** l10n/*.json を自分で読んで引きます。
 * 読む先のファイルは vscode.l10n が読むものと同じなので、置き場所と書式は
 * これまでのままです。webview へ渡す辞書 (webviewBundle) とも同じものになり、
 * 拡張ホストと webview で必ず同じ言語が出ます。
 *
 * **module スコープで t() を呼ばないでください。** build_vsix.py の読み込み検査は
 * 'vscode' をスタブに差し替えて require するので、そこで確定させた文字列は
 * 実行時とビルド時で別物になります (build_vsix.py の check_js を参照)。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const vscode = require('vscode');

/** 表示言語の設定。'auto' なら VSCode の表示言語に従います。 */
const SECTION = 'aiUsageManager';
const SETTING = 'language';

/**
 * 選べる言語。package.json の `aiUsageManager.language` の enum と揃えること。
 *
 * **label は自言語表記で、訳しません。** 言語の選択肢を利用者が読めない言語で
 * 出したら、選びようがないからです。t() に通さないでください。
 */
const LANGUAGES = [
    { tag: 'en', label: 'English' },
    { tag: 'ja', label: '日本語' },
    { tag: 'ko', label: '한국어' },
    { tag: 'zh-cn', label: '简体中文' },
];

/** 拡張の置き場所。init() が入れます (l10n/ を読むために要ります)。 */
let extensionPath = '';

/** @type {{tag: string, bundle: Record<string, string>}|undefined} */
let cache;

/**
 * 拡張の置き場所を教えます。**activate() の先頭で1回だけ呼んでください。**
 *
 * t() は l10n/*.json を自分で読むので、拡張がどこにあるかを知る必要があります。
 * 呼ぶ前に t() を使っても落ちはしません (原文がそのまま出ます)。落とさないのは、
 * 翻訳の初期化に失敗しただけで拡張が起動しなくなるほうが困るからです。
 *
 * @param {vscode.Uri} extensionUri
 */
function init(extensionUri) {
    extensionPath = (extensionUri && extensionUri.fsPath) || '';
    cache = undefined;
}

/**
 * 読み込んだ訳文を捨てます。**設定が変わったときに呼んでください。**
 *
 * 呼ばないと、設定を変えても前の言語の辞書を返し続けます。
 */
function invalidate() {
    cache = undefined;
}

/**
 * 設定に書かれている値 ('auto' | 'en' | 'ja' | 'ko' | 'zh-cn')。
 *
 * **知らない値は 'auto' として扱います。** settings.json は enum の外の値でも
 * 書けてしまい (VSCode は警告を出すだけ)、綴りを間違えたときに「英語しか出ない」
 * という直しにくい形で表に出るためです。
 *
 * try で囲ってあるのは build_vsix.py の読み込み検査のためです。あそこでは
 * 'vscode' がスタブ (何を触っても自分を返す Proxy) なので、get() は文字列を
 * 返しません。型を見ているのも同じ理由です。
 *
 * @returns {string}
 */
function configuredLanguage() {
    let value;
    try {
        value = vscode.workspace.getConfiguration(SECTION).get(SETTING, 'auto');
    } catch (e) {
        return 'auto';
    }
    if (typeof value !== 'string') {
        return 'auto';
    }
    const tag = value.trim().toLowerCase();
    return LANGUAGES.some((entry) => entry.tag === tag) ? tag : 'auto';
}

/** VSCode 自身の表示言語 ('ja' / 'en' / 'zh-cn' など)。 */
function displayLanguage() {
    let value;
    try {
        value = vscode.env.language;
    } catch (e) {
        return 'en';
    }
    return typeof value === 'string' && value ? value : 'en';
}

/**
 * いま出すべき言語。設定 → VSCode の表示言語 → 英語 の順で決めます。
 *
 * backend.js がこの値を環境変数 AI_USAGE_MANAGER_LANG でバックエンドへ渡し、
 * panel.js が <html lang> に入れます。**言語の判定はここ1箇所だけです。**
 *
 * @returns {string}
 */
function language() {
    const chosen = configuredLanguage();
    return chosen !== 'auto' ? chosen : displayLanguage();
}

/**
 * 引く順に言語タグを返します。
 *
 * "zh_TW" -> ["zh-tw", "zh"] のように、完全一致から主要サブタグへ落とします。
 * vscode.env.language は "zh-cn" のような小文字タグを返しますが、区切りと
 * 大小はここで吸収します。services/i18n.py の _candidates と同じ落とし方です。
 *
 * @param {string} tag
 * @returns {string[]}
 */
function candidates(tag) {
    const normalized = String(tag || '').trim().toLowerCase().replace(/_/g, '-');
    if (!normalized) {
        return [];
    }
    const primary = normalized.split('-')[0];
    return normalized === primary ? [normalized] : [normalized, primary];
}

/**
 * いまの言語の訳文。1回読んだら使い回します。
 *
 * **キャッシュは言語タグごとに持ちます。** 以前は1つしか持っておらず、
 * 表示言語は VSCode を再起動しない限り変わらないという前提でした。設定で
 * 切り替えられるようになったので、その前提はもう成り立ちません。
 *
 * 英語 (と bundle が無い言語) では空の辞書になり、原文がそのまま出ます。
 *
 * @returns {Record<string, string>}
 */
function bundle() {
    const tag = language();
    if (cache && cache.tag === tag) {
        return cache.bundle;
    }

    /** @type {Record<string, string>} */
    let loaded = {};
    for (const candidate of candidates(extensionPath ? tag : '')) {
        const file = path.join(extensionPath, 'l10n', `bundle.l10n.${candidate}.json`);
        try {
            const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
            if (parsed && typeof parsed === 'object') {
                loaded = parsed;
                break;
            }
        } catch (e) {
            // 無い言語のほうが多い (英語には bundle がありません)。
            // 読めなければ原文で出せばよいので、ここで騒がない。
        }
    }

    // **init() 前は覚えません。** ここで空の辞書を覚えると、あとから init() が
    // 来ても原文のままになります。
    if (extensionPath) {
        cache = { tag, bundle: loaded };
    }
    return loaded;
}

/**
 * 英語の原文を、いまの言語に置き換えて返します。
 *
 * 差し込みは名前付きプレースホルダで書いてください。
 *
 *     t('{provider} is not supported.', { provider: account.providerLabel })
 *
 * 語順は言語ごとに違います。**文字列を + で繋がないでください。**
 * 繋いだ時点で、その順序が翻訳者に直せないものとして固定されます。
 *
 * 置換の規則は media/main.js の t() と同じにしてあります (両側で同じ訳文の
 * 辞書を引くので、片方だけ規則が違うと同じ原文が違う結果になります)。
 *
 * @param {string} message
 * @param {Record<string, any>} [args]
 * @returns {string}
 */
function t(message, args) {
    const catalog = bundle();
    // hasOwnProperty で見るのは、'constructor' のような原文が来たときに
    // Object.prototype 由来のものを訳文と取り違えないためです。
    const text = Object.prototype.hasOwnProperty.call(catalog, message)
        ? catalog[message] : message;
    if (!args) {
        return text;
    }
    return text.replace(/\{(\w+)\}/g, function (whole, name) {
        return Object.prototype.hasOwnProperty.call(args, name)
            ? String(args[name]) : whole;
    });
}

/**
 * アカウントに対する操作の名前。**原文 (=キー) をここに1つだけ置きます。**
 *
 * 同じ文言を extension.js (通知の見出し) と store.js (進行中の表示 guiBusy) の
 * 両方が使います。以前はそれぞれにリテラルが書いてあり、たまたま一致して
 * いるだけでした。英語化して「原文がキー」になると、綴りが一字ずれた時点で
 * 別のキーになり、**片方だけ訳が当たらない**という形で表に出ます。
 * しかもその食い違いは、どちらのファイルを見ても分かりません。
 *
 * ここにある文字列は t() に渡す前の原文です。訳すのは使う側です。
 *
 * **remove だけは別ウィンドウを伴いません。** 削除に窓は要らないので、
 * バックエンドの中だけで終わります。ここに並んでいるからといって、
 * 別ウィンドウの完了を待つ道 (extension.js の runGuiCommand / store.js の
 * runGuiOperation) へ通さないでください。
 */
const ACCOUNT_OPERATIONS = {
    add: 'Adding an account',
    edit: 'Editing an account',
    relogin: 'Signing in again',
    remove: 'Deleting an account',
};

/**
 * 「A、B、C」の並べ方は言語ごとに違います (英語は "A, B and C")。
 * 区切り文字を決め打ちにしないための場所です。
 *
 * @param {string[]} items
 * @returns {string}
 */
function formatList(items) {
    try {
        return new Intl.ListFormat(language(), { style: 'long', type: 'conjunction' })
            .format(items);
    } catch (e) {
        // ICU が痩せている環境でも、並べ方が素朴になるだけで済ませる。
        return items.join(', ');
    }
}

/**
 * webview へ渡す訳文の辞書。
 *
 * **webview からは vscode API を触れません。** そこで media/main.js には
 * 拡張ホストと同じ「原文がキー」の辞書をそのまま渡し、向こう側の t() が
 * 引くようにしてあります。webview 専用のキー一覧を別に持たずに済み、
 * 両側で同じ `t('...')` の書き方ができます。
 *
 * **拡張ホストの t() が引くものと同じ辞書です。** 別々に読んでいた頃は、
 * 拡張ホストが vscode.l10n、webview がこの関数、という二重の経路でした。
 * いまは1つなので、片方だけ言語がずれることが起こりません。
 *
 * extensionUri は init() を呼び忘れたときの保険です (panel.js は持っているので
 * 渡してくれます)。英語 (bundle が無い言語も含む) では空の辞書を返し、
 * main.js は原文のまま出します。
 *
 * @param {vscode.Uri} [extensionUri]
 * @returns {Record<string, string>}
 */
function webviewBundle(extensionUri) {
    if (!extensionPath && extensionUri && extensionUri.fsPath) {
        extensionPath = extensionUri.fsPath;
    }
    return bundle();
}

/**
 * 辞書を <script> の中へ埋め込める形にします。
 *
 * **'<' を必ず潰すこと。** 訳文に "</script>" が入っていると、そこで
 * script が閉じて以降が HTML として解釈されます。この画面は取得先サーバーの
 * 応答を出すため textContent で徹底的に組んでいるので (media/main.js 冒頭)、
 * ここだけ穴を空けるわけにはいきません。
 *
 * @param {Record<string, string>} catalog
 * @returns {string}
 */
function bundleLiteral(catalog) {
    return JSON.stringify(catalog).replace(/</g, '\\u003c');
}

module.exports = {
    init, invalidate, t, language, configuredLanguage, LANGUAGES,
    formatList, webviewBundle, bundleLiteral, ACCOUNT_OPERATIONS,
};
