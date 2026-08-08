"""Claude.ai (サブスクリプション) の利用枠を取得する。

claude.ai の内部 API は非公開のため、キー構成は予告なく変わりえます。
形式を認識できなくなったときに「使用率 0%」と偽らないことが重要です
(利用者が「まだ余裕がある」と誤解してしまうため)。
"""

import logging
import re
from typing import Any, Dict, List

from services.i18n import t
from services.providers.base import (
    AUTH_COOKIE, HttpClient, UsageError, UsageProvider, build_result,
    extract_cookie_header, parse_cookie_header, percent_metric,
)

logger = logging.getLogger(__name__)

_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# 既知の制限枠。表示順もこの順序に従う。
#
# ラベルは**訳す前の原文 (英語)** です。訳すのは metric を組み立てるところ
# (parse_usage_response) で、ここではありません。表を作る場所と使う場所で
# 規約を変えないための約束です (services/i18n.py の docstring を参照)。
KNOWN_LIMITS = [
    ("five_hour", "5-hour"),
    ("seven_day", "Weekly (all)"),
    ("seven_day_opus", "Weekly (Opus)"),
    ("seven_day_sonnet", "Weekly (Sonnet)"),
]


class ClaudeProvider(UsageProvider):
    id = "claude"
    label = "Claude.ai"
    description = "Shows the Claude Pro / Max quotas (5-hour and weekly)."

    auth_kind = AUTH_COOKIE
    credential_label = "Cookie"
    credential_hint = (
        'Press "Get cookies automatically" above and sign in,\n'
        "or paste sessionKey=sk-ant-sid01-... yourself."
    )
    credential_marker = "sk-ant-"

    login_url = "https://claude.ai/login"
    home_url = "https://claude.ai/"
    cookie_domain = "claude.ai"
    session_cookie_name = "sessionKey"

    uses_extra_field = True
    extra_field_label = "Organization ID"
    extra_field_hint = "Left empty, it is looked up on the first fetch"

    def __init__(self, timeout: int = 15):
        self.http = HttpClient(credential_label=self.credential_label, timeout=timeout)

    def is_login_page(self, url: str) -> bool:
        url = url or ""
        return "/login" in url or "/magic-link" in url

    def normalize_credential(self, value: str) -> str:
        """開発者ツールから持ち出した内容を、保存できる形に整えます。

        取り除くのは**貼り付けの飾りだけ** (curl のオプション、引用符、
        ヘッダ名) です。Cookie そのものは減らしません。

        **Gemini と方針が違う点に注意してください。** あちらは貼られた
        Cookie を3つに絞って保存します。Google の Cookie ヘッダには
        Google アカウント全体を操作できるものが混ざるため、設定ファイルが
        漏れたときの被害を抑える必要があるからです。

        Claude の Cookie は claude.ai に閉じており、しかも取得の可否は
        sessionKey だけで決まるとは限りません (Cloudflare が発行する
        cf_clearance のように、落とすと弾かれうるものが混ざります)。
        ここで絞ると、アプリ内ブラウザでログインした場合に今まで保存
        されていた Cookie が減り、これまで通っていた取得が通らなくなる
        可能性があります。**減らす理由が無いものは減らしません。**
        """
        value = (value or "").strip()

        header = extract_cookie_header(value)
        if self.session_cookie_name in parse_cookie_header(header):
            return header

        # Cookie の形で sessionKey が見つからない = 値だけを貼られた
        # (sk-ant-sid01-...) 可能性がある。_headers が Cookie 形式へ
        # 整えるので、そのまま返す。取り違えの指摘は validate_credential に任せる。
        return value

    def cookies_for_profile(self, pasted: str) -> str:
        """プロファイルへは、飾りを外した Cookie 一式をそのまま預けます。

        normalize_credential と違い、こちらは「セッションの維持に要るもの
        全部」が対象です (base.UsageProvider.cookies_for_profile 参照)。
        Claude では両者が同じ内容になりますが、意味が違うので別に書きます。
        """
        return extract_cookie_header(pasted)

    def validate_credential(self, value: str) -> str:
        """貼り付けミスだけを拾います。有効かどうかを決めるのは claude.ai です。"""
        value = (value or "").strip()
        if not value:
            return ""

        jar = parse_cookie_header(value)
        if self.session_cookie_name in jar:
            return ""
        # sessionKey の値だけを貼られた形 (sk-ant-sid01-...)
        if self.credential_marker in value and "=" not in value:
            return ""

        if value.startswith("curl") or " -H " in value:
            return t(
                "No cookie could be taken out of what you pasted.\n"
                "You may have picked a row that is not a request to Claude.ai.\n"
                'Type "organizations" into the Filter box to narrow the list,\n'
                'then run "Copy as cURL" again on one of the remaining rows.'
            )

        return t(
            "{cookie} was not found.\n"
            "Take it from a browser that is signed in to Claude.ai.\n"
            "(the value of {cookie} starts with {marker}sid01-)",
            cookie=self.session_cookie_name, marker=self.credential_marker,
        )

    def validate_extra(self, value: str) -> str:
        if value and not _UUID_PATTERN.match(value):
            return t(
                "The Organization ID is not in UUID form.\n"
                "Left empty, it is looked up on the first fetch. Save it anyway?"
            )
        return ""

    # ---------------- リクエスト ----------------

    def _headers(self, cookie: str) -> Dict[str, str]:
        # sessionKey のみが渡された場合は Cookie 形式に整形する
        cookie_str = (cookie or "").strip()
        if cookie_str and "sessionKey=" not in cookie_str and not cookie_str.startswith("{"):
            cookie_str = f"sessionKey={cookie_str}"

        return {
            "User-Agent": self.http.user_agent,
            "Accept": "application/json",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": "https://claude.ai/",
            "Origin": "https://claude.ai",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": cookie_str,
        }

    def _get(self, url: str, cookie: str, what: str) -> Any:
        if not (cookie or "").strip():
            raise UsageError(
                t('Cookie / sessionKey is not set. Set it from "Sign In Again".'),
                auth_error=True,
            )
        return self.http.get_json(url, self._headers(cookie), what)

    def fetch_organizations(self, cookie: str) -> list:
        """Cookie を使って所属している Organization 一覧を取得します。"""
        data = self._get("https://claude.ai/api/organizations", cookie,
                         t("the organization list"))
        if not isinstance(data, list):
            raise UsageError(
                t("The response for the organization list is malformed."))
        return data

    def resolve_organization_id(self, cookie: str) -> str:
        """利用状況の取得に使う Organization ID を決定します。"""
        orgs = self.fetch_organizations(cookie)
        if not orgs:
            raise UsageError(t("You do not belong to any organization."))

        # チャット機能を持つ組織を優先する。該当が無ければ先頭を使う。
        # (複数組織に所属している場合の選定基準は公開されていないため、
        #  誤った組織を選んだ疑いがあることをログに残す)
        def has_chat(org):
            caps = org.get("capabilities")
            return isinstance(caps, list) and "chat" in caps

        candidates = [o for o in orgs if isinstance(o, dict) and has_chat(o)]
        if not candidates:
            candidates = [o for o in orgs if isinstance(o, dict)]
        if not candidates:
            raise UsageError(t("The organization list is malformed."))

        if len(orgs) > 1:
            logger.info(
                "複数の Organization (%d件) に所属しています。'%s' を使用します。"
                "意図しない組織の場合はアカウント編集画面で Organization ID を直接指定してください。",
                len(orgs), candidates[0].get("name") or candidates[0].get("uuid"),
            )

        org_id = candidates[0].get("uuid")
        if not org_id:
            raise UsageError(t("Could not get the Organization ID."))
        return org_id

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        """使用状況を取得します。Organization ID が未設定なら自動取得します。"""
        org_id = (organization_id or "").strip()
        if not org_id:
            org_id = self.resolve_organization_id(credential)

        url = f"https://claude.ai/api/organizations/{org_id}/usage"
        data = self._get(url, credential, t("usage"))

        if not isinstance(data, dict):
            raise UsageError(t("The response for usage is malformed."))

        return self.parse_usage_response(data, organization_id=org_id)

    # ---------------- レスポンス解釈 ----------------

    @staticmethod
    def _coerce_utilization(value: Any, key: str):
        """utilization の値を 0-100 のパーセントとして解釈します。

        claude.ai の内部APIは 0-100 のパーセント値を返します
        (0.0-1.0 の比率ではありません)。以前は「1.0 以下なら比率」と推測して
        100倍していたため、使用率 0.5% が 50% と表示されていました。
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.warning("%s.utilization が数値ではありません: %r", key, value)
            return None
        util = float(value)
        if util < 0 or util > 100:
            logger.warning("%s.utilization が想定範囲外です: %s", key, util)
        return util

    def parse_usage_response(self, data: Dict[str, Any], organization_id: str = "") -> Dict[str, Any]:
        metrics: List[Dict[str, Any]] = []

        for key, label in KNOWN_LIMITS:
            entry = data.get(key)
            if not isinstance(entry, dict):
                continue
            util = self._coerce_utilization(entry.get("utilization"), key)
            if util is None:
                continue
            metrics.append(percent_metric(
                key, t(label), util,
                resets_at=entry.get("resets_at") or entry.get("resetsAt"),
            ))

        if not metrics:
            legacy = self._parse_legacy_message_limit(data)
            if legacy:
                metrics.append(legacy)

        if not metrics:
            # ここで 0% を返してしまうと「使い切っていない」とユーザーが誤解する。
            # 形式変更を検知できるよう、キー構成をログに残したうえで失敗として扱う。
            logger.error("使用状況のレスポンスから既知の制限枠を検出できませんでした: %s", list(data.keys()))
            raise UsageError(t(
                "The usage format was not recognised "
                "(the API may have changed). Keys received: {keys}",
                keys=', '.join(map(str, list(data.keys())[:10])) or t("(none)"),
            ))

        return build_result(metrics, organization_id=organization_id, raw=data)

    def _parse_legacy_message_limit(self, data: Dict[str, Any]):
        """旧仕様の messageLimit 形式へのフォールバック。

        現行の claude.ai では確認できていない形式ですが、念のため残しています。
        値の型が不正な場合は例外を出さずに None を返します。
        """
        ml = data.get("messageLimit")
        if not isinstance(ml, dict):
            return None

        limit = ml.get("limit")
        remaining = ml.get("remaining")
        if isinstance(limit, bool) or isinstance(remaining, bool):
            return None
        if not isinstance(limit, (int, float)) or not isinstance(remaining, (int, float)):
            logger.warning("messageLimit の limit/remaining が数値ではありません: %r / %r", limit, remaining)
            return None
        if limit <= 0:
            logger.warning("messageLimit の limit が 0 以下です: %r", limit)
            return None

        return percent_metric(
            "five_hour", t("5-hour"), (limit - remaining) / limit * 100.0,
            resets_at=ml.get("resetsAt") or ml.get("resets_at"),
        )
