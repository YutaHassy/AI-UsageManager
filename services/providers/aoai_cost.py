"""Azure OpenAI ゲートウェイの API 課金額 (JPY)。

Azure Cost Management ではなく、組織が立てているゲートウェイの /bill/billing を
1回 GET するだけで取れます。認証は静的な api-key ヘッダ1本で、Azure AD の
資格情報は不要です。

**エンドポイントは利用者が設定します。** 既定値は持ちません。この形式の
ゲートウェイは組織ごとに立てるもので、万人に通用する URL が無いためです
(以前は特定組織の URL を既定値として焼き込んでいましたが、公開にあたって
外しました)。

レスポンスは JSON ではなく右寄せのプレーンテキストの表です (実測):

                         model  input_tokens  output_tokens       spend  count   spend_jpy
     azure_ai/claude-haiku-4-5       3260956          14442  115.390096     40  138.468115
    azure_ai/claude-sonnet-4-5       9369862          80694 1280.528688    114 1536.634426
               claude-opus-4-8             0              0    0.000000      1    0.000000
    Total Cost 1675 JPY

列は右から数えて 5 つが数値 (input_tokens / output_tokens / spend / count / spend_jpy)
で、それより前がモデル名です。モデル名に空白が入っても壊れないよう、
左から数えずに右から数えて切り出します。

金額は spend_jpy 列 (末尾) を使います。spend 列とは値が違い、
どちらが最終請求額なのかはこちら側では判断できないため、
運用で使われている spend_jpy に合わせています。

Total Cost 行は整数に丸められている (spend_jpy の合計 1675.10 に対して 1675)
ため、行が無い場合は spend_jpy を合計してフォールバックします。

期間指定のパラメータは無く、返るのは累計です。日次や月次の内訳は取れません。
"""

import logging
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from services.i18n import t
from services.providers.base import (
    AUTH_TOKEN, HttpClient, UsageError, UsageProvider, build_result, money_metric,
)

logger = logging.getLogger(__name__)

# [B-04] API キーの送信を許すホストの範囲。**利用者が設定します。**
#
# 制限が無いと、入力ミスや「このURLを設定してください」という誘導だけで
# API キーが第三者のホストへ出ていきます。とはいえ、許してよいホストが
# どれなのかはこちらには分かりません — 組織ごとに違うためです。
# そこで「自分の組織のドメインを申告してもらい、その外へは送らない」形に
# しています。設定していなければ制限しません (保存時の確認だけが歯止めに
# なります)。
#
# 書き方は config.json の設定 aoai_allowed_hosts に、カンマ区切りで:
#
#     "aoai_allowed_hosts": "example.com, gateway.example.co.jp"
#
# example.com と書けば example.com 自身と *.example.com が通ります。
_allowed_hosts: List[str] = []


def configure(settings: dict) -> None:
    """アプリ設定から、API キーの送信を許すホストを取り込みます。

    proxy_manager.configure と同じで、設定を読み込んだ側から渡してもらいます
    (プロバイダから設定ファイルを読みに行かせない)。
    """
    global _allowed_hosts
    raw = (settings or {}).get("aoai_allowed_hosts", "") or ""
    _allowed_hosts = [part.strip().lower().lstrip(".")
                      for part in raw.split(",") if part.strip()]
    if _allowed_hosts:
        logger.info("Azure OpenAI の送信先を次のドメインに限ります: %s",
                    ", ".join(_allowed_hosts))


def _host_matches(host: str, pattern: str) -> bool:
    """host が pattern (ドメイン) の内側かどうかを返します。

    **ドットの位置が重要です。** 単純な endswith にすると、
    "evilexample.com".endswith("example.com") が True になり、
    別人のドメインが通ってしまいます。境界のドットを必ず挟みます。
    """
    if not host or not pattern:
        return False
    return host == pattern or host.endswith("." + pattern)


def is_restricted() -> bool:
    """送信先の制限が設定されているかどうか。"""
    return bool(_allowed_hosts)


def _is_allowed_host(host: str) -> bool:
    """このホストへ API キーを送ってよいかどうかを返します。

    **制限が未設定なら通します。** 既定で塞いでしまうと、設定の存在に
    気づいていない利用者には「何をしても取得できない」だけの機能になります。
    """
    host = (host or "").lower()
    if not host:
        return False
    if not _allowed_hosts:
        return True
    return any(_host_matches(host, p) for p in _allowed_hosts)

# ヘッダ行と合計行。書式が変わったことを検知できるようにパターンで持つ。
_HEADER_RE = re.compile(r"\bmodel\b.*\bspend_jpy\b")
_TOTAL_RE = re.compile(r"Total\s+Cost\s+([0-9][0-9,]*(?:\.[0-9]+)?)")

# 右から数えて数値であることを期待する列数 (input/output/spend/count/spend_jpy)
_NUMERIC_TAIL = 5


def _to_number(text: str):
    """カンマ区切りを許して数値化します。数値でなければ None。"""
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


class AoaiCostProvider(UsageProvider):
    id = "aoai-cost"
    label = "Azure OpenAI"
    description = ("Shows the cumulative cost (JPY) of an Azure OpenAI gateway, "
                   "broken down by model.")

    auth_kind = AUTH_TOKEN
    credential_label = "API key"
    credential_hint = ("Paste the gateway's api-key.\n"
                       "Whoever runs the gateway issues it.")

    # 取ってくる手順 (manual_url / manual_steps) は持ちません。このゲートウェイは
    # 組織ごとに立てるもので、キーの発行画面に万人へ通用する URL が無いためです
    # (extra_field_default を空にしているのと同じ理由)。開けない URL を案内する
    # くらいなら、発行元を一言で示すほうが利用者は先へ進めます。

    uses_extra_field = True
    extra_field_label = "Endpoint URL"
    extra_field_hint = "The gateway's billing URL (https://<host>/bill/billing)"
    # 既定値は持ちません。このゲートウェイは組織ごとに立てるものなので、
    # 万人に通用する URL がありません (モジュール冒頭の説明を参照)。
    extra_field_default = ""

    supports_budget = True
    currency = "JPY"

    def __init__(self, timeout: int = 20):
        self.http = HttpClient(credential_label=self.credential_label, timeout=timeout)

    def validate_extra(self, value: str) -> str:
        """エンドポイント URL に不安があるとき、確認メッセージを返します。

        [B-04] このアプリの validate_extra() の呼び出し規約 (base.py 参照) は
        「空文字なら OK、非空文字なら『このまま保存しますか？』の Yes/No 確認を
        出す」という UI 前提で、Yes を選べば保存を止められません
        (base.py の docstring も「ここで弾き切ろうとしないこと」としており、
        契約上ここだけで保存を強制ブロックすることはできません)。
        そのため https 以外のスキームについては、ここで警告するだけでなく
        fetch_usage() 側でも無条件にリクエストを止め、API キーが実際に
        平文経路や無関係な宛先へ出ていくこと自体を防ぎます
        (config.json を直接書き換えられた場合や、この対策より前に
        保存済みだった値にも効きます)。
        """
        value = (value or "").strip()
        if not value:
            return ""

        parsed = urlparse(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return t(
                "The endpoint is not in the form https://<host>/... \n"
                "The API key is sent to this URL as-is, so over http:// "
                "anyone listening in can read it.\n"
                "Even if you save this, sending is blocked at fetch time "
                "and the fetch fails.\n"
                "Change it to a URL that starts with https://."
            )

        host = (parsed.hostname or "").lower()

        # 許可ドメインの外は、確認を出しても Yes を押されれば通ってしまう。
        # ここは「送信されると API キーが第三者へ出る」ケースなので、
        # https 以外と同じく fetch_usage() 側でブロックすることを明示する。
        if not _is_allowed_host(host):
            return t(
                "\"{host}\" is outside the domains you allow ({allowed}).\n"
                "The API key is sent to this URL as-is, so that host "
                "would receive your API key.\n"
                "Even if you save this, sending is blocked at fetch time "
                "and the fetch fails.\n"
                "Change the endpoint, or add the domain to "
                "aoai_allowed_hosts in the settings file (config.json).",
                host=parsed.hostname, allowed=", ".join(_allowed_hosts),
            )

        # 制限が未設定なら、ここが唯一の歯止め。送信先を目で確かめてもらう。
        if not is_restricted():
            return t(
                "The API key will be sent to \"{host}\" as-is.\n"
                "Save it only if that host is a gateway run by you or your "
                "organisation.\n"
                "To refuse every other destination from now on, list your "
                "domains in aoai_allowed_hosts in the settings file "
                "(config.json).\n"
                "Save it anyway?",
                host=parsed.hostname,
            )
        return ""

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        api_key = (credential or "").strip()
        if not api_key:
            raise UsageError(
                t('{credential} is not set. Set it from "Edit".',
                  credential=t(self.credential_label)),
                auth_error=True,
            )

        url = (organization_id or "").strip()
        if not url:
            raise UsageError(t(
                'The endpoint URL is not set. Set it from "Edit".\n'
                "It is the billing URL of your Azure OpenAI gateway "
                "(https://<host>/bill/billing)."
            ))

        # [B-04] validate_extra() の確認は Yes を選べば通ってしまう
        # (保存前の警告に過ぎない) ため、API キーが実際に送信される直前の
        # ここで https 以外を無条件にブロックする。保存時点のチェックを
        # すり抜けた値 (手編集された config.json や、対策導入前に
        # 保存済みだった値) にも効く、最後の砦。
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise UsageError(t(
                "The endpoint is not https://, so sending the API key was blocked.\n"
                'Change the endpoint URL from "Edit" to one that starts with '
                "https://.\n"
                "Configured value: {url}",
                url=url,
            ))

        # 許可ドメインの外へは送らない。ここが実際の防御線で、
        # 保存時の確認 (Yes で通せる) をすり抜けた値もここで止まる。
        host = parsed.hostname.lower()
        if not _is_allowed_host(host):
            raise UsageError(t(
                "The endpoint host \"{host}\" is outside the domains you allow "
                "({allowed}), so sending the API key was blocked.\n"
                'Check the endpoint URL from "Edit", or add the domain to '
                "aoai_allowed_hosts in the settings file (config.json).",
                host=parsed.hostname, allowed=", ".join(_allowed_hosts),
            ))

        text = self.http.get_text(url, {"api-key": api_key}, t("the cost"))
        return self.parse_billing_text(text)

    # ---------------- レスポンス解釈 ----------------

    @classmethod
    def _parse_row(cls, line: str):
        """データ行を (モデル名, 入力トークン, 出力トークン, count, spend_jpy) にします。

        書式が違う行 (ヘッダや空行) では None を返します。
        """
        parts = line.split()
        if len(parts) < _NUMERIC_TAIL + 1:
            return None

        numbers = [_to_number(p) for p in parts[-_NUMERIC_TAIL:]]
        if any(n is None for n in numbers):
            return None

        model = " ".join(parts[:-_NUMERIC_TAIL])
        if not model:
            return None

        input_tokens, output_tokens, _spend, count, spend_jpy = numbers
        return model, input_tokens, output_tokens, count, spend_jpy

    @classmethod
    def parse_billing_text(cls, text: str) -> Dict[str, Any]:
        rows: List[Tuple] = []
        total = None
        saw_header = False

        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            if _HEADER_RE.search(stripped):
                saw_header = True
                continue

            total_match = _TOTAL_RE.search(stripped)
            if total_match:
                total = _to_number(total_match.group(1))
                continue

            row = cls._parse_row(stripped)
            if row is not None:
                rows.append(row)

        if not saw_header and not rows and total is None:
            # 0円ならデータ行が無いことはありうるが、ヘッダも合計も無いのは書式変更。
            # ここで 0 円と報告すると「使っていない」と誤解させる。
            preview = " / ".join((text or "").split("\n")[:3])[:200]
            logger.error("課金額のレスポンスを解釈できませんでした: %s", preview)
            raise UsageError(t(
                "The cost format was not recognised "
                "(the API may have changed). Received: {preview}",
                preview=preview or t("(empty)"),
            ))

        if total is None:
            # Total Cost 行が無い場合はモデル別の合計で埋める
            total = sum(row[4] for row in rows)
            logger.warning("Total Cost 行が見つからないため、モデル別の合計で代用します。")

        metrics = [money_metric("total", t("Cumulative cost"), total, currency="JPY")]

        # 使っていないモデルの 0 円行は情報量が無いので出さない。
        # 金額の大きい順に並べて、効いているモデルが上に来るようにする。
        for model, input_tokens, output_tokens, count, spend_jpy in sorted(
            rows, key=lambda r: r[4], reverse=True
        ):
            if spend_jpy <= 0:
                continue
            metrics.append(money_metric(
                f"model:{model}",
                t("{model} ({count} calls)", model=model, count=f"{count:,.0f}"),
                spend_jpy, currency="JPY",
            ))

        return build_result(
            metrics,
            total_jpy=total,
            models={row[0]: row[4] for row in rows},
            raw=text,
        )
