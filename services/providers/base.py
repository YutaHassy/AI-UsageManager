"""使用状況を取得する「プロバイダ」の共通土台。

Claude.ai の利用枠だけでなく、他サービスの枠や API の課金額も同じ画面に
並べたいので、「1つの取得先から複数の指標を持ち帰る」形に一般化しています。

指標には性質の違う3種類があり、UI は kind を見て表示方法を切り替えます:

  percent … 利用枠の消費率 (0-100%)。リセット時刻を持ち、ゲージで出せる。
  money   … 課金額。上限が無いのでゲージにはできず、金額そのものを出す。
  amount  … 残クレジットなどの数量。総量が分かるときだけゲージにもできる。

「利用率」だけを前提にすると課金額を後から載せられなくなるため、
最初からこの3種類を扱える形にしてあります。
"""

import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlsplit

from services import proxy_manager
from services.i18n import t

logger = logging.getLogger(__name__)

# ---------------- requests / urllib3 の遅延読み込み ----------------
#
# requests / urllib3 は「使うときに」読み込みます。
#
# **cli.py は ready を出すまで一度も HTTP を使いません** (最初の通信は
# fetch_usage 時)。それなのにモジュール先頭で import していたため、
# ready までの 332 ms のうち 144 ms (43%) をここで使っていました。経路は
# cli.py -> models/account.py -> services/providers/__init__.py -> ここ、です。
#
# **モジュール先頭の import を消すだけでは 1 ms も速くなりません。**
# services/providers/__init__.py は _ORDER / _RETIRED でプロバイダを
# **import 時に7個インスタンス化します**。各プロバイダの __init__ は
# HttpClient を作り、HttpClient は requests.Session() を作っていました。
# つまり「モジュール先頭の import」を消しても、その数行あとの
# ClaudeProvider() が同じ import を呼び戻すだけです。
# **効かせるには Session の生成そのものを遅らせる必要があります**
# (下の HttpClient.session プロパティ)。実測で確認済み: 先頭の import を
# 消しただけの版では -X importtime に requests がそのまま残りました。
#
# **「入っているか」の点検はこれとは別に残っています。** cli.py の preflight()
# が importlib.util.find_spec で見ており、あちらは import しません。requests が
# 無い利用者への案内 (pip のコマンド付き) はこれまでどおり出ます。
#
# 先例として proxy_manager._bypasses_proxy() も
# `from requests.utils import should_bypass_proxies` を関数内で行っています。
# やり方はそちらに揃えてあります。
requests = None
urllib3 = None


def _load_http_modules() -> None:
    """requests / urllib3 をこのモジュールのグローバルへ束縛します。

    呼ぶのは下の3箇所だけです:

      resolve_verify()        … urllib3.disable_warnings を使うため
      HttpClient.session      … requests.Session を作るため
      HttpClient._request()   … except 節が requests.exceptions を見るため

    2回目以降は sys.modules から返るだけなので、繰り返し呼んでも実質無料です
    (毎リクエスト通る resolve_verify や _request から呼んでも問題になりません)。
    """
    global requests, urllib3

    if requests is not None and urllib3 is not None:
        return

    import requests as _requests
    import urllib3 as _urllib3

    requests = _requests
    urllib3 = _urllib3

# TLS 検証を明示的に無効化したい場合に立てる環境変数 (社内プロキシによる SSL 傍受対策)。
# 名前はアプリ名に由来するもので Claude 固有ではないため、共通側に置いています。
ENV_INSECURE_SSL = "AI_USAGE_MANAGER_INSECURE_SSL"
# アプリ名変更前の環境変数名。既存の設定を壊さないようフォールバックとして残す。
LEGACY_ENV_INSECURE_SSL = "CLAUDE_USAGE_MANAGER_INSECURE_SSL"

# 検証を無効化した場合に大量に出る警告を抑止する (無効化した場合のみ抑止する)
_WARNINGS_DISABLED = False

# ---- 認証方式 ----
AUTH_COOKIE = "cookie"   # 専用ブラウザプロファイルでログインし Cookie を回収する
AUTH_TOKEN = "token"     # API キー等を手で貼り付ける
AUTH_OAUTH = "oauth"     # 別ツールが保持している OAuth 資格情報を借りる

# ---- 指標の種類 ----
PERCENT = "percent"
MONEY = "money"
AMOUNT = "amount"

# ログやトレースバックに残す前に伏せる秘密のパターン。
# 取得先が増えるほど「うっかりログに出る」経路が増えるので、
# プロバイダ個別ではなくここでまとめて潰します。
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),          # Anthropic (sessionKey / APIキー)
    re.compile(r"sk-(?:admin-|proj-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI 系
    re.compile(r"ya29\.[A-Za-z0-9_\-]+"),           # Google の OAuth アクセストークン
    re.compile(r"ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"),  # JWT
)


class UsageError(Exception):
    """取得の失敗。

    auth_error が True の場合は資格情報の失効・無効を意味し、
    再ログインや再入力で回復できる可能性があります (通信エラー等とは区別する)。
    """

    def __init__(self, message: str, auth_error: bool = False):
        super().__init__(message)
        self.auth_error = auth_error


def redact(text: str) -> str:
    """ログ出力用に、秘密らしき文字列を伏せます。"""
    result = text or ""
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("***REDACTED***", result)
    return result


# Cloudflare がボット判定でリクエストを止めたときに付けてくる応答ヘッダ。
#
# **これが付いた 403 は資格情報の問題ではありません。** 見分けないと、
# ボットとして弾かれただけの利用者の画面に「要再ログイン」が出て、
# 何度貼り直しても直らないものを貼り直し続けることになります
# (chatgpt.com は現に User-Agent を見て 403 を返します)。
#
# **判定はこのヘッダ1つに絞ってください。** 「Content-Type が text/html なら
# ボット判定」としてはいけません。Cookie が本当に失効したときにも
# 403 + HTML のエラーページが返りうるので、それを auth_error=False にすると
# 今度は正しい「要再ログイン」を落とします (誤診の向きが変わるだけです)。
CF_MITIGATED = "cf-mitigated"


def server_reason(response) -> str:
    """応答本文にサーバが書いた失敗理由があれば取り出します。

    **401 の本文を捨てないための関数です。** 以前はステータスだけを見て
    「認証エラー (401): Cookie が無効か期限切れです」と決め打ちしていました。
    しかし chatgpt.com の backend-api が 401 に載せてくる理由は一種類では
    ありません。

        "Could not parse your authentication token."      → 貼り直しで直る
        "Workspace is not authorized in this region."     → 貼り直しても直らない

    どちらも画面には同じ文言で出ていたため、直し方が正反対なのに利用者にも
    ログにも区別が残りませんでした。

    **取り出すのは message と code だけです。** 本文を丸ごと載せてはいけません。
    上の redact は既知のパターンしか伏せないので、まだ知らない形式の秘密が
    そのまま画面とログに出ます。
    """
    try:
        data = response.json()
    except ValueError:
        # 本文が JSON でない (HTML のエラーページ等)。理由は取り出せません。
        # なお backend-api の 401 は Content-Type が text/plain のくせに
        # 本文は JSON です。requests の .json() は Content-Type を見ないので
        # ここで読めます — Content-Type で分岐を足すと、この経路が死にます。
        return ""
    if not isinstance(data, dict):
        return ""

    error = data.get("error")
    if isinstance(error, str):
        message, code = error, ""
    elif isinstance(error, dict):
        message, code = error.get("message"), error.get("code")
    else:
        message = data.get("detail") or data.get("message")
        code = data.get("code")

    message = redact(message.strip())[:300] if isinstance(message, str) else ""
    code = redact(code.strip())[:60] if isinstance(code, str) else ""

    if message and code:
        return f"{message} ({code})"
    return message or code


def insecure_ssl_enabled() -> bool:
    """TLS 検証の無効化が環境変数で明示的に許可されているかを返します。"""
    for name in (ENV_INSECURE_SSL, LEGACY_ENV_INSECURE_SSL):
        if os.environ.get(name, "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def resolve_verify():
    """TLS 検証の設定を解決します。

    既定は検証あり。社内プロキシ等で自己署名 CA を挟んでいる場合は、
    REQUESTS_CA_BUNDLE に CA 証明書を指定するのが本来の対処です。
    どうしても検証を切る必要がある場合のみ、環境変数で明示的に無効化します。
    """
    global _WARNINGS_DISABLED

    # 下の disable_warnings で urllib3 を使います。ここは毎リクエスト通りますが、
    # 2回目以降は束縛済みかどうかを見るだけです。
    _load_http_modules()

    if insecure_ssl_enabled():
        if not _WARNINGS_DISABLED:
            logger.warning(
                "%s が設定されているため TLS 証明書の検証を無効化します。"
                "通信内容 (セッション Cookie や API キーを含む) が傍受される可能性があります。",
                ENV_INSECURE_SSL,
            )
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            _WARNINGS_DISABLED = True
        return False

    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle and os.path.exists(ca_bundle):
        return ca_bundle

    return True


class HttpClient:
    """プロキシ・TLS・エラー分類をまとめた GET 専用の薄いラッパ。

    どの取得先も同じ社内プロキシを通り、同じように「資格情報の失効」を
    他の失敗と区別する必要があるため、その扱いをここに集約します。

    credential_label は 401/403 のメッセージに差し込む語です。
    Cookie 認証なら「Cookie」、APIキー認証なら「Admin API キー」のように
    プロバイダ側から渡してもらい、利用者が何を直せばよいか分かるようにします。
    """

    # ログイン画面 (QtWebEngine) と同じ Chromium 版を名乗ります。
    # ここだけ古いままにすると、Cookie を取ったブラウザと Cookie を使う
    # リクエストで UA が食い違います。Qt を上げたら
    # services/browser_profile.FALLBACK_CHROME_UA と揃えて更新してください。
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )

    def __init__(self, credential_label: str = "credential", timeout: int = 15,
                 user_agent: str = None):
        # **ここは訳す前の原文 (英語) を持ちます。** 呼び出し元は
        # UsageProvider.credential_label をそのまま渡してきますが、あれも
        # 原文です。訳すのは文へ差し込む _request / _as_json の側です
        # (「値を作る瞬間に訳す」— services/i18n.py の docstring を参照)。
        self.credential_label = credential_label
        self.timeout = timeout
        self.user_agent = user_agent or self.DEFAULT_UA

        # **ここで requests.Session() を作らないでください。**
        # services/providers/__init__.py は import 時に全プロバイダを
        # インスタンス化し、各プロバイダの __init__ が HttpClient を作ります。
        # つまり __init__ で Session を作ると、起動しただけで requests が
        # 読み込まれ、ready までの待ち時間に 144 ms 乗ります (上の解説を参照)。
        # 実際に通信する時まで遅らせるために、生成は session プロパティに任せます。
        self._session = None
        # 生成の競合よけ。cli.py の取得は1本のワーカースレッドで直列化されて
        # いるので現状ぶつかりませんが、__init__ で作っていた頃と違い生成の
        # 時刻が「最初のリクエスト」に移るため、二重生成 (= Cookie を持った
        # Session が捨てられる) が起きない形にしておきます。
        self._session_lock = threading.Lock()
        # いま cookiejar に載っている資格情報の指紋。_stash_cookies が使います。
        # 生の Cookie を属性で持ち歩くと、例外表示やデバッガに出てしまうため
        # ハッシュにしておきます (gemini._rotation_key と同じ考え方)。
        self._cookie_fingerprint = None

    @property
    def session(self):
        """requests.Session。最初に使うときに作ります。

        **読み取り専用です。** 差し替えたい場合は _session を触るのではなく、
        HttpClient を作り直してください。
        """
        if self._session is None:
            with self._session_lock:
                if self._session is None:
                    _load_http_modules()
                    self._session = requests.Session()
        return self._session

    def _stash_cookies(self, cookie_header: str, url: str):
        """Cookie ヘッダの中身を、宛先ホスト限定で cookiejar へ移します。

        **requests はリダイレクトを追うとき、手で設定された Cookie ヘッダを
        捨てます** (Session.resolve_redirects が `del headers["Cookie"]` して
        から cookiejar で組み直すため)。つまりヘッダだけで渡していると、
        302 が1つ挟まった時点で認証 Cookie が消えます。

        実際 Gemini がこれで壊れました (2026-08)。gemini.google.com/usage が
        Google の bot 判定画面 (www.google.com/sorry/) へ 302 で飛ばすように
        なり、戻ってきた最後の /usage には GOOGLE_ABUSE_EXEMPTION だけが付いて、
        **サインアウト状態の HTML が 200 で返っていました。**

        この壊れ方は遠くまで届きます。**401 が返らないので認証エラーとして
        扱われません。** 実際に見えていた症状は「使用量ページから認証トークン
        (SNlM0e) を取り出せない」で、そこから「Cookie が失効したのだろう」と
        読めてしまいます。しかし Cookie は生きていました
        (RotateCookies は 200 を返していた)。

        入れる範囲は**宛先ホストだけ**に絞ります。`.google.com` のように
        広げればブラウザと同じ挙動になりますが、そうすると www.google.com の
        bot 判定画面にまで資格情報が付いて回ります。実測ではホスト限定でも
        取得できたので、広げる理由がありません。

        **jar を空にするのは、渡された資格情報が変わったときだけです。**
        ここには2つの相反する要求があります。

          捨てたい: プロバイダは services/providers/__init__.py が import 時に
            1つだけ作り、同じ HttpClient を全アカウントで使い回します。前の
            アカウントの Cookie を残すと、名前がぶつからないものだけが次の
            アカウントの通信に紛れ込みます (Claude を3アカウント登録している
            場合がこれ)。

          残したい: **1回の取得の中でサーバが付けてくる Cookie は引き継が
            なければなりません。** Gemini は使用量ページを開いた時点で
            GOOGLE_ABUSE_EXEMPTION / NID / __Secure-3PSID などを返し、
            続く batchexecute はそれを前提にしています。毎回空にすると
            **この POST だけが 400 で弾かれます** (実測。ページ取得は 200 な
            ので、「トークンは取れたのに枠だけ取れない」という形で出ます)。

        資格情報の指紋で見分ければ両方を満たせます。同じアカウントを続けて
        叩いている間は jar を育て、別のアカウントに移った瞬間に捨てます。
        """
        session = self.session
        fingerprint = hashlib.sha256(cookie_header.encode("utf-8")).hexdigest()
        if fingerprint == self._cookie_fingerprint:
            # 同じ資格情報の続き。サーバが足した Cookie ごと jar を活かす。
            return

        session.cookies.clear()
        self._cookie_fingerprint = fingerprint
        host = urlsplit(url).hostname or ""
        for name, value in parse_cookie_header(cookie_header).items():
            session.cookies.set(name, value, domain=host, path="/")

    def _request(self, method: str, url: str, headers: Dict[str, str], what: str,
                 params: Dict[str, Any] = None, json_body: Any = None,
                 data: Any = None):
        """共通のリクエスト処理。エラーは必ず UsageError に包んで返します。

        GET だけでなく POST も通すのは、取得先によって形式が違うためです
        (Google の Code Assist API のように POST で情報を返すものがある)。
        """
        logger.debug("%s を取得します: %s %s", what, method, url)

        # **下の except 節が requests を参照できるのは、ここで束縛するからです。**
        # self.session プロパティ経由でも必ず束縛されますが、それに頼ると
        # 「session の作り方を変えたら except 節が NameError になる」という
        # 遠く離れた壊れ方をします。この1行で、この関数だけを見て安全だと
        # 分かるようにしておきます (2回目以降は None 判定だけで戻ります)。
        _load_http_modules()

        # Cookie はヘッダのまま送らず cookiejar へ移します。ヘッダのままだと
        # リダイレクトを1つ挟んだだけで消えます (理由は _stash_cookies を参照)。
        # 呼び出し元の辞書は書き換えません (使い回されることがあるため)。
        headers = dict(headers or {})
        cookie_key = next((k for k in headers if k.lower() == "cookie"), None)
        if cookie_key is not None:
            self._stash_cookies(headers.pop(cookie_key), url)

        try:
            # proxies が None のときは requests が環境変数 HTTP_PROXY/HTTPS_PROXY を使う。
            # url を渡すのは NO_PROXY を尊重させるため (社内ホスト向けの通信を
            # 社内プロキシへ投げてしまわないようにする)。
            response = self.session.request(
                method, url, headers=headers, params=params, json=json_body, data=data,
                timeout=self.timeout,
                verify=resolve_verify(), proxies=proxy_manager.requests_proxies(url),
            )
        except requests.exceptions.ProxyError as e:
            raise UsageError(t(
                "Proxy error: {reason}\n"
                "Proxy: {proxy}\n"
                "Check the proxy user name and password. The password is "
                "kept separately from the other settings, encrypted.",
                reason=e, proxy=proxy_manager.redacted_proxy_url(),
            )) from e
        except requests.exceptions.SSLError as e:
            raise UsageError(t(
                "SSL error: {reason}\n"
                "Behind a corporate proxy, point REQUESTS_CA_BUNDLE at the CA "
                "certificate. The value comes from the environment this "
                "process was started with, so set it first and then start "
                "the editor again — setting it while the editor is running "
                "does not reach here, not even after restarting the backend.",
                reason=e,
            )) from e
        except requests.exceptions.Timeout as e:
            raise UsageError(t(
                "Timed out (no response within {seconds} seconds).",
                seconds=self.timeout,
            )) from e
        except requests.RequestException as e:
            raise UsageError(t("Network error: {reason}", reason=e)) from e

        logger.debug("%s ステータス: %s", what, response.status_code)
        logger.debug("%s レスポンス: %s", what, redact(response.text[:200]))
        # 切り分けに要るのはこの3つです。「資格情報が切れた」のか
        # 「ボットとして弾かれた」のかは、本文よりこちらを見るほうが早く
        # 分かります (次に同じ疑いが出たとき実測をやり直さずに済みます)。
        logger.debug("%s ヘッダ: cf-mitigated=%s cf-ray=%s content-type=%s", what,
                     response.headers.get(CF_MITIGATED),
                     response.headers.get("cf-ray"),
                     response.headers.get("content-type"))

        # **ボット判定を認証エラーとして扱わないこと。** 詳細は CF_MITIGATED の
        # 説明を参照。ステータス分岐より前に1箇所だけ置いて、403 だけでなく
        # 429 / 503 に乗ってきた場合もまとめて拾います。
        # 401 は除きます — 認証の失敗であることに変わりはないためです。
        if response.status_code != 401 and CF_MITIGATED in response.headers:
            raise UsageError(t(
                "Blocked by the site's bot protection ({status}). "
                "This is not a problem with {credential}.\n"
                "Wait a little and try again. CF-RAY: {ray}",
                status=response.status_code, credential=t(self.credential_label),
                ray=response.headers.get("cf-ray") or t("(none)"),
            ))

        if response.status_code in (401, 403):
            message = t(
                "Authentication error ({status}): {credential} is invalid or expired.",
                status=response.status_code, credential=t(self.credential_label))
            # **本文にサーバが書いた理由があるなら必ず添えること。**
            # ここを捨てていたせいで、直し方の違う複数の 401 が
            # すべて同じ「要再ログイン」に潰れていました (server_reason 参照)。
            reason = server_reason(response)
            if reason:
                # **差し込み名に message は使えません。** t() の第1引数が
                # message なので、キーワードがぶつかって TypeError になります。
                message = t("{detail}\nThe server said: {reason}",
                            detail=message, reason=reason)
            raise UsageError(message, auth_error=True)
        if response.status_code == 407:
            raise UsageError(t(
                "Proxy authentication error (407): "
                "the proxy needs a user name and password.\n"
                "Proxy: {proxy}\n"
                "The user name is a setting; the password has its own "
                "entry point, because it is stored encrypted.",
                proxy=proxy_manager.redacted_proxy_url(),
            ))
        if response.status_code == 429:
            raise UsageError(t("Rate limited (429): try again later."))
        if response.status_code != 200:
            raise UsageError(t("HTTP error ({status}): could not fetch {what}.",
                               status=response.status_code, what=what))

        return response

    def _as_json(self, response, what: str) -> Any:
        try:
            return response.json()
        except ValueError as e:
            # ログイン画面の HTML が返ってくるケースなど
            raise UsageError(
                t("The response for {what} could not be read as JSON. "
                  "{credential} may be invalid.",
                  what=what, credential=t(self.credential_label)),
                auth_error=True,
            ) from e

    def get_json(self, url: str, headers: Dict[str, str], what: str,
                 params: Dict[str, Any] = None) -> Any:
        return self._as_json(self._request("GET", url, headers, what, params), what)

    def post_json(self, url: str, headers: Dict[str, str], what: str,
                  json_body: Any = None, params: Dict[str, Any] = None,
                  data: Any = None) -> Any:
        """POST で JSON を取得します。

        data はフォームエンコードで送りたい場合に使います
        (OAuth のトークンエンドポイントは JSON ではなくフォームを要求する)。
        """
        response = self._request("POST", url, headers, what, params,
                                 json_body=json_body, data=data)
        return self._as_json(response, what)

    def post_response(self, url: str, headers: Dict[str, str], what: str,
                      data: Any = None, params: Dict[str, Any] = None):
        """応答オブジェクトそのものを返します。

        本文ではなく Set-Cookie を読みたい場合に使います
        (Cookie の更新エンドポイントは、新しい値を本文ではなくヘッダで返す)。
        """
        return self._request("POST", url, headers, what, params, data=data)

    def get_text(self, url: str, headers: Dict[str, str], what: str,
                 params: Dict[str, Any] = None) -> str:
        """本文をテキストとして返します (JSON ではない表形式を返す取得先向け)。"""
        response = self._request("GET", url, headers, what, params)
        # 文字コード指定が無いと requests は ISO-8859-1 を仮定してしまう
        if not response.encoding:
            response.encoding = "utf-8"
        return response.text

    def post_text(self, url: str, headers: Dict[str, str], what: str,
                  data: Any = None, params: Dict[str, Any] = None) -> str:
        """POST の応答をテキストとして返します。

        Google の batchexecute のように、JSON ではない独自の枠に
        JSON を包んで返す取得先向けです (response.json() では読めません)。
        """
        response = self._request("POST", url, headers, what, params, data=data)
        if not response.encoding:
            response.encoding = "utf-8"
        return response.text


# ---------------- 貼り付けられた Cookie の解釈 ----------------
#
# **複数のプロバイダが同じものを必要とします。** Cookie 認証の取得先は
# どれも「開発者ツールから持ち出した内容を丸ごと貼ってもらう」経路を
# 持っており、その貼り方 (cURL / Copy value / -b) は取得先によらず同じです。
# 取り出し方をプロバイダごとに書くと、Windows の cmd 形式 (^") のような
# 見落としやすい形が片方にだけ実装された状態になります。


def extract_cookie_header(text: str) -> str:
    """貼り付けられた内容から、Cookie ヘッダの部分だけを取り出します。

    DevTools からの持ち出し方は1つではありません:

      「Copy as cURL」  → curl ... -H 'cookie: name=value; ...' の形
      (PowerShell 形式) → curl ... -H "cookie: name=value; ..." の形
      (cmd 形式)        → curl ... -b ^"name=value; ...^" の形
      「Copy value」    → name=value; name=value がそのまま
      -b / --cookie     → curl ... -b 'name=value; ...' の形

    Windows の Chrome / Edge には「Copy as cURL (cmd)」があり、
    引用符が ^" という形で escape されます。この ^ を見落とすと
    -b の値を取り出せず、貼り付けの先頭 (curl ... -b ^) が
    そのまま Cookie 名に食い込みます。

    利用者に正しい持ち出し方を1つだけ覚えてもらうのは無理があるので、
    どれで来ても通るようにします。どれにも当てはまらなければ、
    貼られたものをそのまま返して validate_credential に説明させます。
    """
    text = (text or "").strip()

    # "cookie: ..." を含む形 (Copy as cURL / Copy value のヘッダ名つき)
    match = re.search(r"""cookie\s*:\s*(["']?)(?P<value>[^"'\r\n]+)""", text, re.IGNORECASE)
    if match:
        return match.group("value").strip()

    # curl の -b / --cookie で渡す形 (cmd 形式の ^" にも合わせる)
    match = re.search(r"""(?:^|\s)(?:-b|--cookie)\s+\^?(["'])(?P<value>.*?)\^?\1""",
                      text, re.DOTALL)
    if match:
        return match.group("value").strip()

    return text


def parse_cookie_header(value: str) -> Dict[str, str]:
    """"name=value; name=value" を辞書にします。"""
    jar = {}
    for part in (value or "").split(";"):
        name, sep, val = part.partition("=")
        name = name.strip()
        if sep and name:
            jar[name] = val.strip()
    return jar


# ---------------- 時刻 ----------------

def unix_to_iso(seconds) -> str:
    """Unix 秒を ISO8601(UTC) 文字列にします。読めなければ None。

    リセット時刻を Unix 秒で返す取得先が複数あり (Codex / ChatGPT / Gemini)、
    画面側はどれも ISO8601 を前提にしているため、変換をここに集約します。
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        logger.warning("Unix 秒を日時に変換できませんでした: %r", seconds)
        return None


# ---------------- 指標の組み立て ----------------

def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def percent_metric(key: str, label: str, utilization: float,
                   resets_at: str = None) -> Dict[str, Any]:
    """利用枠の消費率 (0-100%)。"""
    util = _clamp_percent(utilization)
    return {
        "key": key,
        "label": label,
        "kind": PERCENT,
        "value": util,
        "utilization": util,
        "display": f"{util:.1f}%",
        "resets_at": resets_at,
    }


def currency_symbol(currency: str) -> str:
    """通貨記号。未知の通貨では空文字を返します (コードを併記する側で使う)。"""
    return {"USD": "$", "JPY": "¥"}.get(currency, "")


def format_money(amount: float, currency: str = "USD") -> str:
    symbol = currency_symbol(currency)
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def money_metric(key: str, label: str, amount: float, currency: str = "USD",
                 period: str = None, budget: float = None) -> Dict[str, Any]:
    """課金額。

    budget を渡すと予算に対する消費率としてゲージにも出せます。
    渡さない場合は utilization が None になり、UI は金額だけを表示します
    (上限の無い金額を 0-100% のゲージに載せると嘘になるため)。

    **表示に消費率 (%) は入れません。** 予算は利用者が決めた任意の値なので、
    「45%」だけを見せても元の金額が分からず判断材料になりません。
    金額 / 上限 の形で、常に実額が読めるようにします
    (amount_metric の「200 / 1,000」と同じ考え方)。
    """
    display = format_money(amount, currency)
    if budget and budget > 0:
        display = f"{display} / {format_money(budget, currency)}"
    if period:
        display = f"{display} / {period}"

    utilization = None
    if budget and budget > 0:
        utilization = _clamp_percent(amount / budget * 100.0)

    return {
        "key": key,
        "label": label,
        "kind": MONEY,
        "value": float(amount),
        "utilization": utilization,
        "display": display,
        "currency": currency,
        "period": period,
        "budget": budget,
        "resets_at": None,
    }


def amount_metric(key: str, label: str, value: float, total: float = None,
                  unit: str = "", resets_at: str = None) -> Dict[str, Any]:
    """残クレジットなどの数量。

    value は「残っている量」です。total が分かる場合のみ、
    消費率 (= 1 - 残/総量) をゲージに出します。
    """
    display = f"{value:,.0f}{unit}"
    utilization = None
    if total and total > 0:
        display = f"{value:,.0f} / {total:,.0f}{unit}"
        utilization = _clamp_percent((total - value) / total * 100.0)

    return {
        "key": key,
        "label": label,
        "kind": AMOUNT,
        "value": float(value),
        "utilization": utilization,
        "display": display,
        "total": total,
        "unit": unit,
        "resets_at": resets_at,
    }


def build_result(metrics: List[Dict[str, Any]], **extra) -> Dict[str, Any]:
    """プロバイダの戻り値を組み立てます。

    max_utilization は「最も逼迫している枠」で、一覧の要約とステータス判定に
    使います。ゲージに出せる指標が1つも無い (課金額だけ等) 場合は None にします。
    ここで 0.0 を返すと「まだ余裕がある」と誤解させるためです。
    """
    gauged = [m["utilization"] for m in metrics if m.get("utilization") is not None]
    result = {
        "metrics": metrics,
        "max_utilization": max(gauged) if gauged else None,
    }
    result.update(extra)
    return result


# 上限金額を適用する対象の指標キー。課金系の取得先はどれも合計を "total" で返します。
# モデル別の内訳には適用しません (内訳ごとに上限があるわけではないため)。
TOTAL_KEY = "total"


def apply_budget(result: Dict[str, Any], budget: float,
                 metric_key: str = TOTAL_KEY) -> Dict[str, Any]:
    """取得済みの結果に、利用者が設定した上限金額を反映します。

    **fetch_usage には手を入れません。** 予算はアカウント側の設定であって
    取得先の応答ではないので、8つあるプロバイダ全部のシグネチャに
    無関係な引数を足すより、取得後にここで被せる方が影響が小さく済みます。
    Qt に依存しない純粋関数なので、そのまま単体テストできます。

    元の dict は書き換えず、新しい dict を返します。呼び出し元
    (GUI スレッド) とワーカーが同じ辞書を触らないようにするためです。
    """
    if not budget or budget <= 0:
        return result

    metrics = result.get("metrics") or []
    updated = []
    changed = False
    for metric in metrics:
        if metric.get("kind") == MONEY and metric.get("key") == metric_key:
            updated.append(money_metric(
                metric["key"], metric["label"], metric["value"],
                currency=metric.get("currency") or "USD",
                period=metric.get("period"),
                budget=budget,
            ))
            changed = True
        else:
            updated.append(metric)

    if not changed:
        return result

    merged = dict(result)
    merged.update(build_result(updated))
    return merged


class UsageProvider:
    """1つの取得先を表します。

    UI はこのクラスのメタデータだけを見てログイン画面や入力欄を組み立てます。
    サービス固有の URL や Cookie 名を UI 側へ書かないでください
    (書くと取得先を増やすたびに UI を触ることになります)。
    """

    id = ""
    label = ""
    description = ""            # ダイアログに出す1行説明
    auth_kind = AUTH_COOKIE

    # 資格情報の入力・保存まわり
    credential_label = "Cookie"
    credential_hint = ""        # 手入力欄のプレースホルダ
    credential_marker = ""      # 貼り付け内容の妥当性を疑うための目印

    # AUTH_COOKIE のときに使う
    login_url = ""              # ログイン画面で最初に開く URL
    home_url = ""               # セッションの生存確認で読み込む URL
    cookie_domain = ""          # 回収対象とする Cookie のドメイン
    session_cookie_name = ""    # これが取れたらログイン成功とみなす

    # 取得先ごとに1つだけ持てる追加フィールド。
    # Claude では Organization ID、社内ゲートウェイではエンドポイント URL のように
    # 意味が違うので、ラベルと検証はプロバイダ側で決めます
    # (設定ファイル上のキー名は互換のため organization_id のまま)。
    uses_extra_field = False
    extra_field_label = "Organization ID"
    extra_field_hint = ""
    extra_field_default = ""

    # 課金系の取得先だけが使う「上限金額」。True にすると入力欄が出て、
    # 入力された額に対する消費率がゲージに出ます。
    #
    # currency は**入力欄の記号表示のためだけ**に使います。指標そのものの
    # 通貨は応答の解釈時に決まる (Anthropic は応答の currency を読む) ので、
    # そちらには影響させないでください。食い違った場合は、応答側の
    # 複数通貨ガードが先に例外を投げます。
    supports_budget = False
    currency = "USD"
    # 新規追加時に入れておく値。利用者は上書きするのが前提なので、
    # 「よくある額」を入れておいて直接打ち替えてもらいます。
    # 0 にすると入力欄が空に見えて、何を入れる欄なのか伝わりません。
    default_budget = 10000.0

    # アプリ内ブラウザでログインできないときの逃げ道。
    #
    # 認証側が埋め込みブラウザからのサインインを拒否することがあります
    # (Google は「このブラウザまたはアプリは安全でない可能性があります」で弾く)。
    # これは埋め込み側アプリがパスワード入力を盗み見できるという理由で
    # 存在する保護なので、偽装して通そうとせず、利用者に普段お使いの
    # ブラウザで値を取ってきてもらいます。
    #
    # manual_url を設定すると、ログイン画面に救済ボタンが出ます。
    manual_url = ""             # 既定ブラウザで開く URL
    manual_steps = ""           # そこで何をするかの手順 (1行1手順)

    # 取得処理がまだ無いものは False。一覧に出しても自動更新の対象から外します。
    implemented = True

    @property
    def has_manual_fallback(self) -> bool:
        return bool(self.manual_url)

    def normalize_credential(self, value: str) -> str:
        """貼り付けられた内容を、保存する形に整えます。

        利用者に「どこがトークンか」を目視で探させると、隣の項目を掴んだり
        引用符やカンマを巻き込んだりして必ず失敗します。画面に出たものを
        丸ごと貼ってもらい、必要な部分の抽出はここで引き受けてください。

        **余計なものを保存しないこと。** 貼り付け元には認証に不要な個人情報
        (メールアドレス等) や別の資格情報が混ざっていることがあります。
        """
        return (value or "").strip()

    def cookies_for_profile(self, pasted: str) -> str:
        """貼り付けられた内容から、ブラウザプロファイルへ預ける Cookie を取り出します。

        normalize_credential が「設定ファイルに残す最小限」を返すのに対し、
        こちらは**セッションの維持に要るもの全部**を返します。取得には使わないが
        更新の際に要る Cookie (Google の SID / SAPISID など) があるためです。

        預け先はアカウント専用のブラウザプロファイルで、設定ファイルには
        入りません。以降のセッション維持は Chromium がまとめて面倒を見ます。

        Cookie 認証を使わない取得先では空を返してください (預けるものが無い)。
        """
        return "" if self.auth_kind != AUTH_COOKIE else (pasted or "").strip()

    def validate_paste(self, pasted: str) -> str:
        """**貼り付けられた内容そのもの**に不安があるとき、確認メッセージを返します。

        validate_credential は normalize_credential を通した**あと**の値を
        受け取ります。つまり抽出の過程で捨てたものは、あちらでは見えません。
        貼り付け元が「この情報は今おかしい」と伝えてきている場合
        (ChatGPT の /api/auth/session が返す error など) は、
        捨てる前のここで拾ってください。

        問題なければ空文字を返してください。
        """
        return ""

    def validate_credential(self, value: str) -> str:
        """貼り付けられた資格情報に不安があるとき、確認メッセージを返します。

        問題なければ空文字を返してください。**ここで弾き切ろうとしないこと。**
        値が有効かを決めるのは取得先のサーバーであって、このアプリではありません。
        明らかに形が違うもの (取り違え・コピー漏れ) だけを拾う用途です。
        """
        return ""

    def validate_extra(self, value: str) -> str:
        """追加フィールドの値に不安があるとき、確認メッセージを返します。

        問題なければ空文字を返してください。ここで書式を判定させることで、
        取得先ごとの事情 (UUID なのか URL なのか) を UI から追い出します。
        """
        return ""

    def is_login_page(self, url: str) -> bool:
        """その URL がログイン画面かどうか。

        セッションの自動更新で「まだログインしていない」を判定するのに使います。
        """
        return "/login" in (url or "")

    def owns_cookie_domain(self, domain: str) -> bool:
        return bool(self.cookie_domain) and self.cookie_domain in (domain or "")

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        """使用状況を取得します。戻り値は build_result() の形。

        呼び出し元 (ワーカースレッド) と GUI スレッドが同じ Account を
        同時に触るとデータレースになるため、引数は値で受け取り、
        解決した Organization ID 等は戻り値に載せて返してください。
        """
        raise NotImplementedError

    def not_implemented(self, reason: str) -> UsageError:
        """未実装のプロバイダが返す共通のエラー。"""
        return UsageError(t(
            "Fetching usage for {provider} is not implemented yet.\n{reason}",
            provider=self.label, reason=reason,
        ))
