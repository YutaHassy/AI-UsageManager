"""アカウントごとに独立した永続ブラウザプロファイルを管理する。

Claude.ai のセッション Cookie は、ログイン/ログアウトのたびに変わります。
普段使っているブラウザから手でコピーしてくる運用だと、ブラウザ側で
ログアウトするたびにアプリ側の Cookie が失効してしまいます。

そこでアカウントごとに専用のブラウザプロファイル (Cookie の保管場所) を
ディスク上に持ち、アプリが自分専用のセッションを維持するようにします。

これにより:
  - 普段使いのブラウザでログアウトしても、アプリ側のセッションは切れない
  - 複数アカウントを同時にログイン状態で保持できる
    (1つのブラウザでは後からログインした方でセッションが上書きされてしまう)
  - アプリを再起動してもログイン状態が残るため、再ログインが1クリックで済む
"""

import logging
import os
import re

from PySide6.QtCore import QDateTime, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEngineProfile

# 置き場所と破棄には Qt が要らないので、Qt を import しない側 (profile_storage)
# に置いてあります。**同じことを二重に書かないでください。** ここで取り込み
# 直しているのは、今までどおり browser_profile.profile_dir のように呼べる形を
# 保つためです。
from services.profile_storage import (  # noqa: F401
    has_saved_session, profile_dir, profiles_root, remove_profile,
)

logger = logging.getLogger(__name__)

# QtWebEngine の既定 UA に入る "QtWebEngine/6.11.1" の部分。
# このトークンが残っていると Google のログインが埋め込みブラウザと判定して弾く。
_QTWEBENGINE_TOKEN = re.compile(r"\s*QtWebEngine/\S+")

# 既定 UA を読めなかったときだけ使う保険。
# **バージョンを決め打ちしないこと。** 詳細は chrome_user_agent() を参照。
FALLBACK_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def chrome_user_agent(profile: QWebEngineProfile) -> str:
    """既定 UA から QtWebEngine トークンだけを外した UA を返します。

    **Chrome のバージョンをここで決め打ちしてはいけません。**
    Chromium は UA とは別に Client Hints (Sec-CH-UA) でも自分の版を送ります。
    UA だけ古い版を名乗ると両者が食い違い、Google のログイン画面が
    「このブラウザまたはアプリは安全でない可能性があります」で拒否します。

    実際にこれが起きました: Chrome/120 を決め打ちしていたところ、
    Qt 6.11 の同梱 Chromium が 140 になり、ChatGPT の Google ログインが
    通らなくなりました。既定 UA から導出すれば Qt を上げても自動で追随します。
    """
    default = profile.httpUserAgent() or ""
    user_agent = _QTWEBENGINE_TOKEN.sub("", default).strip()
    if "Chrome/" not in user_agent:
        # 既定 UA の形が想定と違う。偽装せずに保険を使う方がまだ読める。
        logger.warning("既定の User-Agent を解釈できませんでした: %r", default)
        return FALLBACK_CHROME_UA
    return user_agent

# 同じ保存先に対して QWebEngineProfile を二重に作ると Qt が警告を出し、
# 一方の書き込みが失われるため、必ずここで使い回す。
_profiles = {}


def get_profile(account_id: str) -> QWebEngineProfile:
    """アカウント専用の永続プロファイルを返します (無ければ作成)。"""
    key = str(account_id)
    existing = _profiles.get(key)
    if existing is not None:
        return existing

    directory = profile_dir(key)
    os.makedirs(directory, exist_ok=True)

    # 名前付きで生成するとオフレコード (メモリのみ) ではなくなる
    profile = QWebEngineProfile(f"cum-{key}")
    profile.setPersistentStoragePath(directory)
    profile.setCachePath(os.path.join(directory, "cache"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    # setHttpUserAgent の前に読むこと (上書き後は既定 UA を取り戻せない)
    profile.setHttpUserAgent(chrome_user_agent(profile))

    _profiles[key] = profile
    logger.info("ブラウザプロファイルを用意しました: %s", directory)
    return profile


def inject_cookies(account_id: str, cookie_header: str, domain: str) -> int:
    """貼ってもらった Cookie 一式を、このアカウント専用のプロファイルに預けます。

    **セッションを長く維持するための要です。**

    Google のセッションは1つの Cookie では成り立っておらず、短命な
    __Secure-1PSIDTS を差し替えるために SID / HSID / SAPISID などが要ります。
    設定ファイルには認証に必要な最小限しか残さない方針なので (config.json が
    漏れたときの被害を Gemini の範囲に留めるため)、残りをここへ預けます。

    こうしておくと以降は Chromium が本物のブラウザとまったく同じ手順で
    セッションを維持します。Google 側がどんな更新方式に変えても、
    こちらが追随して実装し直す必要がありません。

    保存先は普段お使いのブラウザと同じくユーザープロファイル配下で、
    アカウントごとに分かれています。

    戻り値は書き込んだ Cookie の数。
    """
    store = get_profile(account_id).cookieStore()
    origin = QUrl(f"https://{domain.lstrip('.')}/")

    cookies = build_cookies(cookie_header, domain)
    for cookie in cookies:
        store.setCookie(cookie, origin)

    # 値は絶対に出さない (Google アカウント全体を扱えるものが混ざっている)
    logger.info("ブラウザプロファイルに Cookie を %d 件預けました (account=%s)",
                len(cookies), account_id)
    return len(cookies)


def build_cookies(cookie_header: str, domain: str) -> list:
    """貼り付けられた内容を QNetworkCookie の一覧に変換します。

    貼り付け元は開発者ツールの「Copy as cURL」なので、Cookie 以外の断片
    (URL、-H で始まるヘッダ指定、シェルの引用符) が必ず混ざります。
    Cookie の形をしていないものはここで捨てます。
    """
    host = domain.lstrip(".")
    # 貼り付け元での有効期限までは分からないので十分に先を入れておく。
    # 実際の期限は、以降のアクセスで Google が Set-Cookie で上書きしてくる。
    expires = QDateTime.currentDateTime().addYears(1)

    cookies = []
    for part in (cookie_header or "").split(";"):
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            continue

        # 先頭の Cookie には貼り付けの飾り (curl ... -b ') が食い込むことがある。
        # 捨ててしまうと、よりによって最初の1件 = 主要な Cookie を失うため、
        # 区切りになりうる文字より後ろを名前として拾い直す。
        for separator in ' \t"\'^':
            if separator in name:
                name = name.rsplit(separator, 1)[1]
        if not name or any(ch in name for ch in ' \t"\'^\\/'):
            continue

        cookie = QNetworkCookie(name.encode("utf-8"), value.encode("utf-8"))
        cookie.setPath("/")
        # __Secure- / __Host- 接頭辞は Secure でないとブラウザ側が受け付けない
        cookie.setSecure(True)
        # "__Host-" 付きは仕様上ドメインを指定できない (指定すると破棄される)
        if not name.startswith("__Host-"):
            cookie.setDomain(f".{host}")
        cookie.setExpirationDate(expires)
        cookies.append(cookie)
    return cookies


def forget_profile(account_id: str) -> None:
    """アカウント削除時などにプロファイルを破棄します。

    ディスク側を消すのは profile_storage の担当です。**こちらにしか無いのは
    使い回し用の辞書 (_profiles) の後始末で、そのためにこの入口があります。**
    Qt を持たないプロセスから消すときは profile_storage.remove_profile()
    を直接呼んでください (そちらには片付ける辞書がありません)。
    """
    key = str(account_id)
    _profiles.pop(key, None)
    remove_profile(key)


def release_all() -> None:
    """終了時にプロファイルへの参照を解放します。"""
    _profiles.clear()
