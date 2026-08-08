"""Antigravity (Gemini) のクォータ使用率と G1 クレジット残高。

## クォータ (本命。Claude の「5時間枠・週間枠の利用率」に相当)

  POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary
  ボディ  {"project": "<cloudaicompanionProject>"}

スキーマは agy.exe 内の proto ディスクリプタから抽出 (JSON では camelCase):

  RetrieveUserQuotaSummaryResponse {
      buckets: QuotaSummaryBucket[]      // json: buckets
      groups:  QuotaSummaryGroup[]       // json: groups
      description: string
  }
  QuotaSummaryBucket {
      bucket_id          → bucketId
      display_name       → displayName
      description        → description
      window             → window
      remaining_fraction → remainingFraction   // oneof "remaining" (0.0-1.0)
      remaining_amount   → remainingAmount     // oneof "remaining" (件数)
      disabled           → disabled
      reset_time         → resetTime           // google.protobuf.Timestamp
  }
  QuotaSummaryGroup { display_name → displayName, description, buckets[] }

**使用率 = (1 - remainingFraction) * 100** です。残り割合であって使用率では
ないので、そのまま出すと意味が逆になります。

### 現状の障害
このエンドポイントは **403 PERMISSION_DENIED** を返します。同じトークンで
loadCodeAssist は 200 なので、スコープではなく **OAuth クライアント単位の許可**
の問題です。下で使っている gemini-cli のクライアントでは quota 系を呼べません。
agy 本体は別のクライアントを持ち、トークンも別の場所に保管しています
(Windows 資格情報マネージャーには該当エントリが見当たりませんでした)。

そのため parse は実装・テスト済みですが、**実データでの確認はできていません。**

## G1 クレジット残高 (副次的)

取得経路 (実測で確認済み):

  POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
       ※ メソッドの区切りは「:」でスラッシュではない
  ヘッダ  Authorization: Bearer <access_token>
          Content-Type: application/json
          User-Agent: **必ず "antigravity" を含めること**
  ボディ  {"metadata": {"ideType": "ANTIGRAVITY", "platform": "WINDOWS_AMD64"}}

**User-Agent がサーバ側のゲートです。** "antigravity" を含まない UA
(agy/1.0 や curl/8.0 など) を送ると reasonCode=UNSUPPORTED_CLIENT が返り、
currentTier / paidTier が丸ごと消えます。これは「クレジット残高 0」と
見分けがつかない静かな失敗なので、下の実装では必ず別のエラーにします。

レスポンスからクレジットを読む場所 (agy.exe の proto ディスクリプタと
gemini-cli の packages/core/src/billing/billing.ts が独立に一致):

  paidTier.availableCredits[] のうち creditType == "GOOGLE_ONE_AI" のものを
  合計する。creditAmount は **int64 の文字列表現** なので int() が必要。

  残高が「無い」(null) ことと「0」であることは意味が違います。
  前者は対象外、後者は使い切りです。混同すると嘘になります。

分母 (総量) を返すフィールドは見つかっていません。したがってゲージには
できず、残量だけを表示します。gemini-cli もパーセント表示はしていません。

認証:
  agy 本体は Windows Credential Manager にトークンを持ちますが、
  ここでは %USERPROFILE%\\.gemini\\oauth_creds.json (gemini-cli のもの) の
  refresh_token を使って access_token を取り直します。
  **このファイルへ書き戻しはしません** (他アプリの状態を壊さないため)。

未解決:
  調査時点のアカウントでは paidTier.id = "g1-pro-tier" が返るのに
  availableCredits が返りませんでした。gemini-cli の OAuth クライアントでは
  クレジットが返らず、Antigravity 自身のクライアントが要る可能性がありますが
  未検証です。そのため実際に残高が取れるかの確度は低いままです。
"""

import json
import logging
import os
import time
from typing import Any, Dict

from services.i18n import t
from services.providers.base import (
    AUTH_OAUTH, HttpClient, UsageError, UsageProvider, amount_metric, build_result,
    percent_metric,
)

logger = logging.getLogger(__name__)

LOAD_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
QUOTA_SUMMARY_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# アクセストークンの更新に使う installed-app 用の OAuth クライアント。
#
# **このリポジトリには値を置きません。** 値そのものは gemini-cli が自分の
# リポジトリで公開しているもので、Google も installed app の client secret を
# 秘密として扱わないとしています。とはいえ**他人の OAuth クライアントであり、
# こちらが配布してよいものではありません。** 公開リポジトリに置くと
# シークレットスキャンにも掛かります (実際 push が弾かれました)。
#
# 使う場合は、gemini-cli の公開している値を環境変数で渡してください:
#
#     ANTIGRAVITY_CLIENT_ID / ANTIGRAVITY_CLIENT_SECRET
#
# 未設定なら取得は「設定されていない」と分かる形で失敗します
# (無言で 401 になるより、何を用意すればよいかを言う方がよい)。
#
# なお **このプロバイダは _RETIRED です** (services/providers/__init__.py)。
# クレジット残高は取れますが、クォータ使用率は retrieveUserQuotaSummary が
# 403 を返すため未達で、一覧には出していません。
CLIENT_ID = os.environ.get("ANTIGRAVITY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", "")

# サーバ側のゲートを通すための UA。"antigravity" を含めることが必須。
USER_AGENT = "antigravity-cli/1.0 (windows; amd64)"

# gemini-cli の G1_CREDIT_TYPE と同じ
G1_CREDIT_TYPE = "GOOGLE_ONE_AI"

# アクセストークンの期限が近いとみなす余裕 (秒)
_EXPIRY_MARGIN = 60


def gemini_home() -> str:
    return os.path.join(os.path.expanduser("~"), ".gemini")


def oauth_creds_path() -> str:
    return os.path.join(gemini_home(), "oauth_creds.json")


class AntigravityProvider(UsageProvider):
    id = "antigravity"
    label = "Antigravity (Gemini)"
    description = ("The Antigravity G1 credit balance. "
                   "Uses the gemini-cli sign-in state.")

    auth_kind = AUTH_OAUTH
    credential_label = "the Antigravity / gemini-cli sign-in state"

    def __init__(self, timeout: int = 20):
        self.http = HttpClient(credential_label=self.credential_label, timeout=timeout)
        # 取得のたびにトークンを取り直さないよう、期限まではメモリに持つ
        self._access_token = None
        self._expires_at = 0.0

    # ---------------- 認証 ----------------

    @staticmethod
    def _read_creds() -> Dict[str, Any]:
        path = oauth_creds_path()
        if not os.path.exists(path):
            raise UsageError(
                t("The OAuth credential was not found: {path}\n"
                  "Sign in to gemini-cli / Antigravity.", path=path),
                auth_error=True,
            )
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise UsageError(
                t("The OAuth credential could not be read: {reason}", reason=e),
                auth_error=True) from e
        if not isinstance(data, dict):
            raise UsageError(
                t("The OAuth credential is malformed."), auth_error=True)
        return data

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._expires_at - _EXPIRY_MARGIN:
            return self._access_token

        creds = self._read_creds()

        # 保存済みトークンがまだ有効ならそのまま使う (expiry_date はミリ秒)
        expiry_ms = creds.get("expiry_date")
        token = creds.get("access_token")
        if token and isinstance(expiry_ms, (int, float)):
            expires_at = float(expiry_ms) / 1000.0
            if now < expires_at - _EXPIRY_MARGIN:
                self._access_token, self._expires_at = token, expires_at
                return token

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            raise UsageError(
                t("The access token has expired and there is no refresh_token. "
                  "Sign in again."),
                auth_error=True,
            )

        # OAuth クライアントは環境変数から渡してもらう (上の CLIENT_ID の説明を参照)。
        # ここで止めないと、client_id が空のまま Google へ投げて 401 になり、
        # 「サインインし直してください」という的外れな案内に化ける。
        if not CLIENT_ID or not CLIENT_SECRET:
            raise UsageError(t(
                "The OAuth client for refreshing the token is not configured.\n"
                "Set ANTIGRAVITY_CLIENT_ID and ANTIGRAVITY_CLIENT_SECRET to the "
                "installed-app credentials published by gemini-cli."
            ))

        # トークンエンドポイントは JSON ではなくフォームを要求する
        data = self.http.post_json(
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded"},
            t("the access token"),
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = (data or {}).get("access_token")
        if not token:
            raise UsageError(
                t("The access token could not be refreshed. Sign in again."),
                auth_error=True,
            )

        expires_in = data.get("expires_in")
        self._access_token = token
        self._expires_at = now + (float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0)
        # oauth_creds.json へは書き戻さない (gemini-cli の状態を壊さないため)
        return token

    # ---------------- 取得 ----------------

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # これが無いと UNSUPPORTED_CLIENT になり tier 情報が消える
            "User-Agent": USER_AGENT,
        }

    def fetch_usage(self, credential: str = "", organization_id: str = "") -> Dict[str, Any]:
        token = self._get_access_token()
        headers = self._headers(token)

        info = self.http.post_json(
            LOAD_CODE_ASSIST_URL, headers, t("the account info"),
            json_body={"metadata": {"ideType": "ANTIGRAVITY", "platform": "WINDOWS_AMD64"}},
        )

        # 本命はクォータの使用率。取れない場合だけクレジット残高に落とす。
        project = (info or {}).get("cloudaicompanionProject")
        try:
            quota = self.http.post_json(
                QUOTA_SUMMARY_URL, headers, t("the quota"),
                json_body={"project": project} if project else {},
            )
            return self.parse_quota_summary(quota)
        except UsageError as e:
            logger.info("クォータを取得できなかったため、クレジット残高に切り替えます: %s", e)
            quota_error = e

        try:
            return self.parse_load_code_assist(info)
        except UsageError as e:
            # どちらも取れないときは、本命側の理由を伝えないと打つ手が分からない
            raise UsageError(t(
                "The quota utilization could not be fetched.\n{quota_error}\n\n"
                "The credit balance could not be fetched either.\n{credit_error}",
                quota_error=quota_error, credit_error=e,
            )) from e

    # ---------------- クォータの解釈 ----------------

    @classmethod
    def _bucket_metric(cls, bucket: Dict[str, Any], prefix: str = ""):
        """QuotaSummaryBucket を1つの指標にします。

        remainingFraction は「残り割合」なので、使用率にするには
        1 から引きます。そのまま出すと意味が逆になります。
        """
        if not isinstance(bucket, dict) or bucket.get("disabled"):
            return None

        bucket_id = bucket.get("bucketId") or bucket.get("displayName") or "bucket"
        label = bucket.get("displayName") or bucket.get("description") or bucket_id
        window = bucket.get("window")
        if window:
            label = f"{label} ({window})"
        key = f"{prefix}{bucket_id}"
        resets_at = bucket.get("resetTime")

        fraction = bucket.get("remainingFraction")
        if isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
            return percent_metric(key, label, (1.0 - float(fraction)) * 100.0,
                                  resets_at=resets_at)

        amount = bucket.get("remainingAmount")
        if amount is not None:
            try:
                # int64 は文字列で来ることがある
                value = float(str(amount))
            except (TypeError, ValueError):
                logger.warning("remainingAmount を数値として読めませんでした: %r", amount)
                return None
            return amount_metric(
                key, t("{label} remaining", label=label), value, resets_at=resets_at)

        # remaining は oneof なので、どちらも無い = 情報が無い。
        # 0% と報告すると「まだ使っていない」と誤解される。
        logger.warning("バケット %s に remaining がありません: %s", bucket_id, sorted(bucket.keys()))
        return None

    @classmethod
    def parse_quota_summary(cls, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise UsageError(t("The response for the quota is malformed."))

        metrics = []
        for bucket in data.get("buckets") or []:
            metric = cls._bucket_metric(bucket)
            if metric:
                metrics.append(metric)

        for group in data.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_name = group.get("displayName") or ""
            for bucket in group.get("buckets") or []:
                metric = cls._bucket_metric(bucket, prefix=f"{group_name}/")
                if metric:
                    if group_name:
                        metric["label"] = f"{group_name} / {metric['label']}"
                    metrics.append(metric)

        if not metrics:
            raise UsageError(t(
                "The quota format was not recognised "
                "(the API may have changed). Keys received: {keys}",
                keys=', '.join(sorted(data.keys())) or t("(none)"),
            ))

        return build_result(metrics, description=data.get("description"), raw=data)

    # ---------------- クレジットの解釈 ----------------

    @staticmethod
    def _sum_g1_credits(tier: Any):
        """ティアの availableCredits から G1 クレジットを合計します。

        対象のクレジットが1件も無ければ None を返します
        (残高 0 と「そもそも対象外」を区別するため)。
        """
        if not isinstance(tier, dict):
            return None
        credits = tier.get("availableCredits")
        if not isinstance(credits, list):
            return None

        total = None
        for entry in credits:
            if not isinstance(entry, dict) or entry.get("creditType") != G1_CREDIT_TYPE:
                continue
            amount = entry.get("creditAmount")
            try:
                # creditAmount は int64 の「文字列」で来る
                value = int(str(amount))
            except (TypeError, ValueError):
                logger.warning("creditAmount を数値として読めませんでした: %r", amount)
                continue
            total = (total or 0) + value
        return total

    @classmethod
    def parse_load_code_assist(cls, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise UsageError(t("The response for the credit balance is malformed."))

        # UNSUPPORTED_CLIENT はティア情報が丸ごと欠けるため、
        # 「残高0」と見分けがつかない。必ず別のエラーにする。
        reason = data.get("reasonCode")
        if reason:
            raise UsageError(t(
                "The server refused the request ({reason}).\n"
                "{detail}\n"
                "The User-Agent check may be catching it.",
                reason=reason, detail=data.get('reasonMessage') or '',
            ))

        paid_tier = data.get("paidTier")
        current_tier = data.get("currentTier")
        credits = cls._sum_g1_credits(paid_tier)
        if credits is None:
            credits = cls._sum_g1_credits(current_tier)

        if credits is None:
            # ここで 0 を返すと「使い切った」と誤解される。
            tier_id = ((paid_tier or {}).get("id") or (current_tier or {}).get("id")
                       or t("(unknown)"))
            raise UsageError(t(
                "The response contained no G1 credit information.\n"
                "Tier: {tier}\n"
                "This account may not expose its credit balance.",
                tier=tier_id,
            ))

        tier_name = (paid_tier or {}).get("name") or (current_tier or {}).get("name") or ""
        label = (t("G1 credit balance ({tier})", tier=tier_name) if tier_name
                 else t("G1 credit balance"))

        # 総量を返すフィールドが無いため、ゲージにはできず残量だけを出す
        metrics = [amount_metric("g1_credits", label, credits)]

        return build_result(
            metrics,
            tier_id=(paid_tier or {}).get("id") or (current_tier or {}).get("id"),
            tier_name=tier_name,
            raw=data,
        )
