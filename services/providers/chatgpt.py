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


def _extract_session_token(text: str):
    """/api/auth/session の出力から (項目名, 値) を取り出します。

    見つからなければ None。ここで取りこぼしたものは
    「貼り付けた内容がそのまま資格情報として扱われる」だけなので、
    無理に拾おうとせず、確実に分かる形だけを対象にします。
    """
    text = (text or "").strip()
    if not text.startswith("{"):
        return None

    try:
        data = json.loads(text)
    except ValueError:
        data = None

    if isinstance(data, dict):
        for key in _TOKEN_PRIORITY:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return key, value.strip()
        return None

    # JSON として読めない = ブラウザからのコピーが途中で切れた等。
    # 形だけを頼りに拾い直す。
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


def _jwt_expiry(token: str):
    """JWT の exp (Unix秒) を返します。読めなければ None。

    **署名は検証しません。** ここでの用途は「もう失効しているなら、
    401 を待たずに『貼り直してください』と先に言う」ことだけです。
    トークンが正当かどうかを決めるのは送信先の OpenAI であって、このアプリでは
    ありません (検証したつもりになると、鍵の取得漏れで逆に誤判定します)。
    """
    try:
        payload = token.split(".")[1]
        # base64url のパディングは JWT では省かれる
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(decoded)
    except (IndexError, ValueError, binascii.Error):
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


_to_iso = unix_to_iso


class ChatGPTProvider(UsageProvider):
    id = "chatgpt"
    label = "ChatGPT"
    # Codex CLI は一覧から取り下げたので、そちらへ誘導しない
    # (取得先として選べないものを案内すると、探して見つからないことになる)。
    description = "Shows the ChatGPT quotas (weekly and others)."

    auth_kind = AUTH_COOKIE
    credential_label = "Cookie / accessToken"
    credential_hint = (
        'Sign in with "Get cookies automatically".\n'
        'If you cannot sign in there, use "Cannot sign in?" on the sign-in '
        "window to paste an accessToken instead."
    )

    # Google アカウントでログインしていると、アプリ内ブラウザからは
    # サインインできません (Google が埋め込みブラウザを拒否する)。
    # その場合の逃げ道。詳細は _resolve_access_token を参照。
    manual_url = SESSION_URL
    manual_steps = (
        "1. Use the button below to open your usual browser.\n"
        "   (open it in a browser that is signed in to ChatGPT)\n"
        "2. Copy everything you see (Ctrl+A then Ctrl+C).\n"
        "   You do not need to hunt for the token.\n"
        '3. Paste it into the box below and press "Set".\n'
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
        value = (value or "").strip()

        found = _extract_session_token(value)
        if not found:
            return value

        key, token = found
        if key == "sessionToken":
            # Cookie の形にしておくと、既存の Cookie 経路 (accessToken への
            # 交換) にそのまま乗るため、accessToken が切れても自動で取り直せる。
            return f"{self.session_cookie_name}={token}"
        return token

    def validate_credential(self, value: str) -> str:
        """貼り付けミスだけを拾います。有効かどうかは OpenAI が決めることです。"""
        value = (value or "").strip()

        if value.startswith('"') or value.endswith('"'):
            return t("There are double quotes around the value.")

        # normalize_credential を通ってもなお JSON = 目当ての項目が無かった
        if value.startswith("{"):
            return t(
                "No accessToken could be taken out of what you pasted.\n"
                "Paste what you see in a browser that is signed in to ChatGPT.\n"
                "A browser that is not signed in shows a result with no token in it."
            )

        if not _looks_like_jwt(value) and "=" not in value:
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

    def _resolve_access_token(self, cookie: str) -> str:
        """資格情報を、backend-api 用の Bearer トークンにします。

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
            return cookie

        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": "*/*",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Cookie": self._format_cookie(cookie),
        }
        data = self.http.get_json(SESSION_URL, headers, t("the session"))
        if not isinstance(data, dict):
            raise UsageError(t("The response for the session is malformed."))

        token = data.get("accessToken")
        if not token:
            raise UsageError(
                t("Could not get an access token from the session. "
                  "The cookie may have expired."),
                auth_error=True,
            )
        return token

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        if not (credential or "").strip():
            raise UsageError(
                t('{credential} is not set. Set it from "Sign In Again".',
                  credential=self.credential_label),
                auth_error=True,
            )

        access_token = self._resolve_access_token(credential)

        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": "application/json",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Authorization": f"Bearer {access_token}",
        }
        data = self.http.get_json(USAGE_URL, headers, t("usage"))
        if not isinstance(data, dict):
            raise UsageError(t("The response for usage is malformed."))

        return self.parse_usage_response(data)

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
