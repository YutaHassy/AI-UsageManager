"""画面に出す文字列の翻訳。

**ソースに書く文字列は英語で、それがそのままカタログの見出しです。**
辞書は `services/locales/<言語タグ>.json` に置きます。

    t("Account not found.")   ->  ja なら「アカウントが見つかりません。」
                                  カタログに無ければ英語のまま

英語のカタログはありません。原文が英語なので、引く必要が無いからです。
これは VS Code 側の仕組み (vscode.l10n) と同じ考え方で、拡張ホストと
バックエンドで「原文がキー」という規約を1つに揃えてあります。

**言語は import した瞬間に決まります。** set_locale() のような関数は置いて
いません。置くと「呼ぶ前に services.providers を import してしまった」という
順序ミスが必ず起きるためです。プロバイダは
services/providers/__init__.py の _ORDER で import 時にインスタンス化され、
description などのクラス属性もそこで確定します。呼び忘れようのない形に
することが、この形の目的です。

言語は次の順に決めます。

  1. 環境変数 AI_USAGE_MANAGER_LANG
     拡張から起動されたときは backend.js が vscode.env.language を入れます。
     cli.py は gui_helper.py を os.environ ごと渡して起動するので、
     ログイン画面 (PySide6) にもそのまま伝わります。
  2. OS の言語 (手で動かしたときのため)
  3. 英語
"""

import json
import locale as _locale
import logging
import os

logger = logging.getLogger(__name__)

# backend.js が渡してくる環境変数。名前は AI_USAGE_MANAGER_LOGLEVEL や
# AI_USAGE_MANAGER_GUI_PYTHON と揃えてあります。
ENV_LANG = "AI_USAGE_MANAGER_LANG"

_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


def _os_language() -> str:
    """OS の表示言語。分からなければ空文字。

    環境変数を先に見るのは、POSIX ではそちらが利用者の意思表示だからです。
    Windows では通常どれも設定されていないので、UI 言語を直接尋ねます
    (locale.getdefaultlocale() は 3.11 で非推奨になっており、
    locale.getlocale() は setlocale する前は何も返しません)。
    """
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            # "ja_JP.UTF-8" や "ja_JP@foo" から言語部分だけを取り出す
            return value.split(".")[0].split("@")[0]

    if os.name == "nt":
        try:
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return _locale.windows_locale.get(lcid, "")
        except Exception:  # noqa: BLE001
            # 言語の判定に失敗しただけで起動できなくなるのは筋が悪い。
            # 英語で出せば読めない画面にはならない。
            logger.debug("OS の表示言語を判定できませんでした", exc_info=True)

    return ""


def _candidates(tag: str):
    """引く順に言語タグを返します。

    "zh_TW" -> ["zh-tw", "zh"] のように、完全一致から主要サブタグへ落とします。
    vscode.env.language は "zh-cn" のような小文字タグを返しますが、OS 側は
    "ja_JP" の形で返すので、区切りと大小をここで吸収します。
    """
    normalized = (tag or "").strip().lower().replace("_", "-")
    if not normalized:
        return []
    primary = normalized.split("-")[0]
    return [normalized] if normalized == primary else [normalized, primary]


def _load(tag: str) -> dict:
    for candidate in _candidates(tag):
        path = os.path.join(_LOCALES_DIR, f"{candidate}.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                catalog = json.load(f)
        except (OSError, ValueError) as e:
            # カタログが壊れていても英語で動かす。ここで例外を投げると、
            # 拡張からは「バックエンドが無言で死んだ」としか見えない。
            logger.warning("翻訳カタログを読めませんでした (%s): %s", path, e)
            continue
        if isinstance(catalog, dict):
            return catalog
        logger.warning("翻訳カタログの形式が不正です (最上位が辞書ではない): %s", path)
    return {}


language = (os.environ.get(ENV_LANG) or _os_language() or "en").strip().lower()
_CATALOG = _load(language)


def t(message: str, **kwargs) -> str:
    """英語の原文を、いまの言語に置き換えて返します。

    差し込みは名前付きプレースホルダで書いてください。

        t("{provider} is not implemented yet.", provider=provider.label)

    語順は言語ごとに違います。**文字列を + で繋がないでください。**
    繋いだ時点で、その順序が翻訳者に直せないものとして固定されます。
    """
    text = _CATALOG.get(message, message)
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (IndexError, KeyError) as e:
        # 翻訳側のプレースホルダが原文と食い違っている。build_vsix.py の
        # check_l10n() が固める前に見つける想定だが、見逃したときに
        # 取得そのものを落とすのは割に合わない。原文で出して先へ進める。
        logger.warning("翻訳のプレースホルダが原文と一致しません (%s): %r", e, message)
        return message.format(**kwargs)
