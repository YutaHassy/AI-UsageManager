"""vsix を組み立てるスクリプト。

    npm i -g vsce                        # 初回だけ (拡張は node_modules を持ちません)
    python vscode-extension/build_vsix.py

やることは5つだけです。

1. プロジェクト直下の `services/` `models/` `icons/` と `ui/account_dialog.py` を
   `vscode-extension/backend/` へコピーする
2. `node --check` で .js の構文を見る
3. `extension.js` を Node で実際に読み込んでみる (**構文検査では足りないため。**
   理由は check_js の説明を参照)
4. 翻訳カタログ (package.nls / l10n bundle / services/locales) が
   3言語で揃っているかを見る (check_l10n の説明を参照)
5. `vsce package` で固めて `dist/` へ出す

**コピーを取るのは、vsix が拡張ディレクトリの外を参照できないためです。**
取得ロジックとログイン画面の原本はプロジェクト直下の1つだけで、配布のたびに
そこから同じものを同梱しています (cli.py / gui_helper.py の sys.path 解決も
「コピー前ならプロジェクト直下、コピー後なら自分の隣」という同じ前提です)。

**コピー先は毎回消してから作り直します。** 上書きだけだと、原本から消した
ファイルが backend/ に残り続け、vsix にだけ古いモジュールが入ります。
そうなると exe と拡張で挙動が違い、しかも原本を見ても理由が分かりません。

**このスクリプト自身は vsix に入りません** (.vscodeignore で除外)。
展開して読めるのは「動いているもの」で、それを配布物へ固める手順は
リポジトリ側にあります。

※ このファイルは README.md の記述と vsix の実際の中身から再構成したもの
   です (原本は vsix に含まれていません)。
"""

import json
import os
import re
import shutil
import subprocess
import time
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_BACKEND = os.path.join(_HERE, "backend")
_DIST = os.path.join(_ROOT, "dist")

# プロジェクト直下からコピーするもの。ディレクトリはまるごと、
# ui/ は拡張が使う account_dialog.py だけ (デスクトップ版の画面は要らない)。
_COPY_DIRS = ["services", "models", "icons"]
_COPY_FILES = [os.path.join("ui", "account_dialog.py"),
               os.path.join("ui", "session_refresher.py")]

# backend/ に残す原本。コピーで消してはいけないもの。
_KEEP = {"cli.py", "gui_helper.py"}

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def fail(message: str):
    print(f"[build] 中断: {message}", file=sys.stderr)
    sys.exit(1)


# 削除の再試行。合計でおよそ 10 秒まで粘ります。
_REMOVE_ATTEMPTS = 8
_REMOVE_WAIT = 0.4


def _remove_file(path: str):
    """ファイルを1つ消します。消せなければ少し待って試し直します。

    ウイルス対策の走査、エクスプローラーのサムネイル取得、OneDrive の同期、
    検索インデクサなどが、書いたばかりのファイルを一瞬掴みます。
    どれも数百ミリ秒で離すので、一度の失敗で止める理由はありません。
    """
    for attempt in range(_REMOVE_ATTEMPTS):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == _REMOVE_ATTEMPTS - 1:
                raise
            time.sleep(_REMOVE_WAIT * (attempt + 1))


def _purge(root: str):
    """root の下からファイルを消します。**空になったフォルダは残っても構いません。**

    以前はここで shutil.rmtree を使い、フォルダごと消していました。
    ところがこのリポジトリは OneDrive の同期対象に置かれており、
    **フォルダの削除だけが繰り返し PermissionError で落ちます**
    (ファイルの削除は通り、同じフォルダを直後に手で消すと消える、
    という状態でした)。待ち時間を伸ばしても解決しませんでした。

    そこで**フォルダを消すことに依存しない**作りにしました。防ぎたいのは
    「原本から消したモジュールが backend/ に残り、vsix にだけ古いものが
    入る」ことなので、**消すべきはファイルであってフォルダではありません。**
    中身が空のフォルダが残っても、vsix には何も足しません
    (vsce は空フォルダを梱包しません)。
    """
    for base, dirs, files in os.walk(root, topdown=False):
        for name in files:
            _remove_file(os.path.join(base, name))
        for name in dirs:
            try:
                os.rmdir(os.path.join(base, name))
            except OSError:
                pass  # 空のまま残るだけ。上の docstring のとおり実害なし
    try:
        os.rmdir(root)
    except OSError:
        pass


def clean_backend():
    """backend/ から、前回コピーしたものを消します。"""
    if not os.path.isdir(_BACKEND):
        os.makedirs(_BACKEND)
        return
    for name in os.listdir(_BACKEND):
        if name in _KEEP:
            continue
        path = os.path.join(_BACKEND, name)
        _purge(path) if os.path.isdir(path) else _remove_file(path)
        print(f"[build] 削除: backend/{name}")


def copy_sources():
    """プロジェクト直下の原本を backend/ へコピーします。"""
    for name in _COPY_DIRS:
        src = os.path.join(_ROOT, name)
        if not os.path.isdir(src):
            fail(f"{name}/ がプロジェクト直下に見つかりません: {src}")
        # dirs_exist_ok=True なのは、_purge が空のフォルダを残しうるためです
        # (理由は _purge の docstring を参照)。中身は消えているので、
        # 上書きではなく実質の作り直しになります。
        shutil.copytree(src, os.path.join(_BACKEND, name),
                        ignore=_IGNORE, dirs_exist_ok=True)
        print(f"[build] コピー: {name}/ -> backend/{name}/")

    for rel in _COPY_FILES:
        src = os.path.join(_ROOT, rel)
        if not os.path.isfile(src):
            fail(f"{rel} がプロジェクト直下に見つかりません: {src}")
        dst = os.path.join(_BACKEND, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"[build] コピー: {rel} -> backend/{rel}")


# 読み込み検査の中身。`node -e <このスクリプト> <extension.js の絶対パス>` で走ります。
#
# **'vscode' は拡張ホストの中にしか無いモジュールです。** 素の Node には無いので、
# ふつうに require すると MODULE_NOT_FOUND で落ちます。そこで Module._load を
# 差し替えて、'vscode' を求められたときだけスタブを返します。
#
# スタブは「何を get しても・呼んでも・new しても自分を返す」Proxy で足ります。
# 検査したいのは module スコープの評価が通るかどうかだけで、vscode の API が
# 何を返すかは一切見ないためです。**ただし 'then' だけは undefined を返します。**
# 返してしまうと await や Promise.resolve がこのスタブを thenable と誤認し、
# then(resolve, reject) を呼んで永久に解決しない待ちを作ります。
#
# require('./extension.js') は activate() を呼びません。評価されるのは
# module スコープだけ＝**まさに今回のバグが住んでいる範囲**です。
# extension.js が backend/panel/store/launcher を require するので、
# 1回で拡張側の .js 5本すべてを読み込めます。
_LOAD_CHECK_JS = r"""
'use strict';

const Module = require('module');

const stub = new Proxy(function () {}, {
    get(_target, prop) {
        // thenable と誤認されないように then だけは undefined を返す。
        if (prop === 'then') { return undefined; }
        return stub;
    },
    apply() { return stub; },
    construct() { return stub; },
});

const load = Module._load;
Module._load = function (request, ...rest) {
    if (request === 'vscode') { return stub; }
    return load.call(this, request, ...rest);
};

// node -e では process.argv[1] が最初の追加引数 (= extension.js の絶対パス)。
require(process.argv[1]);
"""


def copy_license():
    """プロジェクト直下の LICENSE を拡張直下へ複製します。

    **vsce は梱包元ディレクトリ (= vscode-extension/) の LICENSE しか見ません。**
    リポジトリ直下に置いたままだと vsix に入らず、マーケットプレイスの
    ページに License のリンクが出ません。

    原本は1つ (リポジトリ直下) のままにして、配布のたびにここから複製します。
    backend/ の作り方と同じ考え方です — 複製を追跡すると、同じ変更が2箇所に
    出て差分が読めなくなるため、複製は .gitignore に入れてあります。
    """
    src = os.path.join(_ROOT, "LICENSE")
    if not os.path.isfile(src):
        fail(f"LICENSE がプロジェクト直下に見つかりません: {src}")
    shutil.copy2(src, os.path.join(_HERE, "LICENSE"))
    print("[build] コピー: LICENSE -> vscode-extension/LICENSE")


def check_js():
    """.js の構文を見て、そのあと実際に読み込めるかまで見ます。

    **固める前に見ます。** 拡張はコンパイルされないので、構文エラーは
    利用者が拡張を入れて初めて分かります (拡張ホストが黙って読み込みに
    失敗するだけで、画面には何も出ません)。

    **`node --check` だけでは足りません。** あれが見るのは構文だけで、
    「定義されていないものを参照している」は構文として正しいため素通りします。
    露見するのは拡張ホストが module を評価した瞬間です。実際 1.1.1 では
    backend.js の冒頭 (docstring / 'use strict' / require 5本 / class
    BackendError) が丸ごと欠けたまま `node --check` を通過し、**壊れた vsix が
    そのまま dist/ に出ました。** 拡張ホストでは最終行の
    `module.exports = { Backend, BackendError };` で

        ReferenceError: BackendError is not defined

    となって activate() ごと不発になり、webview の serializer が登録されないため
    復元されたタブが永久に白紙になる、という形で表に出ました。
    そこで **Node で本当に require してみる段**を後ろに足してあります。

    **media/main.js は読み込み検査の対象に入れません** (`node --check` の対象には
    従来どおり入っています)。あれは webview の中で動く前提で、module スコープから
    acquireVsCodeApi() を呼ぶので、Node で読み込めば必ず落ちます。
    """
    targets = []
    for base, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d not in ("backend", "node_modules", ".git")]
        targets += [os.path.join(base, f) for f in files if f.endswith(".js")]

    if not targets:
        fail(".js が1つも見つかりません。")

    node = shutil.which("node")
    if node is None:
        fail("node が見つかりません。Node.js を入れてください。")

    for path in sorted(targets):
        result = subprocess.run([node, "--check", path],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            fail(f"構文エラー: {os.path.relpath(path, _ROOT)}")
        print(f"[build] 構文 OK: {os.path.relpath(path, _ROOT)}")

    # ---- ここから読み込み検査 (構文検査では足りない理由は docstring を参照) ----
    entry = os.path.join(_HERE, "extension.js")
    if not os.path.isfile(entry):
        fail(f"extension.js が見つかりません: {entry}")

    # encoding を明示するのは、Node が stdout/stderr へ UTF-8 で書くためです。
    # Windows の既定 (cp932) で読むと、失敗したときのスタックが化けて
    # 原因が読めなくなります。
    result = subprocess.run([node, "-e", _LOAD_CHECK_JS, entry],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        fail("読み込みエラー: extension.js を Node で require できません。"
             " 上のスタックを見てください (構文は正しくても、未定義の参照や"
             " module スコープの例外はここで初めて出ます)。")
    print("[build] 読み込み OK: extension.js とそこから require される .js")


# ---- 翻訳カタログの検査 ----------------------------------------------------
#
# 対応する言語。**英語は入れません。** 英語は原文そのもの (ソースに書いてある
# 文字列) で、カタログを持ちません。VSCode も表示言語が英語のときは bundle を
# 読まずに原文を出します。
_LANGS = ["ja", "zh-cn", "ko"]

# %key% の参照。package.json の中でだけ使う書き方です。
_NLS_REF = re.compile(r"%([A-Za-z0-9_.\-]+)%")

# 差し込み位置。原文と訳文で顔ぶれが一致していなければなりません。
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _check_same_keys(group: str, catalogs: dict, problems: list):
    """3言語のカタログが同じ見出しを持っているかを見ます。

    **これが check_l10n の本体です。** 言語を1つ足すたびに、あるいは文言を
    1つ直すたびに、3枚の JSON を手で揃え続けることになります。揃っていない
    ことは画面を見ても分かりません — 訳が無い箇所は英語で出るだけなので、
    「まだ訳していない」のか「壊れている」のかが見分けられないのです。
    """
    union = set()
    for keys in catalogs.values():
        union |= set(keys)

    for lang, keys in catalogs.items():
        missing = sorted(union - set(keys))
        extra = sorted(set(keys) - union)
        for key in missing:
            problems.append(f"{group}: {lang} に見出しがありません: {key!r}")
        for key in extra:
            problems.append(f"{group}: {lang} にだけある見出しです: {key!r}")


def _check_placeholders(group: str, lang: str, source: str, translated: str,
                        problems: list):
    want = set(_PLACEHOLDER.findall(source))
    got = set(_PLACEHOLDER.findall(translated))
    if want != got:
        problems.append(
            f"{group}: {lang} の差し込みが原文と違います (原文 {sorted(want)} / "
            f"訳文 {sorted(got)}): {source[:60]!r}"
        )


def check_l10n():
    """翻訳カタログが揃っているかを、固める前に見ます。

    見るのは3系統です。

      package.nls.*.json          … package.json の貢献ポイント (コマンド名等)
      l10n/bundle.l10n.*.json     … 拡張ホストと webview の JS
      services/locales/*.json     … バックエンド (Python)

    **見ないもの:** 「ソースにある原文が全部カタログに載っているか」は
    見ません。原文は複数行の連結で書かれることが多く、字面から確実に
    取り出せないためです。載っていない原文は英語のまま出るだけなので、
    黙って壊れることはありません。ここで防ぎたいのは**言語ごとの食い違い**
    — 3枚のうち1枚だけ直した、という状態です。
    """
    problems = []

    # 1. package.nls: 既定 (英語) と3言語
    base_path = os.path.join(_HERE, "package.nls.json")
    if not os.path.isfile(base_path):
        fail("package.nls.json がありません (package.json の %key% を引く先です)。")
    base = _load_json(base_path)

    nls = {}
    for lang in _LANGS:
        path = os.path.join(_HERE, f"package.nls.{lang}.json")
        if not os.path.isfile(path):
            problems.append(f"package.nls: package.nls.{lang}.json がありません。")
            continue
        nls[lang] = _load_json(path)
        for key, value in nls[lang].items():
            if key in base:
                _check_placeholders("package.nls", lang, base[key], value, problems)
    # 既定も同じ顔ぶれでなければならない (英語だけ増減していないか)
    _check_same_keys("package.nls", dict(nls, **{"package.nls.json": base}), problems)

    # package.json が参照している %key% が引ける先にあるか。
    # ここが抜けていると、画面に "%command.open.title%" とそのまま出ます。
    with open(os.path.join(_HERE, "package.json"), encoding="utf-8") as f:
        manifest_text = f.read()
    for key in sorted(set(_NLS_REF.findall(manifest_text))):
        if key not in base:
            problems.append(f"package.json: %{key}% を package.nls.json で引けません。")

    # 2. l10n bundle (JS) と 3. services/locales (Python)
    #    どちらも「英語の原文が見出し」なので、見出しそのものと訳文を突き合わせます。
    for group, directory, pattern in (
        ("l10n bundle", os.path.join(_HERE, "l10n"), "bundle.l10n.{lang}.json"),
        ("services/locales", os.path.join(_ROOT, "services", "locales"), "{lang}.json"),
    ):
        catalogs = {}
        for lang in _LANGS:
            path = os.path.join(directory, pattern.format(lang=lang))
            if not os.path.isfile(path):
                problems.append(f"{group}: {os.path.basename(path)} がありません。")
                continue
            catalogs[lang] = _load_json(path)
            for source, translated in catalogs[lang].items():
                if not isinstance(translated, str):
                    problems.append(
                        f"{group}: {lang} の訳文が文字列ではありません: {source[:60]!r}")
                    continue
                _check_placeholders(group, lang, source, translated, problems)
        _check_same_keys(group, catalogs, problems)

    if problems:
        for problem in problems:
            print(f"[build] {problem}", file=sys.stderr)
        fail(f"翻訳カタログが揃っていません ({len(problems)} 件)。")

    print(f"[build] 翻訳 OK: {', '.join(_LANGS)} (+ 原文の英語)")


def package() -> str:
    """vsce で固めて、できあがった vsix のパスを返します。"""
    with open(os.path.join(_HERE, "package.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    out = os.path.join(_DIST, f"{manifest['name']}-{manifest['version']}.vsix")
    os.makedirs(_DIST, exist_ok=True)

    # Windows では vsce.cmd。which は拡張子付きの実体を返す。
    # --allow-missing-repository は付けません。package.json に repository を
    # 書いたので、消えたら梱包の時点で気づけるようにしておきます
    # (マーケットプレイスのページから Repository のリンクが黙って消えるため)。
    vsce = shutil.which("vsce") or shutil.which("vsce.cmd")
    if vsce is not None:
        command = [vsce, "package", "--out", out]
    else:
        # **入っていなければ npx で取りに行きます。** この拡張は
        # node_modules を持たない方針なので、vsce だけのために
        # グローバル導入を必須にすると、環境を1つ汚してからでないと
        # ビルドできないことになります。npx なら都度取得で済みます。
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        if npx is None:
            fail("vsce も npx も見つかりません。Node.js を入れるか "
                 "`npm i -g @vscode/vsce` を実行してください。")
        command = [npx, "--yes", "@vscode/vsce", "package", "--out", out]

    print(f"[build] 梱包: {' '.join(command[:3])} ...")
    result = subprocess.run(command, cwd=_HERE)
    if result.returncode != 0:
        fail("vsce package が失敗しました。")
    return out


def main():
    print(f"[build] プロジェクト: {_ROOT}")
    clean_backend()
    copy_sources()
    copy_license()
    check_js()
    check_l10n()
    out = package()
    size = os.path.getsize(out)
    print(f"[build] 完成: {out} ({size:,} バイト)")


if __name__ == "__main__":
    main()
