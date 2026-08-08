"""取得先 (プロバイダ) のレジストリ。

新しい取得先を足すときは、このファイルで import して _ORDER に並べるだけで
一覧・ダイアログ・自動更新の対象に入ります。UI 側は触らないでください。

使わなくなった取得先は _ORDER から _RETIRED へ移します (削除ではなく移動)。
一覧からは消えますが ID からは引けるため、設定ファイルに残っている
古いアカウントを別の取得先に化けさせずに済みます。

import を明示的に書いているのは、PyInstaller の静的解析でプロバイダを
拾わせるためです (動的 import にすると exe に含まれず、実行時に消えます)。
"""

import logging
from typing import List

from services.providers.anthropic_cost import AnthropicCostProvider
from services.providers.antigravity import AntigravityProvider
from services.providers.aoai_cost import AoaiCostProvider
from services.providers.base import (  # noqa: F401  (外部から使う共通シンボル)
    AMOUNT, AUTH_COOKIE, AUTH_OAUTH, AUTH_TOKEN, ENV_INSECURE_SSL, MONEY, PERCENT,
    HttpClient, UsageError, UsageProvider,
    amount_metric, apply_budget, build_result, currency_symbol, extract_cookie_header,
    format_money, insecure_ssl_enabled, money_metric, parse_cookie_header, percent_metric,
    resolve_verify, unix_to_iso,
)
from services.providers.chatgpt import ChatGPTProvider
from services.providers.claude import ClaudeProvider
from services.providers.codex import CodexProvider
from services.providers.gemini import GeminiProvider

logger = logging.getLogger(__name__)

# 既存の設定ファイルには provider が入っていないため、
# 未指定は必ず Claude として読む (そうしないと既存アカウントが行方不明になる)。
DEFAULT_PROVIDER_ID = "claude"

# 一覧・選択肢に出す順序。利用枠を先に、課金を後に置く。
_ORDER = [
    ClaudeProvider(),
    ChatGPTProvider(),
    GeminiProvider(),
    AoaiCostProvider(),
    AnthropicCostProvider(),
]

# 実装は残してあるが、一覧には出さない取得先。
#
#   codex       … 使っていないため取り下げ。実装はローカルの記録ファイルを
#                 読むだけで完結しており、そのまま動く状態。
#   antigravity … 同上。クレジット残高は取得できるが、クォータ使用率は
#                 retrieveUserQuotaSummary が 403 を返すため未達。
#
# **_ORDER に戻せばそのまま復活します。** ここに残しているのは、
# rollout JSONL の形式や agy.exe の proto スキーマといった、
# 解析しないと分からない知見が実装とテストに書かれているためです。
# 消すのは簡単ですが、調べ直すのは簡単ではありません。
_RETIRED = [
    CodexProvider(),
    AntigravityProvider(),
]

# 取り下げたものも ID からは引けるようにしておきます。
# 設定ファイルに残っている古いアカウントを、既定 (Claude) に化けさせて
# 「Claude の取得に失敗しました」と嘘の失敗を出さないためです。
_BY_ID = {provider.id: provider for provider in _ORDER + _RETIRED}


def all_providers() -> List[UsageProvider]:
    return list(_ORDER)


def get(provider_id: str) -> UsageProvider:
    """ID からプロバイダを引きます。

    未知の ID は既定 (Claude) に倒します。設定ファイルを手編集された場合や
    プロバイダを削除したあとの設定を読んだ場合に、起動できなくなるより
    既定で動いたほうが復旧しやすいためです。
    """
    provider = _BY_ID.get((provider_id or "").strip())
    if provider is not None:
        return provider

    if provider_id:
        logger.warning(
            "未知のプロバイダ '%s' が指定されました。'%s' として扱います。",
            provider_id, DEFAULT_PROVIDER_ID,
        )
    return _BY_ID[DEFAULT_PROVIDER_ID]


def is_known(provider_id: str) -> bool:
    return (provider_id or "").strip() in _BY_ID
