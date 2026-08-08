"""プロキシ設定を requests と QtWebEngine の両方に適用する。

社内プロキシが認証を要求する環境では、次のような非対称が起きます。

  - requests は環境変数 HTTP_PROXY / HTTPS_PROXY を読む。
    この値に "http://ユーザー:パスワード@proxy:8080" のように資格情報が
    含まれていれば、そのまま認証を通過できる。
  - QtWebEngine (Chromium) は Windows のプロキシ設定を見る。
    こちらには通常パスワードが入っていないため 407 が返り、
    proxyAuthenticationRequired に応答しないとページが開けない。

そのため「requests では通信できるのに、アプリ内のミニブラウザだけ真っ白」
という状態になります。ここで資格情報を一元管理し、両方に供給します。
"""

import logging
import os
import re
from urllib.parse import quote, urlparse

from services.i18n import t

logger = logging.getLogger(__name__)

MODE_SYSTEM = "system"
MODE_MANUAL = "manual"
MODE_NONE = "none"

# Qt の applicationProxy をこのプロセスで上書きしたか。
# 一度上書きすると Chromium のシステムプロキシ自動検出には戻せないため、
# system モードでは原則として触らない。
_qt_proxy_overridden = False

# 現在の設定 (config.json から読み込んで configure() で反映する)
_settings = {
    "proxy_mode": MODE_SYSTEM,
    "proxy_host": "",
    "proxy_port": 8080,
    "proxy_username": "",
    "proxy_password": "",
}


def configure(settings: dict) -> None:
    """アプリ設定からプロキシ設定を取り込みます。"""
    for key in _settings:
        if key in settings:
            _settings[key] = settings[key]
    logger.info(
        "プロキシ設定: mode=%s host=%s port=%s 認証=%s",
        _settings["proxy_mode"], _settings["proxy_host"] or "(システム設定)",
        _settings["proxy_port"], "あり" if _settings["proxy_username"] else "なし",
    )
    _apply_qt_application_proxy()


def current() -> dict:
    return dict(_settings)


def has_credentials() -> bool:
    return bool(credentials()[0])


def credentials(host: str = None) -> tuple:
    """プロキシ認証に使うユーザーID・パスワードを返します。

    設定画面で入力された値を優先し、未入力なら環境変数
    (HTTPS_PROXY 等の "http://ユーザー:パスワード@host:port" 形式) から補います。

    requests は環境変数を直接読むので何もしなくても通りますが、
    QtWebEngine は環境変数から資格情報を読み取りません
    (ホストとポートだけ拾ってユーザーIDは空になる)。
    そのため補わないと、アプリ内のミニブラウザだけが 407 で開けなくなります。

    host: これから資格情報を送ろうとしているプロキシサーバーのホスト名。
    環境変数由来の資格情報は、あくまで「環境変数に書かれたプロキシ」宛てに
    発行されたものです。manual モードで環境変数とは別のプロキシを指定した
    場合にまで使い回すと、社内プロキシの AD 資格情報が無関係なホストへ
    渡ってしまうため、host を渡された場合は一致するときだけ補います
    (大文字小文字は区別しません)。host を省略した場合は呼び出し漏れで
    既存の動作を壊さないよう、従来どおり無条件に補います。
    """
    user = (_settings.get("proxy_username") or "").strip()
    if user:
        # 設定画面で明示的に入力された値は利用者が自分で指定したものなので、
        # host が何であっても常に使う。
        return user, _settings.get("proxy_password", "")

    if _settings.get("proxy_mode") == MODE_NONE:
        return "", ""

    detected = detect_from_environment()
    env_host = detected.get("host", "")

    if host is not None and env_host and host.strip().lower() != env_host.strip().lower():
        # 接続先が環境変数のプロキシと違う。ここで補ってしまうと、
        # 環境変数に入っている (多くは社内 AD の) 資格情報が無関係な
        # ホストへ送信されてしまうため、原因を追えるよう記録した上で空を返す。
        logger.warning(
            "環境変数のプロキシ資格情報は %s 宛てのものですが、接続先は %s のため使用しません。"
            "このホスト向けの資格情報が必要な場合は「⚙ 設定」で明示的に入力してください。",
            env_host, host,
        )
        return "", ""

    return detected.get("username", ""), detected.get("password", "")


# ---------------- 環境変数からの読み取り ----------------

def _parse_env_proxy(value: str) -> dict:
    """HTTP_PROXY 形式の文字列を分解します。"""
    if not value:
        return {}
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 8080,
        "username": parsed.username or "",
        "password": parsed.password or "",
    }


def detect_from_environment() -> dict:
    """環境変数 / Windows のプロキシ設定から、入力候補を推測します。

    設定ダイアログの「環境変数から取り込む」で使います。
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        parsed = _parse_env_proxy(os.environ.get(name, ""))
        if parsed.get("host"):
            return parsed

    # 環境変数が無ければ Windows のプロキシ設定を見る (資格情報は入っていない)
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if enabled:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            # "host:port" または "http=host:port;https=host:port"
            match = re.search(r"(?:https?=)?([^;=\s]+):(\d+)", str(server))
            if match:
                return {"host": match.group(1), "port": int(match.group(2)),
                        "username": "", "password": ""}
    except (ImportError, OSError, FileNotFoundError):
        pass

    return {}


# ---------------- requests への適用 ----------------

def _bypasses_proxy(url: str) -> bool:
    """NO_PROXY 等により、この URL がプロキシを迂回すべきかどうか。"""
    try:
        from requests.utils import should_bypass_proxies
        return bool(should_bypass_proxies(url, no_proxy=None))
    except Exception:
        # requests の内部 API が変わっても通信自体は止めない
        logger.debug("NO_PROXY の判定に失敗しました", exc_info=True)
        return False


def requests_proxies(url: str = None):
    """requests の proxies 引数に渡す値を返します。

    None を返した場合、requests は環境変数の設定をそのまま使います。

    url を渡すと NO_PROXY を尊重します。これを見ないと、manual モードの
    ときに社内向けホスト (NO_PROXY に登録済み) への通信まで社内プロキシへ
    投げてしまい、「system モードでは取れるのに manual にすると失敗する」
    という原因の分かりにくい不具合になります。
    """
    if url and _bypasses_proxy(url):
        # 明示的に「プロキシを使わない」を返す。None を返すと呼び出し側で
        # 環境変数が再解釈され、モードによって挙動が変わってしまう。
        return {"http": None, "https": None}

    mode = _settings.get("proxy_mode", MODE_SYSTEM)

    if mode == MODE_NONE:
        # 環境変数を無視して直接接続する
        return {"http": None, "https": None}

    if mode == MODE_MANUAL:
        host = (_settings.get("proxy_host") or "").strip()
        if not host:
            return None
        url = _build_proxy_url(host, _settings.get("proxy_port", 8080))
        return {"http": url, "https": url}

    # system モード: 環境変数に資格情報が無く、こちらに保存されている場合だけ補う
    if has_credentials():
        env_value = ""
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env_value = os.environ.get(name, "")
            if env_value:
                break
        parsed = _parse_env_proxy(env_value)
        if parsed.get("host") and not parsed.get("username"):
            url = _build_proxy_url(parsed["host"], parsed["port"])
            return {"http": url, "https": url}

    return None


def _build_proxy_url(host: str, port) -> str:
    user, password = credentials(host)
    if user:
        # 記号を含むIDやパスワードでも壊れないようにエスケープする
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    else:
        auth = ""
    return f"http://{auth}{host}:{port}"


def redacted_proxy_url() -> str:
    """ログ・画面表示用に、資格情報を伏せたプロキシ表記を返します。"""
    mode = _settings.get("proxy_mode", MODE_SYSTEM)
    if mode == MODE_NONE:
        return t("(not used)")
    if mode == MODE_MANUAL:
        host = _settings.get("proxy_host") or t("(not set)")
        return f"{host}:{_settings.get('proxy_port', 8080)}"
    detected = detect_from_environment()
    if detected.get("host"):
        return t("{host}:{port} (system settings)",
                 host=detected['host'], port=detected['port'])
    return t("(system settings / not detected)")


# ---------------- Qt (WebEngine) への適用 ----------------

def _apply_qt_application_proxy() -> None:
    """手動指定のときだけ Qt 側のプロキシを明示的に設定します。

    system モードでは Chromium が Windows の設定を自動で使うため何もしません
    (認証だけ install_proxy_authentication() で肩代わりします)。
    """
    # global 宣言は、下の早期 return で _qt_proxy_overridden を「読む」より前に
    # 置く必要があります (読んだあとに宣言すると SyntaxError になる)。
    global _qt_proxy_overridden

    mode = _settings.get("proxy_mode", MODE_SYSTEM)
    # manual を先に求めておくのは、MODE_MANUAL でもホスト未設定なら
    # 従来から「system モード側へ落ちて return」していたためです。
    # この分岐をそのまま保存します。
    manual = mode == MODE_MANUAL and bool((_settings.get("proxy_host") or "").strip())

    # --- system モード (何もしない場合) ---
    # **PySide6 を読み込むのは、実際に applicationProxy を触るときだけです。**
    # 既定 (system モード・上書きなし) では下で何もせず戻るのに、import だけで
    # 62 ms かかっていました。cli.py はこれを backend.load() から呼び、
    # notify("ready") より必ず前に通るため、その 62 ms はまるごと
    # 「拡張が待たされる時間」でした。
    # 判定を import より前へ出しただけで、どのモードで何をするかは変えていません。
    #
    # ここで setApplicationProxy() を呼んではいけない。
    # 型 DefaultProxy の QNetworkProxy を applicationProxy に設定すると
    # 「applicationProxy に従う」という自己参照になり、Qt はプロキシ無しに倒す。
    # その結果 Chromium はシステムのプロキシ設定を使わなくなり、
    # claude.ai を自力で名前解決しようとして ERR_NAME_NOT_RESOLVED になる。
    # 何もしなければ Chromium が Windows の設定 (PAC や除外リストも含む) を
    # そのまま使ってくれる。
    if not manual and mode != MODE_NONE and not _qt_proxy_overridden:
        return

    try:
        from PySide6.QtNetwork import QNetworkProxy
    except ImportError:  # pragma: no cover
        return

    if manual:
        _set_qt_http_proxy(QNetworkProxy, _settings["proxy_host"].strip(),
                           _settings.get("proxy_port", 8080))
        _qt_proxy_overridden = True
        return

    if mode == MODE_NONE:
        proxy = QNetworkProxy()
        proxy.setType(QNetworkProxy.ProxyType.NoProxy)
        QNetworkProxy.setApplicationProxy(proxy)
        _qt_proxy_overridden = True
        return

    # --- system モード (一度上書きしたあとに戻ってきた場合) ---
    # 同一セッション内で手動指定などから戻ってきた場合だけは、
    # 一度上書きした applicationProxy を元に戻せないため、
    # 検出した値で近似する (完全に戻すには再起動が必要)。
    detected = detect_from_environment()
    if detected.get("host"):
        _set_qt_http_proxy(QNetworkProxy, detected["host"], detected["port"])
        logger.info(
            "システム設定のプロキシ %s:%s を適用しました。"
            "PAC や除外リストも含めて完全に元へ戻すにはアプリを再起動してください。",
            detected["host"], detected["port"],
        )
    else:
        proxy = QNetworkProxy()
        proxy.setType(QNetworkProxy.ProxyType.NoProxy)
        QNetworkProxy.setApplicationProxy(proxy)
        logger.warning(
            "システムのプロキシ設定を検出できませんでした。直接接続に切り替えます。"
        )


def _set_qt_http_proxy(QNetworkProxy, host: str, port) -> None:
    proxy = QNetworkProxy()
    proxy.setType(QNetworkProxy.ProxyType.HttpProxy)
    proxy.setHostName(host)
    proxy.setPort(int(port))
    user, password = credentials(host)
    if user:
        proxy.setUser(user)
        proxy.setPassword(password)
    QNetworkProxy.setApplicationProxy(proxy)


def install_proxy_authentication(page, on_missing=None) -> None:
    """QWebEnginePage にプロキシ認証の応答を仕込みます。

    Chromium はプロキシから 407 を返されると proxyAuthenticationRequired を
    発行します。ここに応答しないとページが開けません
    (アプリ内のミニブラウザだけ真っ白になる原因)。

    on_missing: 資格情報が未設定だったときに呼ばれるコールバック。
    """

    def handler(url, authenticator, proxy_host):
        # proxy_host は Chromium が「実際に 407 を返してきたプロキシ」として
        # 教えてくれる値なので、これを host として渡せば、環境変数のプロキシと
        # 違うプロキシに社内資格情報を渡してしまうことを防げる。
        user, password = credentials(proxy_host)
        if not user:
            logger.error(
                "プロキシ %s が認証を要求しましたが、資格情報が未設定です。", proxy_host
            )
            if on_missing:
                on_missing(proxy_host)
            return
        logger.info("プロキシ %s の認証情報を送信します (user=%s)", proxy_host, user)
        authenticator.setUser(user)
        authenticator.setPassword(password)

    page.proxyAuthenticationRequired.connect(handler)
