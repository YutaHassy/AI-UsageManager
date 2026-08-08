"""Anthropic API (Claude Console) の課金額。

公式リファレンスで確認済みの仕様:

  GET https://api.anthropic.com/v1/organizations/cost_report
      starting_at / ending_at (ISO8601)、group_by[]=workspace_id|description
      日次 (1d) のみ。ページングは has_more / next_page → page。
  ヘッダ  x-api-key: <Admin API キー>  /  anthropic-version: 2023-06-01

レスポンス構造:

  {"data": [{"starting_at": "...", "ending_at": "...",
             "results": [{"amount": "123.78912", "currency": "USD",
                          "cost_type": "tokens", "description": "...",
                          "model": "claude-opus-4-6", "token_type": "...",
                          "workspace_id": "...", ...}]}],
   "has_more": true, "next_page": "page_..."}

**amount は「セント」単位の10進文字列です。** リファレンスに
「Cost amount in lowest currency units (e.g. cents) ... "123.45" in "USD"
represents $1.23」と明記されています。ドルとして扱うと 100 倍になるので、
必ず 100 で割ります。整数セントではなく小数を持つ点にも注意
(例 "123.78912" → $1.2378912)。

cost_type は tokens / web_search / code_execution / session_usage の4種。
model は group_by[]=description を指定したトークン系の項目にだけ入り、
それ以外は null なので、内訳の見出しは model → description → cost_type の
順に拾います。

制約:
  - **Admin API キーは個人アカウントでは使えません** (組織が必要)。
    形は sk-ant-admin01-... で、通常の API キーとは別物です。
  - 日次バケットなので、利用枠のように数分間隔で叩く意味はありません。
  - Priority Tier のコストはこのエンドポイントに含まれません
    (リファレンスに明記。含まれない分だけ実請求より少なく出ます)。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from services.i18n import t
from services.providers.base import (
    AUTH_TOKEN, HttpClient, UsageError, UsageProvider, build_result, money_metric,
)

logger = logging.getLogger(__name__)

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
API_VERSION = "2023-06-01"

# 1回の取得で辿るページ数の上限。has_more を信じて無限に回さないための保険。
_MAX_PAGES = 12


class AnthropicCostProvider(UsageProvider):
    id = "anthropic-cost"
    label = "Anthropic API"
    description = ("Shows this month's Claude API cost (USD) broken down by model. "
                   "An Admin API key is required.")

    auth_kind = AUTH_TOKEN
    credential_label = "Admin API key"
    credential_hint = ("sk-ant-admin01-... "
                       "(issued at Console → Settings → Admin keys)")
    credential_marker = "sk-ant-admin"

    supports_budget = True
    currency = "USD"

    def __init__(self, timeout: int = 30):
        self.http = HttpClient(credential_label=self.credential_label, timeout=timeout)

    # ---------------- 期間 ----------------

    @staticmethod
    def month_to_date(now: datetime = None):
        """当月1日から翌日までの期間を返します。

        ending_at を「明日」にするのは、当日分を取りこぼさないためです。
        日次バケットなので、今日の 00:00 を終端にすると今日が入りません。
        """
        now = now or datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return start.strftime(fmt), end.strftime(fmt)

    # ---------------- 取得 ----------------

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        api_key = (credential or "").strip()
        if not api_key:
            raise UsageError(
                t('{credential} is not set. Set it from "Edit".',
                  credential=self.credential_label),
                auth_error=True,
            )

        headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "Accept": "application/json",
            # リファレンスが統合作成者に User-Agent の設定を勧めているため付ける
            "User-Agent": "AI-UsageManager/1.0",
        }
        starting_at, ending_at = self.month_to_date()

        pages: List[Dict[str, Any]] = []
        page_token = None
        for _ in range(_MAX_PAGES):
            params = {
                "starting_at": starting_at,
                "ending_at": ending_at,
                "group_by[]": "description",
            }
            if page_token:
                params["page"] = page_token

            data = self.http.get_json(COST_REPORT_URL, headers, t("the cost"),
                                      params=params)
            if not isinstance(data, dict):
                raise UsageError(t("The response for the cost is malformed."))
            pages.append(data)

            if not data.get("has_more"):
                break
            page_token = data.get("next_page")
            if not page_token:
                # has_more が立っているのに次のトークンが無い = 仕様変更の疑い。
                # 黙って打ち切ると金額が少なく出るので、必ずログに残す。
                logger.warning("has_more が true ですが next_page がありません。取得を打ち切ります。")
                break
        else:
            logger.warning("ページ数が上限 (%d) に達しました。金額が不足している可能性があります。", _MAX_PAGES)

        return self.parse_cost_report(
            pages, period=t("{month} (this month)", month=starting_at[:7]))

    # ---------------- レスポンス解釈 ----------------

    @staticmethod
    def _cents_to_usd(amount: Any):
        """amount (セント単位の10進文字列) を USD に直します。

        文字列以外や数値でない値は None を返し、呼び出し元で読み飛ばします。
        ここで 0 に倒すと、壊れた行の分だけ金額が黙って少なく出てしまいます。
        """
        try:
            return float(str(amount)) / 100.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _breakdown_key(entry: Dict[str, Any]) -> str:
        """内訳の見出し。

        model はトークン系の項目にしか入らないので、
        web 検索やコード実行の分は description / cost_type で拾います。
        """
        for key in ("model", "description", "cost_type"):
            value = entry.get(key)
            if value:
                return str(value)
        return t("Other")

    @classmethod
    def parse_cost_report(cls, pages: List[Dict[str, Any]], period: str = "") -> Dict[str, Any]:
        if not pages or not any("data" in page for page in pages):
            # data キーが無いのは仕様変更。0 ドルと報告すると「使っていない」と誤解される。
            raise UsageError(t(
                "The cost format was not recognised "
                "(the API may have changed). Keys received: {keys}",
                keys=', '.join(sorted(pages[0].keys())) if pages else t("(none)"),
            ))

        total_usd = 0.0
        by_key: Dict[str, float] = {}
        currencies = set()
        skipped = 0

        for page in pages:
            buckets = page.get("data")
            if not isinstance(buckets, list):
                continue
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                for entry in bucket.get("results") or []:
                    if not isinstance(entry, dict):
                        continue
                    usd = cls._cents_to_usd(entry.get("amount"))
                    if usd is None:
                        skipped += 1
                        continue
                    currencies.add(entry.get("currency") or "USD")
                    total_usd += usd
                    by_key[cls._breakdown_key(entry)] = (
                        by_key.get(cls._breakdown_key(entry), 0.0) + usd
                    )

        if skipped:
            logger.warning("金額として読めない項目を %d 件読み飛ばしました。", skipped)

        # 通貨が混ざると単純な合計は意味を持たない。現状 USD のみのはずなので、
        # 混ざったら合計を出さずに気づけるようにする。
        if len(currencies) > 1:
            raise UsageError(t(
                "Several currencies are mixed together ({currencies}). "
                "A total cannot be calculated.",
                currencies=', '.join(sorted(currencies)),
            ))
        currency = currencies.pop() if currencies else "USD"

        metrics = [money_metric("total", t("Cost this month"), total_usd,
                                currency=currency, period=period or None)]

        for name, usd in sorted(by_key.items(), key=lambda kv: kv[1], reverse=True):
            if usd <= 0:
                continue
            metrics.append(money_metric(f"item:{name}", name, usd, currency=currency))

        return build_result(metrics, total_usd=total_usd, breakdown=by_key)
