"""ChatGPT (ブラウザ版) の利用上限。

DevTools の Network タブで実際の通信を確認して判明した経路です:

  1. GET https://chatgpt.com/api/auth/session   (Cookie 認証)
     NextAuth のセッション確認エンドポイント。レスポンスの accessToken (JWT) が、
     以降の backend-api 呼び出しで使う Bearer トークンになる。
     ブラウザの fetch() が Authorization ヘッダを明示的に付けていた
     (Cookie を送るだけでは backend-api 側は通らない) ため、この二段階が必要。

  2. GET https://chatgpt.com/backend-api/wham/usage   (Authorization: Bearer <accessToken>)
     実際の利用状況。レスポンス例 (plan_type: "plus" のアカウントで実測):

       {"plan_type": "plus",
        "rate_limit": {"allowed": true, "limit_reached": false,
                        "primary_window": {"used_percent": 41,
                                            "limit_window_seconds": 604800,
                                            "reset_after_seconds": 458225,
                                            "reset_at": 1786431036},
                        "secondary_window": null},
        "credits": {"has_credits": false, "unlimited": false, "balance": "0", ...}}

     primary_window / secondary_window の構造は Codex CLI の rate_limits.primary/secondary
     と同じ形 (used_percent, reset_at)。ただし枠の長さは window_minutes ではなく
     limit_window_seconds (秒) で来る点が違う。

     plan や契約内容によっては secondary_window が使われたり、credits
     (従量の追加クレジット) 側に残高が乗ることもありうるが、実測できていない。
     credits.has_credits が true のときだけ残高を追加の指標として出す。

  Work プランは Codex と使用量管理を共用しているため、Work の場合は
  「Codex CLI」の取得先でも同じ枠が見える (このプロバイダは個人の
  Plus/Pro 等、ChatGPT 単体の枠を対象にしている)。
"""

import base64
import binascii
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict

from services.i18n import t
from services.providers.base import (
    AUTH_COOKIE, HttpClient, UsageError, UsageProvider,
    amount_metric, build_result, percent_metric, unix_to_iso,
)

logger = logging.getLogger(__name__)

SESSION_URL = "https://chatgpt.com/api/auth/session"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# chatgpt.com の前には Cloudflare が居て、**User-Agent を見て 403 を返します**
# (実測: UA が curl や python-requests だと /api/auth/session が 403、
#  Chrome を名乗ると 200)。判定はヘッダの組み合わせも見るので、UA だけ
# Chrome を名乗って他が4つしか無い状態は、その不一致自体が手がかりになります。
# claude.py が付けているものに揃えておきます。
#
# **Sec-Ch-Ua の版は base.HttpClient.DEFAULT_UA と揃えてください。**
# 片方だけ上げると、名乗っている Chrome の版がヘッダ間で食い違います。
_BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# 資格情報を保存し直す間隔。
#
# **NextAuth のセッションはローリングです。** /api/auth/session を呼ぶたびに
# 新しい sessionToken が発行され、有効期限は毎回 90日先へ押し出されます
# (実測 2026-08-19: 応答の Set-Cookie は毎回 2160 時間先)。つまり
# **取得を続けている限り、資格情報は延ばし続けられます。**
#
# **逆に、保存し直さなければ必ず切れます。** 最初に貼った1個をずっと使う
# ことになり、更新の鎖が切れた時点で応答が RefreshAccessTokenError になって
# 「要再ログイン」に落ちます (2026-08 に実際に起きたのがこれ)。
#
# 間引くのは、値が**毎回変わる**からです。JWE は呼ぶたびに暗号化し直されるので
# 「変わったかどうか」では判定できず、素直に書き戻すと取得のたびに設定ファイルへ
# 書くことになります。90日の枠に対して1時間おきで十分すぎます。
RENEW_INTERVAL_SEC = 3600

# 最後に保存し直した時刻 (time.monotonic)。アカウントごとに分けます。
#
# **鍵に保存値そのものを使ってはいけません。** 保存し直すと値が変わるため、
# 次回は別の鍵になり、間引きが一度も効かなくなります (gemini が長命な
# __Secure-1PSID の側を鍵にしているのと同じ理由)。
_last_renewal: Dict[str, float] = {}

# 貼り付けの前後に紛れ込む、目に見えない文字。
# **BOM は str.strip() では落ちません。** 先頭に BOM が1つ付いただけで
# 下の _extract_session_token が JSON を JSON と認識できなくなります。
_INVISIBLE = "\ufeff\u200b\u200c\u200d\u2060"

# limit_window_seconds と表示名の対応 (許容幅 ±20%)。
# 表示名は訳す前の原文 (英語)。訳すのは _window_label です。
_WINDOWS = [
    (5 * 3600, "5-hour"),
    (24 * 3600, "Daily"),
    (7 * 24 * 3600, "Weekly"),
    (30 * 24 * 3600, "Monthly"),
]
_WINDOW_TOLERANCE = 0.2


def _window_label(seconds) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        return t("Rate limit")
    for sec, label in _WINDOWS:
        if abs(seconds - sec) <= sec * _WINDOW_TOLERANCE:
            return t(label)
    if seconds >= 24 * 3600:
        return t("{days}-day window", days=f"{seconds / (24 * 3600):.0f}")
    return t("{hours}-hour window", hours=f"{seconds / 3600:.0f}")


# 途中で切れた貼り付けなど、JSON として読めなかったときの保険。
_TOKEN_FIELD = re.compile(r'"(sessionToken|accessToken)"\s*:\s*"([^"\s]+)"')

# 抽出の優先順。sessionToken が先なのは、これが
# __Secure-next-auth.session-token Cookie の値そのもので、
# これさえ持っていれば accessToken を毎回取り直せるためです
# (accessToken だけだと10日ほどで貼り直しになる)。
_TOKEN_PRIORITY = ("sessionToken", "accessToken")


def _load_session_json(text: str):
    """貼り付けから /api/auth/session の JSON を取り出します。読めなければ None。

    2段構えです。

      1段目: そのまま json.loads する。
      2段目: 最初の "{" から最後の "}" までを切り出して読み直す。
             整形ビューアのラベルやタブ見出しが前後に付いていても通ります。
    """
    text = (text or "").strip().strip(_INVISIBLE).strip()
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    sliced = text[start:end + 1] if 0 <= start < end else None
    for candidate in (text, sliced):
        if candidate is None:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _session_error(data) -> str:
    """/api/auth/session が抱えているエラー名を返します。無ければ空文字。

    **Cookie が生きていてもここにエラーが載ることがあります。** 実測した例:

        {"error": "RefreshAccessTokenError",
         "user": {...}, "expires": "2026-11-17T...",
         "accessToken": <6日前に失効した JWT>}

    NextAuth が accessToken の更新に失敗したという意味で、応答は 200、
    user も expires も正常、**しかし accessToken は失効したままの古いもの**
    が返ります。これを見ずに使うと backend-api から 401 (token_expired) が
    返り、画面には「要再ログイン」とだけ出ます。しかも Cookie 自体は
    生きているので、貼り直しても同じ Cookie なら必ず同じ結果になります
    (ブラウザ側で入り直さないと直りません)。
    """
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    return error.strip() if isinstance(error, str) and error.strip() else ""


def _extract_session_token(text: str):
    """/api/auth/session の出力から (項目名, 値) を取り出します。

    見つからなければ None。

    **入力は「ブラウザの画面を Ctrl+A で丸ごとコピーしたもの」です。**
    利用者に目当ての行を探させない代わりに、ここへは JSON 以外のものが
    いくらでも混ざって届きます (整形ビューアの "Pretty-print" ラベル、
    行番号、タブ見出し、先頭の BOM)。

    **以前はこの関数が `if not text.startswith("{"): return None` で
    始まっていました。** そのため、途中で切れた貼り付けを救うための
    正規表現 (_TOKEN_FIELD) が、先頭に1文字でも余計なものが付いた入力では
    一度も動きませんでした — 救済がいちばん必要な場面でだけ救済が無効に
    なる、という形です。取り出せないと normalize_credential が貼り付け
    全文をそのまま返し、それが資格情報として保存され、あとで必ず
    「要再ログイン」になります。そこで3段構えにします。
    """
    text = (text or "").strip().strip(_INVISIBLE).strip()
    if not text:
        return None

    # 1段目・2段目は _load_session_json に任せる。
    data = _load_session_json(text)
    if data is not None:
        for key in _TOKEN_PRIORITY:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return key, value.strip()
        # JSON として読めたのに目当ての項目が無い = ログインしていない
        # ブラウザの画面など。正規表現へ落としても拾えるものはありません。
        return None

    # 3段目: JSON として読めない (コピーが途中で切れた等)。
    # **先頭が "{" かどうかに関係なく**、形だけを頼りに拾い直します。
    found = {}
    for key, value in _TOKEN_FIELD.findall(text):
        found.setdefault(key, value)
    for key in _TOKEN_PRIORITY:
        if key in found:
            return key, found[key]
    return None


def _looks_like_jwt(value: str) -> bool:
    """accessToken (JWT) を直接貼られたかどうかの判定。

    Cookie は "name=value; name=value" の形なので、区切りの違いで見分きます。
    JWT は "." 区切りの3つの base64url で、先頭は必ず {"alg":... を base64url
    にした "eyJ" になります。
    """
    return (
        value.startswith("eyJ")
        and value.count(".") == 2
        and not any(ch.isspace() for ch in value)
        and "=" not in value.split(".")[0]
    )


def _looks_like_cookie(value: str) -> bool:
    """Cookie ヘッダとして送れる形かどうかの判定。

    **以前はこの判定が `"=" in value` だけでした。** "=" は貼り付けた
    ページのどこにでも入っています — Google アカウントでログインしていると
    user.image が ".../a/XXXX=s96-c" という形で必ず "=" を含みます。
    そのため、抽出に失敗した貼り付け全文が**警告ひとつ出ないまま**
    Cookie として保存され、JSON 丸ごとが Cookie ヘッダに載って送られて
    いました。当然サーバは認証できず、結果は「要再ログイン」です。

    見るのは形だけです (中身が有効かどうかを決めるのは OpenAI であって、
    このアプリではありません)。Cookie ヘッダに改行は入りませんし、
    名前に空白・引用符・波括弧・スラッシュは入りません。
    """
    if not value or any(ch in value for ch in "\r\n\t{}\""):
        return False
    for part in value.split(";"):
        name, sep, _ = part.partition("=")
        name = name.strip()
        if sep and name and not any(ch in name for ch in " '\\/"):
            return True
    return False


def _jwt_payload(token: str):
    """JWT のペイロードを dict で返します。読めなければ None。

    **署名は検証しません。** ここでの用途は、送る前に分かることを
    先に知っておく (失効しているか / どのワークスペースのトークンか) こと
    だけです。トークンが正当かどうかを決めるのは送信先の OpenAI であって、
    このアプリではありません (検証したつもりになると、鍵の取得漏れで
    逆に誤判定します)。
    """
    try:
        payload = token.split(".")[1]
        # base64url のパディングは JWT では省かれる
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(decoded)
    except (IndexError, ValueError, binascii.Error):
        return None
    return data if isinstance(data, dict) else None


def _jwt_expiry(token: str):
    """JWT の exp (Unix秒) を返します。読めなければ None。

    用途は「もう失効しているなら、401 を待たずに『貼り直してください』と
    先に言う」ことだけです (_jwt_payload の説明を参照)。
    """
    data = _jwt_payload(token)
    if data is None:
        return None
    exp = data.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


# accessToken のペイロードで、OpenAI が独自のクレームを置いている名前空間。
_AUTH_CLAIM_NS = "https://api.openai.com/auth"


def _account_headers(token: str) -> Dict[str, str]:
    """backend-api がアカウントを特定するために見るヘッダを組み立てます。

    **Authorization だけでは足りないことがあります。** 本家の Codex CLI は
    /backend-api/wham/usage を叩くとき ChatGPT-Account-Id を必ず付けて
    いますし、データレジデンシーが有効なワークスペースでは
    x-openai-internal-codex-residency が無いと 401
    (「Workspace is not authorized in this region.」) が返ります。
    401 は「資格情報が切れた」と同じ扱いになるため、画面には
    「要再ログイン」と出ます — **貼り直しても直りません。**

    **値は既に手元にあります。** accessToken のペイロードに入っているので、
    追加の問い合わせは要りません (_jwt_expiry が同じペイロードを exp の
    ためだけにデコードしていました)。

    **クレームが無いときは何も足しません。** 個人の Plus/Pro には
    これらの値が無く、付けないことがそのまま現状維持になります。
    """
    payload = _jwt_payload(token) or {}
    auth = payload.get(_AUTH_CLAIM_NS)
    auth = auth if isinstance(auth, dict) else {}

    def claim(*names):
        for name in names:
            for source in (auth, payload):
                value = source.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    headers = {}
    account_id = claim("chatgpt_account_id")
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    residency = claim("chatgpt_data_residency", "chatgpt_compute_residency")
    if residency:
        headers["x-openai-internal-codex-residency"] = residency
    return headers


_to_iso = unix_to_iso


def _renewal_key(data: Dict[str, Any], access_token: str) -> str:
    """間引きに使う、そのアカウントで変わらない鍵。取れなければ空文字。

    値をそのまま持ち歩くと例外表示やデバッガに出てしまうため、
    ハッシュにしておきます (gemini._rotation_key と同じ考え方)。
    """
    user = data.get("user")
    identity = user.get("id") if isinstance(user, dict) else None
    if not isinstance(identity, str) or not identity.strip():
        identity = (_jwt_payload(access_token) or {}).get("sub")
    if not isinstance(identity, str) or not identity.strip():
        return ""
    return hashlib.sha256(identity.strip().encode("utf-8")).hexdigest()


class ChatGPTProvider(UsageProvider):
    id = "chatgpt"
    label = "ChatGPT"
    # Codex CLI は一覧から取り下げたので、そちらへ誘導しない
    # (取得先として選べないものを案内すると、探して見つからないことになる)。
    description = "Shows the ChatGPT quotas (weekly and others)."

    auth_kind = AUTH_COOKIE
    credential_label = "Cookie / accessToken"
    credential_hint = (
        "Paste what you copied from your browser,\n"
        "or an accessToken (eyJ...) on its own."
    )

    # 普段のブラウザで取ってくる道。**これが唯一の道です。**
    #
    # 埋め込みブラウザでログインさせる作りは畳みました。Google アカウントで
    # 作った ChatGPT アカウントは、そもそも埋め込み側からサインインできません
    # (Google が拒否する)。詳細は base.UsageProvider.manual_url と
    # _resolve_access_token を参照。
    manual_url = SESSION_URL
    manual_steps = (
        "1. Use the button below to open ChatGPT in your usual browser.\n"
        "   (the browser has to be signed in to ChatGPT)\n"
        "2. Copy everything you see (Ctrl+A then Ctrl+C).\n"
        "   You do not need to hunt for the token.\n"
        "3. Paste it into the box below and save.\n"
        "   Only the part that is needed is taken out and saved."
    )

    login_url = "https://chatgpt.com/auth/login"
    home_url = "https://chatgpt.com/"
    cookie_domain = "chatgpt.com"
    # NextAuth のセッション Cookie。これが取れたらログイン成功とみなす。
    session_cookie_name = "__Secure-next-auth.session-token"

    implemented = True

    def __init__(self, timeout: int = 15):
        self.http = HttpClient(credential_label=self.credential_label, timeout=timeout)

    def is_login_page(self, url: str) -> bool:
        url = url or ""
        return "/auth/login" in url or "auth0.openai.com" in url

    def normalize_credential(self, value: str) -> str:
        """画面に出た内容を丸ごと貼られる前提で、必要な部分だけを取り出します。

        利用者に目視で探させない (「どこがアクセストークンか分からない」が
        実際に起きた) ことに加えて、**貼り付け元に混ざっている
        メールアドレスや別の資格情報を保存しない**ためでもあります。
        """
        value = (value or "").strip().strip(_INVISIBLE).strip()

        found = _extract_session_token(value)
        if not found:
            # 抽出できなかったときの最後の始末。手で accessToken の行だけを
            # 選んでコピーすると、閉じ引用符とカンマが一緒に付いてきます
            # (`eyJ....",`)。この形は _looks_like_jwt を素通りするため、
            # 警告ひとつ無いまま `Bearer eyJ...",` として送られて 401 —
            # つまり「黙って追加が通り、あとから要再ログイン」になります。
            trimmed = value.strip(',').strip('"').strip()
            return trimmed if _looks_like_jwt(trimmed) else value

        key, token = found
        if key == "sessionToken":
            # Cookie の形にしておくと、既存の Cookie 経路 (accessToken への
            # 交換) にそのまま乗るため、accessToken が切れても自動で取り直せる。
            return f"{self.session_cookie_name}={token}"
        return token

    def validate_paste(self, pasted: str) -> str:
        """貼り付けた画面が「今おかしい」と言っていないかを見ます。

        ここで拾わないと、追加は成功するのに取得だけが必ず失敗する、という
        いちばん分かりにくい壊れ方になります (_session_error の説明を参照)。
        保存する Cookie 自体は暗号化された JWE なので、保存後の値からは
        この事実を二度と読み取れません。**捨てる前のここが唯一の機会です。**
        """
        error = _session_error(_load_session_json(pasted))
        if not error:
            return ""
        return t(
            "ChatGPT says it could not refresh this session ({error}).\n"
            "The access token in it has already expired, so the usage cannot "
            "be read.\n"
            "Sign out of ChatGPT in your browser, sign in again, "
            "and then copy it once more.",
            error=error,
        )

    def validate_credential(self, value: str) -> str:
        """貼り付けミスだけを拾います。有効かどうかは OpenAI が決めることです。"""
        value = (value or "").strip()

        if value.startswith('"') or value.endswith('"'):
            return t("There are double quotes around the value.")

        # normalize_credential を通ってもなお JSON が残っている
        # = 目当ての項目が無かった。**先頭が "{" とは限りません** —
        # 整形ビューアのラベルや BOM が前に付くことがあるので、
        # 「{ で始まる」ではなく「波括弧が残っている」で見ます。
        if "{" in value and "}" in value:
            return t(
                "No accessToken could be taken out of what you pasted.\n"
                "Paste what you see in a browser that is signed in to ChatGPT.\n"
                "A browser that is not signed in shows a result with no token in it."
            )

        if not _looks_like_jwt(value) and not _looks_like_cookie(value):
            return t(
                "This looks like neither an accessToken nor a cookie.\n"
                "An accessToken starts with eyJ and is a long string "
                "split into three parts by periods."
            )

        if _looks_like_jwt(value):
            expiry = _jwt_expiry(value)
            if expiry is not None and expiry <= time.time():
                return t(
                    "This accessToken expired at {when}. Get a new one.",
                    when=_to_iso(expiry) or t("an unknown time"),
                )

        return ""

    # ---------------- リクエスト ----------------

    def _format_cookie(self, cookie: str) -> str:
        cookie_str = (cookie or "").strip()
        if cookie_str and "=" not in cookie_str:
            cookie_str = f"{self.session_cookie_name}={cookie_str}"
        return cookie_str

    def _resolve_access_token(self, cookie: str):
        """資格情報を (Bearer トークン, 保存し直すべき資格情報) にします。

        2つ目は「セッションを延命した結果」です。無ければ空文字を返します
        (延命できない経路と、間引きで見送った場合の両方)。
        詳細は RENEW_INTERVAL_SEC の説明を参照してください。

        chatgpt.com のフロントエンドは Cookie を直接 backend-api へ送らず、
        まず /api/auth/session (NextAuth) から accessToken を取り、
        それを Authorization ヘッダで使っている (DevTools で確認済み)。

        ただし Google アカウントで作った ChatGPT アカウントは、
        アプリ内ブラウザからログインできません。Google が埋め込みブラウザからの
        サインインを拒否するためです (「このブラウザまたはアプリは安全でない
        可能性があります」)。これは埋め込み側アプリがパスワード入力を
        盗み見できるという理由で存在する保護なので、迂回しません。

        代わりに、普段使いのブラウザで
        https://chatgpt.com/api/auth/session を開いて accessToken を
        コピーし、そのまま貼ってもらう経路を用意しています。
        Cookie の代わりに JWT が来たらここで見分けて、交換を飛ばします。
        """
        cookie = (cookie or "").strip()

        if _looks_like_jwt(cookie):
            expiry = _jwt_expiry(cookie)
            if expiry is not None and expiry <= time.time():
                raise UsageError(
                    t("The accessToken you pasted expired at {when}.\n"
                      "Open https://chatgpt.com/api/auth/session in a browser "
                      "and get a new accessToken.",
                      when=_to_iso(expiry) or t("an unknown time")),
                    auth_error=True,
                )
            # 生の accessToken を貼られた経路。Cookie を持っていないので
            # セッションを延ばす手立てがありません (10日で貼り直しになります)。
            return cookie, ""

        cookie_str = self._format_cookie(cookie)
        if not _looks_like_cookie(cookie_str):
            # **ここで止めないと例外が UsageError の外へ抜けます。** 改行を
            # 含む値をそのまま送ると http.client が ValueError を投げますが、
            # これは requests の例外ではないので _request の except 連鎖に
            # 引っかからず、画面には「予期しないエラー」とだけ出ます。
            raise UsageError(
                t("The saved {credential} is not a cookie or an accessToken.\n"
                  "Paste it again from a browser that is signed in to ChatGPT.",
                  credential=t(self.credential_label)),
                auth_error=True,
            )

        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": "*/*",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Cookie": cookie_str,
        }
        headers.update(_BROWSER_HEADERS)
        data = self.http.get_json(SESSION_URL, headers, t("the session"))
        if not isinstance(data, dict):
            raise UsageError(t("The response for the session is malformed."))

        # **error を無視しないこと。** Cookie が生きていても、更新に失敗した
        # 古い accessToken がそのまま返ることがあります (_session_error 参照)。
        # ここで止めないと、失効トークンで backend-api を叩いて 401 をもらい、
        # 「Cookie が切れました」という**事実と違う**案内をすることになります。
        error = _session_error(data)
        if error:
            logger.error("セッションの更新に失敗しています: %s", error)
            raise UsageError(
                t("ChatGPT could not refresh this session ({error}).\n"
                  "Sign out of ChatGPT in your browser, sign in again, "
                  "and then set it up once more. "
                  "Pasting the same value again will not help.",
                  error=error),
                auth_error=True,
            )

        token = data.get("accessToken")
        if not token:
            # **受け取ったキー名を必ず残すこと。** ここは 200 で返ってくる
            # 経路なので、キー名を出さないと「何が返ってきたのか」が画面にも
            # ログにも一切残りません (実際、chatgpt.com が未ログイン時に
            # WARNING_BANNER だけを返すようになったことに気づけませんでした)。
            # **値は載せないこと。** バナーが言うとおり、ここに出るものは
            # パスワード同然です。
            keys = ", ".join(map(str, sorted(data.keys()))) or t("(none)")
            logger.error("セッションから accessToken を取得できませんでした。"
                         "受け取ったキー: %s", keys)
            if set(data) <= {"WARNING_BANNER"}:
                # ログインしていないブラウザの画面そのもの。貼り直させる前に
                # 「そのブラウザでサインインしているか」を確かめてもらう
                # (この2つは利用者の取るべき行動が違います)。
                raise UsageError(
                    t("ChatGPT returned a signed-out session (keys: {keys}).\n"
                      "Sign in to ChatGPT in your browser first, "
                      "then get the value again.", keys=keys),
                    auth_error=True,
                )
            raise UsageError(
                t("Could not get an access token from the session. "
                  "The cookie may have expired. Keys received: {keys}",
                  keys=keys),
                auth_error=True,
            )

        # 交換で受け取った側の期限も見ます。**貼り付けた JWT だけを見ていた
        # ため、ここを素通りしていました。** 失効したものを送れば 401 が返る
        # だけですが、その 401 は「Cookie が無効か期限切れ」としか言えません。
        # 手元で分かることは手元で言います。
        expiry = _jwt_expiry(token)
        if expiry is not None and expiry <= time.time():
            raise UsageError(
                t("The access token from the session expired at {when}.\n"
                  "Open ChatGPT in your browser to refresh it, "
                  "then set it up again.",
                  when=_to_iso(expiry) or t("an unknown time")),
                auth_error=True,
            )

        return token, self._renewed_credential(cookie_str, data, token)

    def _renewed_credential(self, cookie: str, data: Dict[str, Any],
                            access_token: str) -> str:
        """保存し直すべき新しいセッション Cookie を返します。無ければ空文字。

        **ここが「認証が永続する」ことの本体です。** 応答の sessionToken は
        呼ぶたびに発行し直されたもので、有効期限は 90日先です。実測で、
        この値だけで使用状況を取得できることも確認しています。書き戻す先は
        cli.py で、fetch_usage の戻り値の "credential" を見て保存します
        (gemini.rotate_cookies と同じ経路)。
        """
        token = data.get("sessionToken")
        if not isinstance(token, str) or not token.strip():
            # 応答に載らない形になったら、延命はできないが取得は続けられる。
            # ここを例外にはしません (使用状況は既に取れています)。
            logger.debug("応答に sessionToken が無いため延命を見送ります。")
            return ""

        renewed = "%s=%s" % (self.session_cookie_name, token.strip())
        if renewed == cookie:
            return ""

        key = _renewal_key(data, access_token)
        if not key:
            # 誰の話か分からないと間引けません。取得のたびに書き戻すのは
            # 避けたいので、この場合は延命を見送ります。
            logger.debug("アカウントを特定できないため延命を見送ります。")
            return ""

        now = time.monotonic()
        last = _last_renewal.get(key)
        if last is not None and now - last < RENEW_INTERVAL_SEC:
            return ""

        _last_renewal[key] = now
        return renewed

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        if not (credential or "").strip():
            raise UsageError(
                t('{credential} is not set. Set it from "Sign In Again".',
                  credential=t(self.credential_label)),
                auth_error=True,
            )

        access_token, renewed = self._resolve_access_token(credential)

        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": "application/json",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Authorization": f"Bearer {access_token}",
        }
        headers.update(_BROWSER_HEADERS)
        # ワークスペース所属のアカウントは、これが無いと 401 になります
        # (個人アカウントではトークンに値が無いので何も足りません)。
        headers.update(_account_headers(access_token))
        data = self.http.get_json(USAGE_URL, headers, t("usage"))
        if not isinstance(data, dict):
            raise UsageError(t("The response for usage is malformed."))

        result = self.parse_usage_response(data)

        # ここまで来た = セッションは生きている。**生きているうちにしか
        # 延ばせない**ので、このタイミングで保存し直す
        # (失効を検知してからでは間に合わない — gemini と同じ考え方)。
        if renewed:
            result["credential"] = renewed
        return result

    # ---------------- レスポンス解釈 ----------------

    @staticmethod
    def _parse_balance(value: Any):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_usage_response(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        metrics = []

        rate_limit = data.get("rate_limit")
        if isinstance(rate_limit, dict):
            for key, label in (("primary_window", "primary"), ("secondary_window", "secondary")):
                window = rate_limit.get(key)
                if not isinstance(window, dict):
                    continue
                used = window.get("used_percent")
                if isinstance(used, bool) or not isinstance(used, (int, float)):
                    logger.warning("%s の使用率が数値ではありません: %r", key, used)
                    continue
                metrics.append(percent_metric(
                    label,
                    _window_label(window.get("limit_window_seconds")),
                    float(used),
                    resets_at=_to_iso(window.get("reset_at")),
                ))

        credits = data.get("credits")
        if isinstance(credits, dict) and credits.get("has_credits"):
            balance = cls._parse_balance(credits.get("balance"))
            if balance is not None:
                metrics.append(
                    amount_metric("credits", t("Extra credit balance"), balance))

        if not metrics:
            # ここで 0% を返すと「まだ使っていない」と誤解される。
            logger.error("使用状況を解釈できませんでした: %s", sorted(data.keys()))
            raise UsageError(t(
                "The usage format was not recognised "
                "(ChatGPT may have changed). Keys received: {keys}",
                keys=', '.join(map(str, sorted(data.keys()))) or t("(none)"),
            ))

        return build_result(metrics, plan_type=data.get("plan_type"), raw=data)
