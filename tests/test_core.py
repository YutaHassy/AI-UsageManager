"""GUI を伴わない中核ロジックの回帰テスト。

過去に実際に起きた不具合を再発させないことを目的にしています。
実行方法:  venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import base64
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# **リポジトリ直下の原本を必ず先頭にする。** services/ と models/ は
# vscode-extension/backend/ 配下にもビルド時生成のコピーが存在する
# (build_vsix.py が固める前にそこへ複製する)。sys.path の並びによっては
# そちらを先に見つけてしまい、「原本を直したのにテストは古いコピーを見ている」
# という事故になる。コピー側が万一 sys.path に紛れ込んでいても原本を
# 優先させるため、コピー側のパスを明示的に取り除いてから原本を先頭に挿す。
_BACKEND_COPY = os.path.join(PROJECT_ROOT, "vscode-extension", "backend")
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _BACKEND_COPY]
sys.path.insert(0, PROJECT_ROOT)

# VSCode 拡張。しきい値が JavaScript 側へ戻っていないかを見るために読みます。
EXTENSION_DIR = os.path.join(PROJECT_ROOT, "vscode-extension")

from models.account import Account                                    # noqa: E402
from services import browser_profile, providers, secret_store, usage_status  # noqa: E402

# **ここで実際に読み込んだ services / models がリポジトリ直下のものであることを
# 確認する。** 何らかの理由で vscode-extension/backend/ 側のコピーが先に
# import されてしまうと、原本を直してもテストに反映されないという、
# 気づきにくい事故になるため、その場で fail-fast する。
# **services だけでなく models も見ること。** 以前はここが usage_status
# (= services) しか見ておらず、models だけがコピー側から読み込まれた場合は
# 素通りしていた。上の説明は最初から両方を挙げているのに、実装が片方だけ
# だった。tests/test_backend_rpc.py が models.account を import しており、
# pytest の収集順ではそちらが先に走るので、机上の話ではない。
for _module, _package in ((usage_status, "services"),
                          (sys.modules["models.account"], "models")):
    assert os.path.abspath(os.path.dirname(_module.__file__)) == \
        os.path.join(PROJECT_ROOT, _package), (
            f"{_package} パッケージがリポジトリ直下ではなく "
            f"{_module.__file__} から読み込まれました。"
            " vscode-extension/backend/ のコピーを見ていないか確認してください。"
        )

from services.usage_status import (                                   # noqa: E402
    LEVEL_CAUTION, LEVEL_LIMITED, LEVEL_OK, LEVEL_UNKNOWN,
)
from services.config_manager import ConfigLoadError, ConfigManager    # noqa: E402
from services.providers import (                                      # noqa: E402
    AMOUNT, AUTH_COOKIE, MONEY, PERCENT, UsageError,
    amount_metric, apply_budget, build_result, money_metric, percent_metric,
)
from services.providers.anthropic_cost import AnthropicCostProvider   # noqa: E402
from services.providers.antigravity import AntigravityProvider        # noqa: E402
from services.providers.aoai_cost import AoaiCostProvider             # noqa: E402
from services.providers import base as provider_base                 # noqa: E402
from services.providers import chatgpt as chatgpt_module             # noqa: E402
from services.providers.chatgpt import ChatGPTProvider                # noqa: E402
from services.providers.claude import ClaudeProvider                  # noqa: E402
from services.providers.codex import CodexProvider                    # noqa: E402
from services.providers import gemini                                 # noqa: E402
from services.providers.gemini import GeminiProvider, rpc_payload     # noqa: E402
from services.datetime_util import (                                  # noqa: E402
    format_datetime, get_remaining_time_str, parse_utc_to_local,
)

# **ui/styles.py は現行リポジトリに存在しない。** デスクトップGUI版が
# リポジトリ分離で削除されたため (現行の ui/ にあるのは account_dialog.py と
# session_refresher.py の2つで、styles.py は無い)。
# get_status_dot / is_limited はもともと services/usage_status.py の関数を
# 再エクスポートしていただけなので、原本から直接同じ名前で束ねる。
# scale_css / build_theme / get_progress_bar_style / get_status_color は
# Qt のスタイルシート・配色そのものを組み立てる、デスクトップ版固有の関数で
# あり、usage_status.py 側には存在しないため、これらに依存するテストは
# クラス単位・メソッド単位で @unittest.skip する (下記参照)。
from services.usage_status import is_limited, status_dot as get_status_dot  # noqa: E402


class TestAccount(unittest.TestCase):
    def test_none_values_do_not_crash(self):
        """config.json に null が入っていても落ちないこと。"""
        acc = Account(name=None, organization_id=None, cookie=None)
        self.assertEqual((acc.name, acc.organization_id, acc.cookie), ("", "", ""))

    def test_from_dict_with_explicit_nulls(self):
        acc = Account.from_dict({"name": "x", "organization_id": None, "cookie": None})
        self.assertEqual(acc.organization_id, "")

    def test_from_dict_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            Account.from_dict("こわれたエントリ")

    def test_repr_redacts_cookie(self):
        """トレースバックやログに Cookie が載らないこと。"""
        acc = Account("n", "o", "sessionKey=sk-ant-SECRET")
        self.assertNotIn("sk-ant-SECRET", repr(acc))


@unittest.skipUnless(secret_store.is_encryption_available(), "DPAPI が使えない環境")
class TestSecretStore(unittest.TestCase):
    def test_roundtrip(self):
        plain = "sessionKey=sk-ant-sid02-" + "x" * 2000
        token = secret_store.encrypt(plain)
        self.assertTrue(secret_store.is_encrypted(token))
        self.assertNotIn("sk-ant", token[:64])
        self.assertEqual(secret_store.decrypt(token), plain)

    def test_encrypt_is_idempotent(self):
        token = secret_store.encrypt("abc")
        self.assertEqual(secret_store.encrypt(token), token)

    def test_plaintext_passthrough(self):
        """暗号化前に保存された設定もそのまま読めること。"""
        self.assertEqual(secret_store.decrypt("plain-cookie"), "plain-cookie")

    def test_corrupted_token_returns_empty(self):
        self.assertEqual(secret_store.decrypt("dpapi:v1:!!!notbase64!!!"), "")
        self.assertEqual(secret_store.decrypt("dpapi:v1:" + "AAAA" * 10), "")


class TestConfigManager(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cum_test_")
        self.path = os.path.join(self.dir, "sub", "config.json")

    def _write(self, payload):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def test_missing_file_returns_empty(self):
        accounts, settings = ConfigManager(self.path).load()
        self.assertEqual(accounts, [])
        self.assertEqual(settings["auto_update_interval_minutes"], 5)

    def test_save_encrypts_cookie_on_disk(self):
        cm = ConfigManager(self.path)
        self.assertTrue(cm.save([Account("a", "o", "sessionKey=sk-ant-DUMMY")]))
        with open(self.path, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertNotIn("sk-ant-DUMMY", json.dumps(disk))
        if secret_store.is_encryption_available():
            self.assertTrue(disk["accounts"][0]["cookie"].startswith("dpapi:v1:"))

    def test_settings_are_persisted(self):
        cm = ConfigManager(self.path)
        cm.save([], {"auto_update_enabled": True, "auto_update_interval_minutes": 30})
        _, settings = ConfigManager(self.path).load()
        self.assertTrue(settings["auto_update_enabled"])
        self.assertEqual(settings["auto_update_interval_minutes"], 30)

    def test_one_broken_entry_does_not_wipe_the_others(self):
        """1件の破損で全アカウントが消えないこと。"""
        self._write({"accounts": [
            {"id": "1", "name": "生存A", "organization_id": "", "cookie": "c", "enabled": True},
            "こわれたエントリ",
            {"name": "生存B", "organization_id": None, "cookie": None},
        ]})
        cm = ConfigManager(self.path)
        accounts, _ = cm.load()
        self.assertEqual([a.name for a in accounts], ["生存A", "生存B"])
        self.assertEqual(len(cm.load_warnings), 1)

    def test_broken_json_raises_and_blocks_overwrite(self):
        """壊れたファイルを空で上書きして復旧不能にしないこと。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ broken json ")

        cm = ConfigManager(self.path)
        with self.assertRaises(ConfigLoadError):
            cm.load()
        self.assertFalse(cm.save([Account("x", "", "c")]))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken json ")

    def test_non_list_accounts(self):
        self._write({"accounts": {"a": 1}})
        cm = ConfigManager(self.path)
        accounts, _ = cm.load()
        self.assertEqual(accounts, [])
        self.assertTrue(cm.load_warnings)

    def test_bare_relative_path_does_not_raise(self):
        """ディレクトリ部を持たないパスでも os.makedirs('') で落ちないこと。"""
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            self.assertTrue(ConfigManager("bare.json").save([Account("x", "", "c")]))
        finally:
            os.chdir(cwd)

    def test_migration_from_legacy_path(self):
        import services.config_manager as cmod
        legacy = os.path.join(self.dir, "legacy.json")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"accounts": [{"id": "old", "name": "旧", "organization_id": "",
                                     "cookie": "sessionKey=sk-ant-OLD", "enabled": True}]}, f)

        original = cmod.legacy_config_path
        cmod.legacy_config_path = lambda: legacy
        try:
            cm = ConfigManager(self.path)
            self.assertTrue(cm.migrate_legacy_config_if_needed())
            accounts, _ = cm.load()
            self.assertEqual(accounts[0].cookie, "sessionKey=sk-ant-OLD")
            self.assertTrue(os.path.exists(legacy), "移行元は残すこと")
            # 2回目はスキップされる
            self.assertFalse(ConfigManager(self.path).migrate_legacy_config_if_needed())
        finally:
            cmod.legacy_config_path = original


class TestUsageParsing(unittest.TestCase):
    def setUp(self):
        self.parse = ClaudeProvider().parse_usage_response

    def _util(self, payload, key="five_hour"):
        """指定した枠の利用率を取り出すヘルパ。"""
        metrics = {m["key"]: m for m in self.parse(payload)["metrics"]}
        return metrics[key]["utilization"]

    def test_utilization_is_treated_as_percent(self):
        """0.5 は 0.5% であり 50% ではないこと (旧実装は 100 倍していた)。"""
        self.assertAlmostEqual(self._util({"five_hour": {"utilization": 0.5}}), 0.5)
        self.assertAlmostEqual(self._util({"five_hour": {"utilization": 1.0}}), 1.0)
        self.assertAlmostEqual(self._util({"five_hour": {"utilization": 34}}), 34.0)

    def test_opus_weekly_limit_is_exposed(self):
        result = self.parse({
            "five_hour": {"utilization": 34, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"utilization": 72, "resets_at": "2030-01-05T00:00:00Z"},
            "seven_day_opus": {"utilization": 99, "resets_at": "2030-01-05T00:00:00Z"},
        })
        self.assertEqual([x["key"] for x in result["metrics"]],
                         ["five_hour", "seven_day", "seven_day_opus"])
        # ステータス判定は最も逼迫した枠を見る
        self.assertAlmostEqual(result["max_utilization"], 99.0)
        # 利用枠はすべて percent 指標として出る
        self.assertEqual({x["kind"] for x in result["metrics"]}, {PERCENT})

    def test_unrecognized_payload_raises_instead_of_reporting_zero(self):
        """形式変更を「使用率0%」と偽らないこと。"""
        for payload in ({}, {"some_other_key": "value"}, {"five_hour": "not-a-dict"}):
            with self.assertRaises(UsageError):
                self.parse(payload)

    def test_message_limit_type_errors_are_contained(self):
        """messageLimit の型崩れが TypeError で素通りしないこと。"""
        for ml in ({"limit": None, "remaining": 5},
                   {"limit": 10, "remaining": "5"},
                   {"remaining": 5},
                   {"limit": 0, "remaining": 0}):
            with self.assertRaises(UsageError):
                self.parse({"messageLimit": ml})

    def test_message_limit_valid(self):
        self.assertAlmostEqual(self._util({"messageLimit": {"limit": 10, "remaining": 3}}), 70.0)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(self._util({"five_hour": {"utilization": 150}}), 100.0)
        self.assertEqual(self._util({"five_hour": {"utilization": -5}}), 0.0)


class TestAuthErrorFlag(unittest.TestCase):
    """資格情報の失効を他のエラーと区別できること (自動再ログインの起点になる)。"""

    def test_default_is_not_auth_error(self):
        self.assertFalse(UsageError("通信エラー").auth_error)

    def test_auth_error_is_carried(self):
        err = UsageError("認証エラー (403)", auth_error=True)
        self.assertTrue(err.auth_error)
        self.assertEqual(str(err), "認証エラー (403)")


class TestMetrics(unittest.TestCase):
    """指標の3種類 (利用率 / 金額 / 数量) が正しく組み立てられること。

    金額は上限が無いので、ゲージ (utilization) を持たせてはいけません。
    0-100% のバーに載せると「まだ余裕がある」と嘘をつくことになります。
    """

    def test_percent_metric_is_clamped_and_gauged(self):
        metric = percent_metric("five_hour", "5時間", 150.0, resets_at="2030-01-01T00:00:00Z")
        self.assertEqual(metric["kind"], PERCENT)
        self.assertEqual(metric["utilization"], 100.0)
        self.assertEqual(metric["display"], "100.0%")

    def test_money_metric_has_no_gauge_without_budget(self):
        metric = money_metric("cost", "今月", 12.3456)
        self.assertEqual(metric["kind"], MONEY)
        self.assertIsNone(metric["utilization"])
        self.assertEqual(metric["display"], "$12.35")

    def test_money_metric_with_budget_is_gauged(self):
        metric = money_metric("cost", "今月", 25.0, budget=100.0)
        self.assertAlmostEqual(metric["utilization"], 25.0)

    def test_money_metric_non_usd(self):
        metric = money_metric("cost", "今月", 1500, currency="JPY", period="月")
        self.assertEqual(metric["display"], "¥1,500.00 / 月")

    def test_money_metric_with_budget_shows_amounts_not_a_percentage(self):
        """上限は利用者が決めた任意の値。率だけ見せても元の金額が分からない。"""
        metric = money_metric("total", "当月コスト", 4521, currency="JPY", budget=10000)
        self.assertEqual(metric["display"], "¥4,521.00 / ¥10,000.00")
        self.assertNotIn("%", metric["display"])
        self.assertAlmostEqual(metric["utilization"], 45.21)

    def test_amount_metric_gauges_consumption_not_remainder(self):
        """残 200 / 総量 1000 は「80% 消費」として出ること。"""
        metric = amount_metric("credits", "G1クレジット", 200, total=1000)
        self.assertEqual(metric["kind"], AMOUNT)
        self.assertAlmostEqual(metric["utilization"], 80.0)
        self.assertEqual(metric["display"], "200 / 1,000")

    def test_amount_metric_without_total_has_no_gauge(self):
        metric = amount_metric("credits", "G1クレジット", 200)
        self.assertIsNone(metric["utilization"])
        self.assertEqual(metric["display"], "200")

    def test_build_result_max_utilization_is_none_when_nothing_gauged(self):
        """金額だけの取得先で 0% と報告しないこと。"""
        result = build_result([money_metric("cost", "今月", 5.0)])
        self.assertIsNone(result["max_utilization"])

    def test_build_result_picks_the_worst_gauge(self):
        result = build_result([
            percent_metric("a", "A", 10.0),
            money_metric("cost", "今月", 5.0),
            percent_metric("b", "B", 90.0),
        ])
        self.assertAlmostEqual(result["max_utilization"], 90.0)


class TestAoaiBillingParsing(unittest.TestCase):
    """社内 AOAI ゲートウェイのテキスト表を解釈できること。

    REAL_RESPONSE は実際のエンドポイントから取得した応答そのままです。
    """

    REAL_RESPONSE = (
        "                     model  input_tokens  output_tokens       spend  count   spend_jpy\n"
        " azure_ai/claude-haiku-4-5       3260956          14442  115.390096     40  138.468115\n"
        "azure_ai/claude-sonnet-4-5       9369862          80694 1280.528688    114 1536.634426\n"
        "           claude-opus-4-8             0              0    0.000000      1    0.000000\n"
        "         claude-sonnet-4-6             0              0    0.000000      1    0.000000\n"
        "           claude-sonnet-5             0              0    0.000000      1    0.000000\n"
        "Total Cost 1675 JPY\n"
    )

    def setUp(self):
        # [B-04] 送信を許すホストの範囲 (_allowed_hosts) はモジュール変数。
        # 現行版では特定組織のドメインを既定で焼き込まず、利用者が
        # aoai_allowed_hosts 設定で申告する形に変わっている (公開にあたって
        # 社内ドメイン honda.com のハードコードを外した — services/providers/
        # aoai_cost.py の冒頭コメントを参照)。テスト間で状態が漏れないよう、
        # 毎回「制限なし」から始めて、終わったら元に戻す。
        from services.providers import aoai_cost as aoai_cost_mod
        self.aoai_cost_mod = aoai_cost_mod
        self._saved_allowed_hosts = list(aoai_cost_mod._allowed_hosts)
        aoai_cost_mod.configure({})

    def tearDown(self):
        self.aoai_cost_mod._allowed_hosts = self._saved_allowed_hosts

    def parse(self, text):
        return AoaiCostProvider.parse_billing_text(text)

    def test_real_response(self):
        result = self.parse(self.REAL_RESPONSE)
        metrics = result["metrics"]

        # 先頭は累計。一覧の要約にこれが出る。
        self.assertEqual(metrics[0]["key"], "total")
        self.assertEqual(metrics[0]["kind"], MONEY)
        self.assertAlmostEqual(metrics[0]["value"], 1675.0)
        self.assertEqual(metrics[0]["display"], "¥1,675.00")

        # 0円のモデルは出さない (情報量が無い)
        self.assertEqual(
            [m["key"] for m in metrics[1:]],
            ["model:azure_ai/claude-sonnet-4-5", "model:azure_ai/claude-haiku-4-5"],
            "金額の大きい順に並ぶこと",
        )
        self.assertEqual(metrics[1]["label"], "azure_ai/claude-sonnet-4-5 (114回)")
        self.assertAlmostEqual(metrics[1]["value"], 1536.634426)

        # spend 列ではなく spend_jpy 列を使うこと
        self.assertNotAlmostEqual(metrics[1]["value"], 1280.528688)

        # 課金額に上限は無いのでゲージは出さない
        self.assertIsNone(result["max_utilization"])

    def test_zero_usage_is_not_an_error(self):
        """使っていない状態を「書式変更」と誤判定しないこと。"""
        text = (
            "     model  input_tokens  output_tokens     spend  count  spend_jpy\n"
            "Total Cost 0 JPY\n"
        )
        result = self.parse(text)
        self.assertAlmostEqual(result["metrics"][0]["value"], 0.0)
        self.assertEqual(len(result["metrics"]), 1)

    def test_missing_total_line_falls_back_to_the_sum(self):
        text = (
            "  model  input_tokens  output_tokens     spend  count  spend_jpy\n"
            "  gpt-x        10             20      1.000000      3   11.500000\n"
            "  gpt-y        10             20      1.000000      3    0.500000\n"
        )
        self.assertAlmostEqual(self.parse(text)["metrics"][0]["value"], 12.0)

    def test_model_name_containing_spaces(self):
        """列は右から数えるので、モデル名に空白が入っても壊れないこと。"""
        text = (
            "  model  input_tokens  output_tokens     spend  count  spend_jpy\n"
            "  my model v2        10             20      1.000000      3    9.000000\n"
            "Total Cost 9 JPY\n"
        )
        result = self.parse(text)
        self.assertEqual(result["metrics"][1]["key"], "model:my model v2")

    def test_comma_separated_total(self):
        text = (
            "  model  input_tokens  output_tokens     spend  count  spend_jpy\n"
            "Total Cost 1,234,567 JPY\n"
        )
        self.assertAlmostEqual(self.parse(text)["metrics"][0]["value"], 1234567.0)

    def test_unrecognized_payload_raises_instead_of_reporting_zero_yen(self):
        """書式変更を「0円」と偽らないこと。"""
        for text in ("", "   ", "<html>Access Denied</html>", "unexpected output"):
            with self.subTest(text=text):
                with self.assertRaises(UsageError):
                    self.parse(text)

    def test_empty_credential_is_an_auth_error(self):
        with self.assertRaises(UsageError) as ctx:
            AoaiCostProvider().fetch_usage("")
        self.assertTrue(ctx.exception.auth_error)

    def test_non_https_endpoint_is_flagged(self):
        """[B-04] https 以外は確認メッセージを返すこと (保存自体のブロックは fetch_usage 側)。

        許可ホストを1つ設定した状態 (= 制限あり) にして確かめる。
        制限なしの状態は test_unrestricted_host_still_asks_for_confirmation 側で扱う。
        """
        self.aoai_cost_mod.configure({"aoai_allowed_hosts": "dev-aoai-api-all.jpn.mds.honda.com"})
        provider = AoaiCostProvider()
        self.assertTrue(provider.validate_extra(
            "http://dev-aoai-api-all.jpn.mds.honda.com/bill/billing"
        ))
        self.assertFalse(provider.validate_extra(
            "https://dev-aoai-api-all.jpn.mds.honda.com/bill/billing"
        ))
        self.assertFalse(provider.validate_extra(""))

    def test_unrestricted_host_still_asks_for_confirmation(self):
        """[B-04] aoai_allowed_hosts が未設定 (=制限なし) のとき、現行版は
        特定ドメインを既定として焼き込まない (公開にあたって社内ドメインの
        ハードコードを外した)。そのため「既定ホストだから確認不要」という
        免除は無くなり、どんな https ホストでも一度は確認を求めること。
        """
        provider = AoaiCostProvider()
        warning = provider.validate_extra("https://some-gateway.example.jp/bill/billing")
        self.assertTrue(warning)
        self.assertIn("some-gateway.example.jp", warning)

    def test_host_within_the_configured_allowlist_passes_silently(self):
        """[B-04] aoai_allowed_hosts に申告したドメインの内側は、確認なしで通ること。

        別の Azure リソース (別ゲートウェイ) へ切り替える運用を壊さないための逃げ道。
        """
        self.aoai_cost_mod.configure({"aoai_allowed_hosts": "honda.com"})
        provider = AoaiCostProvider()
        self.assertFalse(provider.validate_extra("https://other-gw.honda.com/bill/billing"))
        self.assertFalse(provider.validate_extra(
            "https://dev-aoai-api-all.jpn.mds.honda.com/other/path"
        ))

    def test_external_host_endpoint_is_never_sent(self):
        """[B-04] 申告した許可ドメインの外へは API キーを送らないこと。

        保存時の確認は Yes で通せてしまうため、実送信の直前で止める。
        """
        self.aoai_cost_mod.configure({"aoai_allowed_hosts": "honda.com"})
        provider = AoaiCostProvider()
        # 保存時点でも警告は出る
        warning = provider.validate_extra("https://internal.example.jp/bill/billing")
        self.assertTrue(warning)
        self.assertIn("internal.example.jp", warning)
        # そして実際には送信されない
        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage("dummy-key", "https://internal.example.jp/bill/billing")
        self.assertIn("internal.example.jp", str(ctx.exception))

    def test_lookalike_domain_is_not_treated_as_internal(self):
        """[B-04] evilhonda.com のような紛らわしいホストを許可ドメイン扱いしないこと。

        許可判定を単純な部分一致で書くとここが通ってしまうため、
        境界としてテストに残す。
        """
        self.aoai_cost_mod.configure({"aoai_allowed_hosts": "honda.com"})
        from services.providers.aoai_cost import _is_allowed_host

        provider = AoaiCostProvider()
        with self.assertRaises(UsageError):
            provider.fetch_usage("dummy-key", "https://evilhonda.com/bill/billing")

        self.assertFalse(_is_allowed_host("evilhonda.com"))
        # サブドメインは許可ドメインの内側なので、こちらは許可される
        self.assertTrue(_is_allowed_host("a.b.honda.com"))
        self.assertTrue(_is_allowed_host("DEV-AOAI-API-ALL.JPN.MDS.HONDA.COM"))

    def test_non_https_endpoint_is_never_sent(self):
        """[B-04] 保存時の確認は Yes で通せてしまうため、実送信の直前でも
        無条件にブロックすること (config.json を手編集された場合の保険)。

        スキームのチェックはホストの許可リストより先に行われるため、
        制限が未設定の状態でも効くこと。
        """
        provider = AoaiCostProvider()
        with self.assertRaises(UsageError):
            provider.fetch_usage(
                "dummy-key", "http://dev-aoai-api-all.jpn.mds.honda.com/bill/billing"
            )


class TestCodexRateLimitParsing(unittest.TestCase):
    """Codex CLI の rollout JSONL からレート上限を読めること。

    命名の層が3つあり、混同すると静かに None になる:
      rollout JSONL … used_percent / window_minutes / resets_at  ← これを使う
      app-server    … usedPercent / windowDurationMins / resetsAt
      /wham/usage   … used_percent / limit_window_seconds / reset_after_seconds
    さらに resets_in_seconds は現行バージョンに存在しない。
    """

    SNAPSHOT = {
        "limit_id": "codex",
        "limit_name": None,
        "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1704069000},
        "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": 1704470400},
        "credits": None,
        "plan_type": "plus",
    }

    def test_primary_and_secondary_become_percent_metrics(self):
        result = CodexProvider.parse_snapshot(self.SNAPSHOT)
        self.assertEqual([m["key"] for m in result["metrics"]], ["primary", "secondary"])
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 12.5)
        self.assertAlmostEqual(result["max_utilization"], 40.0)
        self.assertEqual(result["plan_type"], "plus")

    def test_window_minutes_become_readable_labels(self):
        result = CodexProvider.parse_snapshot(self.SNAPSHOT)
        self.assertEqual(result["metrics"][0]["label"], "5時間")
        self.assertEqual(result["metrics"][1]["label"], "週間")

    def test_resets_at_is_unix_seconds_converted_to_iso(self):
        """resets_at は Unix 秒。画面側は ISO8601 文字列を前提にしている。"""
        result = CodexProvider.parse_snapshot(self.SNAPSHOT)
        self.assertEqual(result["metrics"][0]["resets_at"], "2024-01-01T00:30:00Z")
        # ISO に直したものが画面のパーサを通ること
        self.assertIsNotNone(parse_utc_to_local(result["metrics"][0]["resets_at"]))

    def test_unknown_window_is_labelled_not_rounded_to_a_known_one(self):
        """知らない枠を既知の名前に丸めない (別の枠を誤った名前で出さない)。"""
        result = CodexProvider.parse_snapshot(
            {"primary": {"used_percent": 5.0, "window_minutes": 60}}
        )
        self.assertEqual(result["metrics"][0]["label"], "60分枠")

    def test_missing_window_fields_do_not_crash(self):
        result = CodexProvider.parse_snapshot({"primary": {"used_percent": 7.0}})
        self.assertEqual(result["metrics"][0]["label"], "レート上限")
        self.assertIsNone(result["metrics"][0]["resets_at"])

    def test_both_naming_layers_parse_identically(self):
        """app-server 層 (camelCase) で書かれていても同じ結果になること。

        名前を発明しているのではなく、実在する2層の別名を許容している。
        片方しか読めないと、層が違うだけで取得できなくなる。
        """
        snake = CodexProvider.parse_snapshot(
            {"primary": {"used_percent": 12.5, "window_minutes": 300,
                         "resets_at": 1704069000}}
        )
        camel = CodexProvider.parse_snapshot(
            {"primary": {"usedPercent": 12.5, "windowDurationMins": 300,
                         "resetsAt": 1704069000}}
        )
        self.assertEqual(snake["metrics"], camel["metrics"])
        self.assertEqual(camel["metrics"][0]["label"], "5時間")
        self.assertAlmostEqual(camel["metrics"][0]["utilization"], 12.5)

    def test_diagnose_reports_keys_but_never_values(self):
        """診断に会話の内容が混ざらないこと。記録ファイルには本文が入る。"""
        home = tempfile.mkdtemp(prefix="cum_codex_diag_")
        nested = os.path.join(home, "sessions", "2026", "08", "05")
        os.makedirs(nested)
        secret = "これは社外秘のプロンプト本文です"
        with open(os.path.join(nested, "rollout-x.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "prompt": secret,
                "rate_limits": {"primary": {"usedPercentTYPO": 1.0}}}},
                ensure_ascii=False) + "\n")

        original = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = home
        try:
            report = CodexProvider.diagnose()
            self.assertNotIn(secret, report)
            # 直すのに必要なキー名は出ていること
            # diagnose() の出力は意図的に英語のまま (t() を通さない) にしてある
            # ── 転送先の開発者が読むための塊で、表示言語で語が変わると
            # 報告を突き合わせられなくなるため (services/providers/codex.py
            # の diagnose() docstring を参照)。
            self.assertIn("usedPercentTYPO", report)
            self.assertIn("keys of rate_limits", report)
            self.assertIn("lines containing token_count: 1", report)

            # 失敗メッセージにも診断が載り、本文は載らないこと
            with self.assertRaises(UsageError) as ctx:
                CodexProvider().fetch_usage()
            self.assertNotIn(secret, str(ctx.exception))
            self.assertIn("usedPercentTYPO", str(ctx.exception))
        finally:
            if original is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original

    def test_unrecognized_snapshot_raises_instead_of_reporting_zero(self):
        for snapshot in ({}, {"primary": "not-a-dict"},
                         {"primary": {"used_percent": "たくさん"}}):
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(UsageError):
                    CodexProvider.parse_snapshot(snapshot)

    def test_latest_snapshot_wins_and_broken_lines_are_skipped(self):
        directory = tempfile.mkdtemp(prefix="cum_codex_")
        path = os.path.join(directory, "rollout-test.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 1.0, "window_minutes": 300}}}}) + "\n")
            f.write("{ 壊れた行 token_count\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "other"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "rate_limits": {"primary": {"used_percent": 99.0, "window_minutes": 300}}}}) + "\n")

        snapshot = CodexProvider._latest_snapshot(path)
        self.assertAlmostEqual(snapshot["primary"]["used_percent"], 99.0)

    def test_rollout_files_are_found_under_year_month_day_folders(self):
        """実際の構造は sessions/YYYY/MM/DD/rollout-*.jsonl。

        階層を決め打ちすると、Codex 側が構造を変えたときに黙って0件になる。
        """
        home = tempfile.mkdtemp(prefix="cum_codex_home_")
        nested = os.path.join(home, "sessions", "2026", "08", "05")
        os.makedirs(nested)
        newer = os.path.join(nested, "rollout-2026-08-05T10-00-00-abc.jsonl")
        with open(newer, "w", encoding="utf-8") as f:
            f.write("{}\n")
        # 階層が違っても拾えること
        shallow = os.path.join(home, "sessions", "rollout-flat.jsonl")
        with open(shallow, "w", encoding="utf-8") as f:
            f.write("{}\n")

        original = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = home
        try:
            from services.providers import codex as codex_mod
            found = codex_mod.rollout_files()
            self.assertEqual(len(found), 2)
            self.assertIn(newer, found)
            self.assertIn(shallow, found)
        finally:
            if original is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original

    def test_missing_sessions_dir_is_an_auth_error_with_instructions(self):
        """未ログインを「エラー」で終わらせず、何をすればよいか伝えること。"""
        original = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="cum_codex_empty_")
        try:
            with self.assertRaises(UsageError) as ctx:
                CodexProvider().fetch_usage()
            self.assertTrue(ctx.exception.auth_error)
            self.assertIn("プロンプト", str(ctx.exception))
        finally:
            if original is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = original


class TestChatGPTUsageParsing(unittest.TestCase):
    """ChatGPT (Web) の /backend-api/wham/usage レスポンスを解釈できること。

    DevTools で実測したレスポンス (plan_type: "plus") を基にしている。
    primary_window / secondary_window は Codex の primary/secondary と
    同じ形だが、枠の長さが window_minutes ではなく limit_window_seconds
    (秒) で来る点が違う。
    """

    USAGE_RESPONSE = {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 41,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 458225,
                "reset_at": 1786431036,
            },
            "secondary_window": None,
        },
        "credits": {
            "has_credits": False,
            "unlimited": False,
            "overage_limit_reached": False,
            "balance": "0",
        },
    }

    def test_primary_window_becomes_a_percent_metric(self):
        result = ChatGPTProvider.parse_usage_response(self.USAGE_RESPONSE)
        self.assertEqual([m["key"] for m in result["metrics"]], ["primary"])
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 41.0)
        self.assertEqual(result["plan_type"], "plus")

    def test_limit_window_seconds_becomes_a_readable_label(self):
        """604800秒 (7日) は「週間」。window_minutes ではなく秒で来る点に注意。"""
        result = ChatGPTProvider.parse_usage_response(self.USAGE_RESPONSE)
        self.assertEqual(result["metrics"][0]["label"], "週間")

    def test_reset_at_is_unix_seconds_converted_to_iso(self):
        result = ChatGPTProvider.parse_usage_response(self.USAGE_RESPONSE)
        resets_at = result["metrics"][0]["resets_at"]
        self.assertIsNotNone(parse_utc_to_local(resets_at))

    def test_null_secondary_window_is_skipped_without_error(self):
        result = ChatGPTProvider.parse_usage_response(self.USAGE_RESPONSE)
        self.assertNotIn("secondary", [m["key"] for m in result["metrics"]])

    def test_secondary_window_is_parsed_when_present(self):
        response = dict(self.USAGE_RESPONSE)
        response["rate_limit"] = dict(response["rate_limit"])
        response["rate_limit"]["secondary_window"] = {
            "used_percent": 5.0, "limit_window_seconds": 18000, "reset_at": 1704069000,
        }
        result = ChatGPTProvider.parse_usage_response(response)
        self.assertEqual([m["key"] for m in result["metrics"]], ["primary", "secondary"])
        self.assertEqual(result["metrics"][1]["label"], "5時間")

    def test_unknown_window_length_is_labelled_not_rounded_to_a_known_one(self):
        response = {"rate_limit": {"primary_window": {
            "used_percent": 5.0, "limit_window_seconds": 3600}}}
        result = ChatGPTProvider.parse_usage_response(response)
        self.assertEqual(result["metrics"][0]["label"], "1時間枠")

    def test_credits_balance_is_reported_only_when_has_credits_is_true(self):
        """has_credits が false のときは残高0でも指標を出さない (この形の全体を無効枠として扱う)。"""
        result = ChatGPTProvider.parse_usage_response(self.USAGE_RESPONSE)
        self.assertNotIn("credits", [m["key"] for m in result["metrics"]])

        response = dict(self.USAGE_RESPONSE)
        response["rate_limit"] = None
        response["credits"] = {"has_credits": True, "balance": "12.5"}
        result = ChatGPTProvider.parse_usage_response(response)
        self.assertEqual(result["metrics"][0]["key"], "credits")
        self.assertEqual(result["metrics"][0]["kind"], AMOUNT)
        self.assertAlmostEqual(result["metrics"][0]["value"], 12.5)

    def test_unrecognized_response_raises_instead_of_reporting_zero(self):
        for response in ({}, {"rate_limit": "not-a-dict"},
                         {"rate_limit": {"primary_window": {"used_percent": "たくさん"}}}):
            with self.subTest(response=response):
                with self.assertRaises(UsageError):
                    ChatGPTProvider.parse_usage_response(response)

    def test_access_token_exchange_uses_cookie_and_returns_bearer_token(self):
        """Cookie を /api/auth/session に送り accessToken を得る二段階目の入口。

        backend-api は Cookie 単体では通らず、フロントが明示的に
        Authorization ヘッダを付けていた (DevTools で確認済み) ための処理。
        """
        provider = ChatGPTProvider()
        calls = []

        def fake_get_json(url, headers, what, params=None):
            calls.append((url, headers.get("Cookie"), headers.get("Authorization")))
            if url == "https://chatgpt.com/api/auth/session":
                return {"accessToken": "fake-jwt-token"}
            return dict(TestChatGPTUsageParsing.USAGE_RESPONSE)

        provider.http.get_json = fake_get_json
        result = provider.fetch_usage("__Secure-next-auth.session-token=abc123")

        self.assertEqual(calls[0][0], "https://chatgpt.com/api/auth/session")
        self.assertIn("abc123", calls[0][1])
        self.assertEqual(calls[1][2], "Bearer fake-jwt-token")
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 41.0)

    def test_missing_access_token_is_an_auth_error(self):
        provider = ChatGPTProvider()
        provider.http.get_json = lambda url, headers, what, params=None: {}
        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage("sometoken")
        self.assertTrue(ctx.exception.auth_error)

    def test_empty_credential_is_an_auth_error(self):
        provider = ChatGPTProvider()
        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage("")
        self.assertTrue(ctx.exception.auth_error)


class TestChatGPTPastedAccessToken(unittest.TestCase):
    """accessToken を直接貼り付けたときの経路。

    Google アカウントで作った ChatGPT アカウントは、アプリ内ブラウザから
    ログインできない (Google が埋め込みブラウザからのサインインを拒否する)。
    その逃げ道として、普段使いのブラウザで /api/auth/session を開いて
    accessToken をコピーし、Cookie 欄にそのまま貼る運用を認めている。
    """

    @staticmethod
    def make_jwt(exp=None) -> str:
        def segment(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        payload = {"sub": "user-1"}
        if exp is not None:
            payload["exp"] = exp
        return f"{segment({'alg': 'RS256'})}.{segment(payload)}.notarealsignature"

    def test_pasted_token_is_used_directly_without_the_session_exchange(self):
        provider = ChatGPTProvider()
        calls = []

        def fake_get_json(url, headers, what, params=None):
            calls.append((url, headers.get("Authorization")))
            return dict(TestChatGPTUsageParsing.USAGE_RESPONSE)

        provider.http.get_json = fake_get_json
        token = self.make_jwt(exp=time.time() + 3600)
        result = provider.fetch_usage(token)

        # /api/auth/session を叩かない (Cookie を持っていないので叩いても 401 になる)
        self.assertEqual([url for url, _ in calls], ["https://chatgpt.com/backend-api/wham/usage"])
        self.assertEqual(calls[0][1], f"Bearer {token}")
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 41.0)

    def test_expired_token_is_an_auth_error_before_any_request(self):
        """失効を先に伝える。401 の汎用メッセージでは貼り直せばよいと分からない。"""
        provider = ChatGPTProvider()

        def fail(*args, **kwargs):
            self.fail("失効済みトークンで通信してはいけません")

        provider.http.get_json = fail
        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage(self.make_jwt(exp=time.time() - 60))
        self.assertTrue(ctx.exception.auth_error)

    def test_token_without_exp_is_sent_rather_than_rejected(self):
        """有効期限を読めないことは、失効の証拠にならない。判断は OpenAI に委ねる。"""
        provider = ChatGPTProvider()
        provider.http.get_json = lambda url, headers, what, params=None: dict(
            TestChatGPTUsageParsing.USAGE_RESPONSE
        )
        result = provider.fetch_usage(self.make_jwt())
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 41.0)

    def session_json(self, **overrides) -> str:
        """/api/auth/session が返す形。実物と同じキー構成にしてある。"""
        payload = {
            "user": {"id": "user-1", "email": "someone@example.com"},
            "expires": "2026-11-04T01:20:35.342Z",
            "accessToken": self.make_jwt(exp=time.time() + 3600),
            # 実物は JWE (dir/A256GCM)。JWT と違い "." で5つに割れる。
            "sessionToken": "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..aXYxMjM.Y2lwaGVy.dGFn",
        }
        payload.update(overrides)
        return json.dumps(payload)

    def test_whole_session_json_can_be_pasted_as_is(self):
        """「どこがアクセストークンか分からない」を起こさないこと。

        目視で探させると隣の項目を掴む・引用符を巻き込むといった失敗が必ず出る。
        画面に出たものを丸ごと貼れば済むようにしてある。
        """
        provider = ChatGPTProvider()
        normalized = provider.normalize_credential(self.session_json())

        # sessionToken を優先し、Cookie の形で保存する。
        # これなら accessToken が切れても既存の交換経路で取り直せる。
        self.assertTrue(normalized.startswith("__Secure-next-auth.session-token="))
        self.assertEqual(provider.validate_credential(normalized), "")

    def test_only_the_needed_part_is_kept_from_the_pasted_json(self):
        """貼り付け元にはメールアドレスや別の資格情報が混ざっている。"""
        provider = ChatGPTProvider()
        pasted = self.session_json()
        normalized = provider.normalize_credential(pasted)

        self.assertNotIn("someone@example.com", normalized)
        self.assertNotIn("accessToken", normalized)
        self.assertLess(len(normalized), len(pasted))

    def test_access_token_is_used_when_the_json_has_no_session_token(self):
        provider = ChatGPTProvider()
        token = self.make_jwt(exp=time.time() + 3600)
        payload = json.loads(self.session_json(accessToken=token))
        payload.pop("sessionToken")

        self.assertEqual(provider.normalize_credential(json.dumps(payload)), token)

    def test_truncated_json_falls_back_to_whatever_is_intact(self):
        """ブラウザからのコピーが途中で切れることがある。

        JSON として読めなくても、閉じ引用符まで揃っている項目は拾う。
        末尾が切れた場合、優先したい sessionToken は壊れているが
        手前の accessToken は無事なので、そちらで通す。
        """
        provider = ChatGPTProvider()
        token = self.make_jwt(exp=time.time() + 3600)
        truncated = self.session_json(accessToken=token)[:-20]

        self.assertEqual(provider.normalize_credential(truncated), token)

    def test_json_without_any_token_is_reported_rather_than_saved(self):
        """未ログインのブラウザで開くとトークンを含まない結果が出る。"""
        provider = ChatGPTProvider()
        empty = json.dumps({"user": None, "expires": None})
        normalized = provider.normalize_credential(empty)
        self.assertEqual(normalized, empty)
        self.assertIn("取り出せませんでした", provider.validate_credential(normalized))

    def test_a_bare_token_is_left_alone(self):
        """既に値だけを貼った人の入力を壊さないこと。"""
        provider = ChatGPTProvider()
        token = self.make_jwt(exp=time.time() + 3600)
        self.assertEqual(provider.normalize_credential(token), token)

    def test_manual_fallback_is_offered_and_carries_its_own_url_and_steps(self):
        """救済導線が有ることと、UI がプロバイダから値を貰えること。

        ログイン画面にサービス固有の URL を書かない設計を守るため、
        「ボタンを出すか」も「どこを開くか」もプロバイダ側が持つ。
        """
        provider = ChatGPTProvider()
        self.assertTrue(provider.has_manual_fallback)
        self.assertTrue(provider.manual_url.startswith("https://"))
        # 手順は「全部コピーして貼る」であること。項目名を探させてはいけない。
        # manual_steps はクラス属性の原文 (英語) で、t() を通らない
        # (i18n 化で "全部コピー" ではなく英語の "Copy everything" になった)。
        self.assertIn("Copy everything", provider.manual_steps)

        # **資格情報が要る取得先は、全部これを持っていること。**
        # 以前はアプリ内ブラウザでログインさせる道があり、この導線は
        # そちらが使えない人のための逃げ道でした。その道を畳んだので、
        # **いまはこれが唯一の道です。** 持っていない取得先があると、
        # その取得先を選んだ利用者には、資格情報を手に入れる手立てが
        # 画面のどこにもありません。
        for provider in providers.all_providers():
            if provider.auth_kind != AUTH_COOKIE:
                continue
            with self.subTest(provider=provider.id):
                self.assertTrue(provider.has_manual_fallback,
                                "取り方の案内が無い")
                self.assertTrue(provider.manual_steps, "手順が空")

    def test_validate_credential_catches_common_paste_mistakes(self):
        provider = ChatGPTProvider()
        valid = self.make_jwt(exp=time.time() + 3600)

        mistakes = {
            "JSON全体": '{"accessToken": "eyJhbGci.eyJzdWIi.sig"}',
            "引用符つき": f'"{valid}"',
            "無関係な文字列": "ここに貼り付け",
            "失効済み": self.make_jwt(exp=time.time() - 60),
        }
        for name, value in mistakes.items():
            with self.subTest(mistake=name):
                self.assertTrue(provider.validate_credential(value))

        # 正しい形は黙って通す
        self.assertEqual(provider.validate_credential(valid), "")
        self.assertEqual(
            provider.validate_credential(f"__Secure-next-auth.session-token={valid}"), ""
        )

    def test_cookie_header_is_not_mistaken_for_a_token(self):
        """Cookie 側の経路を壊さないこと (JWT 判定が緩いと交換を飛ばしてしまう)。"""
        provider = ChatGPTProvider()
        urls = []

        def fake_get_json(url, headers, what, params=None):
            urls.append(url)
            if url.endswith("/api/auth/session"):
                return {"accessToken": "exchanged"}
            return dict(TestChatGPTUsageParsing.USAGE_RESPONSE)

        provider.http.get_json = fake_get_json
        # Cookie 名に "eyJ..." 風の値が入っていても Cookie は Cookie
        provider.fetch_usage(f"__Secure-next-auth.session-token={self.make_jwt()}")
        self.assertEqual(urls[0], "https://chatgpt.com/api/auth/session")


class TestChatGPTPastedCredentialIsRobust(unittest.TestCase):
    """ブラウザの画面を丸ごと貼る経路が、黙ってゴミを保存しないこと。

    この経路には JSON 以外のものがいくらでも混ざって届く (整形ビューアの
    ラベル、行番号、先頭の BOM、手で選んだときに付いてくる閉じ引用符)。
    取り出しに失敗すると貼り付け全文がそのまま資格情報として保存され、
    **追加は成功するのに、以後の取得が必ず「要再ログイン」になる。**
    利用者から見ると原因がどこにも出ないので、ここを実測で固めておく。
    """

    make_jwt = staticmethod(TestChatGPTPastedAccessToken.make_jwt)

    def paste(self, **overrides) -> str:
        payload = {
            "user": {
                "id": "user-1",
                "email": "someone@example.com",
                # Google アカウントだと必ず "=" を含む。これがあるせいで
                # 「= があれば Cookie だろう」という判定が素通りしていた。
                "image": "https://lh3.googleusercontent.com/a/ACg8ocK=s96-c",
            },
            "expires": "2026-11-04T01:20:35.342Z",
            "accessToken": self.token,
        }
        payload.update(overrides)
        return json.dumps(payload, indent=2)

    def setUp(self):
        self.token = self.make_jwt(exp=time.time() + 3600)
        self.provider = ChatGPTProvider()

    def keep(self, pasted: str) -> str:
        return self.provider.normalize_credential(pasted)

    def test_junk_in_front_of_the_json_no_longer_swallows_the_token(self):
        """先頭に1文字でも余計なものが付くと全滅していた経路。

        以前は _extract_session_token が `startswith("{")` で即座に諦めて
        いたため、途中切れを救うための正規表現もろとも無効になっていた。
        """
        body = self.paste()
        for label, pasted in (
            ("整形ビューアのラベル", "Pretty-print\n" + body),
            ("先頭の BOM", "\ufeff" + json.dumps(json.loads(body))),
            ("タブ見出し", "JSON  生データ  ヘッダー\n" + body),
        ):
            with self.subTest(label):
                self.assertEqual(self.keep(pasted), self.token)

    def test_a_hand_copied_token_drops_its_closing_quote_and_comma(self):
        """`eyJ....",` の形。JWT 判定を素通りし、警告ゼロで 401 になっていた。"""
        self.assertEqual(self.keep('%s",' % self.token), self.token)

    def test_a_paste_with_no_token_is_reported_even_without_a_leading_brace(self):
        """ログインしていないブラウザの画面を貼ったとき、黙って保存しない。"""
        pasted = "Pretty-print\n" + json.dumps({"WARNING_BANNER": "DO NOT SHARE"})
        kept = self.keep(pasted)
        self.assertNotEqual(kept, "")
        self.assertTrue(self.provider.validate_credential(kept),
                        "取り出せなかった貼り付けが無警告で保存される")

    def test_a_pasted_json_is_not_accepted_as_a_cookie(self):
        """"=" が1つあるだけで Cookie 扱いしないこと。

        貼り付け元には必ず "=" が混ざっている (上の user.image を参照)。
        ここが緩いと、JSON 全文が Cookie ヘッダに載って送られる。
        """
        self.assertFalse(chatgpt_module._looks_like_cookie(self.paste()))
        self.assertTrue(chatgpt_module._looks_like_cookie("a=1; b=2"))
        self.assertTrue(chatgpt_module._looks_like_cookie(
            "__Secure-next-auth.session-token=%s" % self.token))

    def test_a_credential_with_newlines_never_reaches_the_network(self):
        """改行を含む値を送ると http.client が UsageError の外へ例外を投げる。

        そうなると画面には「予期しないエラー」としか出ず、原因が残らない。
        通信の前に止めて、貼り直しを促すところまでを固定する。
        """
        def fail(*args, **kwargs):
            self.fail("この値で通信してはいけません")

        self.provider.http.get_json = fail
        with self.assertRaises(UsageError) as ctx:
            self.provider.fetch_usage("name=value\nmore=junk")
        self.assertTrue(ctx.exception.auth_error)


class TestChatGPTTellsWhatItGotBack(unittest.TestCase):
    """取得に失敗したとき、何が返ってきたのかが残ること。

    chatgpt.com が未ログイン時に WARNING_BANNER だけを返すようになったのに
    気づけなかったのは、この経路がキー名を1つもログにも画面にも残さず、
    ただ「Cookie が失効している可能性があります」とだけ言っていたため。
    """

    def respond(self, payload):
        provider = ChatGPTProvider()
        provider.http.get_json = lambda url, headers, what, params=None: payload
        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage("__Secure-next-auth.session-token=whatever")
        return ctx.exception

    def test_a_signed_out_session_is_told_apart_from_an_expired_cookie(self):
        """貼り直しても直らないものを貼り直させない。

        ログアウトしたブラウザの画面と、Cookie が切れた場合とでは、
        利用者の取るべき次の一手が違う (前者はまずサインインしてもらう)。
        文言は翻訳されて出るので、ここでは字面ではなく
        「別の案内になること」と「キー名が残ること」を見る。
        """
        signed_out = self.respond({"WARNING_BANNER": "DO NOT SHARE"})
        expired = self.respond({"user": {}, "expires": "2026-01-01T00:00:00Z"})
        self.assertTrue(signed_out.auth_error)
        self.assertIn("WARNING_BANNER", str(signed_out))
        self.assertNotEqual(str(signed_out), str(expired))

    def test_an_unexpected_shape_lists_the_keys_it_received(self):
        """形が変わったことに、次は気づけるようにしておく。"""
        error = self.respond({"user": {}, "expires": "2026-01-01T00:00:00Z"})
        self.assertTrue(error.auth_error)
        self.assertIn("expires", str(error))
        self.assertIn("user", str(error))

    def test_the_values_themselves_are_never_put_in_the_message(self):
        """出すのはキー名だけ。ここに出るものはパスワード同然。"""
        error = self.respond({"WARNING_BANNER": "DO NOT SHARE", "secret": "sk-ant-xyz"})
        self.assertNotIn("sk-ant-xyz", str(error))
        self.assertNotIn("DO NOT SHARE", str(error))


class TestChatGPTWorkspaceHeaders(unittest.TestCase):
    """ワークスペース所属のアカウントが 401 で弾かれないこと。

    Authorization だけでは足りない。データレジデンシーが有効な
    ワークスペースは x-openai-internal-codex-residency が無いと 401
    (「Workspace is not authorized in this region.」) を返し、それは画面では
    「要再ログイン」に見える — **貼り直しても直らないのに。**
    値は accessToken の中に入っているので、追加の問い合わせは要らない。
    """

    @staticmethod
    def token(**claims) -> str:
        def segment(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        payload = {"sub": "user-1", "exp": time.time() + 3600}
        if claims:
            payload["https://api.openai.com/auth"] = claims
        return f"{segment({'alg': 'RS256'})}.{segment(payload)}.notarealsignature"

    def headers_for(self, token) -> dict:
        provider = ChatGPTProvider()
        seen = {}

        def fake_get_json(url, headers, what, params=None):
            seen.update(headers)
            return dict(TestChatGPTUsageParsing.USAGE_RESPONSE)

        provider.http.get_json = fake_get_json
        provider.fetch_usage(token)
        return seen

    def test_account_id_and_residency_come_from_the_token_itself(self):
        headers = self.headers_for(self.token(
            chatgpt_account_id="acct-123", chatgpt_data_residency="eu"))
        self.assertEqual(headers["ChatGPT-Account-Id"], "acct-123")
        self.assertEqual(headers["x-openai-internal-codex-residency"], "eu")

    def test_compute_residency_is_used_when_data_residency_is_absent(self):
        headers = self.headers_for(self.token(chatgpt_compute_residency="us"))
        self.assertEqual(headers["x-openai-internal-codex-residency"], "us")

    def test_a_personal_token_adds_nothing(self):
        """個人の Plus/Pro を壊さないこと。無いものは付けない。"""
        headers = self.headers_for(self.token())
        self.assertNotIn("ChatGPT-Account-Id", headers)
        self.assertNotIn("x-openai-internal-codex-residency", headers)

    def test_the_request_still_looks_like_a_browser(self):
        """Cloudflare は UA 単体ではなくヘッダの組み合わせも見る。"""
        headers = self.headers_for(self.token())
        for name in ("Sec-Fetch-Site", "Sec-Ch-Ua", "Accept-Language"):
            self.assertIn(name, headers)


class TestHttpFailuresAreLegible(unittest.TestCase):
    """失敗の理由が、画面とログに残ること。

    401 の本文を捨てていたため、直し方が正反対の失敗が全部同じ
    「認証エラー (401)」に潰れていた。403 も 401 と同じ枝に入れていたので、
    ボット判定で弾かれただけの利用者にも「要再ログイン」が出ていた。
    """

    class Response:
        def __init__(self, status, body="", headers=None):
            self.status_code = status
            self.text = body
            self.headers = headers or {}

        def json(self):
            return json.loads(self.text)

    class Session:
        def __init__(self, response):
            self.response = response

        def request(self, method, url, **kwargs):
            return self.response

    def fetch(self, response):
        client = provider_base.HttpClient(credential_label="Cookie")
        client._session = self.Session(response)
        with self.assertRaises(UsageError) as ctx:
            client.get_json("https://example.test/usage", {}, "usage")
        return ctx.exception

    def test_the_reason_the_server_gave_is_kept(self):
        error = self.fetch(self.Response(401, json.dumps({"error": {
            "message": "Workspace is not authorized in this region.",
            "code": "unauthorized_region"}})))
        self.assertTrue(error.auth_error)
        self.assertIn("Workspace is not authorized in this region.", str(error))
        self.assertIn("unauthorized_region", str(error))

    def test_a_plain_401_without_a_body_still_reads_as_an_auth_error(self):
        error = self.fetch(self.Response(401, "<html>no</html>"))
        self.assertTrue(error.auth_error)

    def test_a_cloudflare_block_is_not_reported_as_an_expired_credential(self):
        """ボット判定を「要再ログイン」にしない。貼り直しても直らない。"""
        error = self.fetch(self.Response(
            403, "<html>Just a moment</html>",
            {"cf-mitigated": "challenge", "cf-ray": "abc123-NRT"}))
        self.assertFalse(error.auth_error)
        self.assertIn("abc123-NRT", str(error))

    def test_a_403_without_the_cloudflare_header_is_still_an_auth_error(self):
        """条件を広げすぎて、正しい「要再ログイン」を落とさないこと。"""
        error = self.fetch(self.Response(403, "<html>forbidden</html>"))
        self.assertTrue(error.auth_error)

    def test_a_401_carrying_the_cloudflare_header_stays_an_auth_error(self):
        error = self.fetch(self.Response(401, "", {"cf-mitigated": "challenge"}))
        self.assertTrue(error.auth_error)

    def test_secrets_in_the_body_are_not_echoed_back(self):
        error = self.fetch(self.Response(401, json.dumps({"error": {
            "message": "bad token sk-ant-abcdefghijklmnopqrstuvwxyz"}})))
        self.assertNotIn("sk-ant-abcdefghijklmnopqrstuvwxyz", str(error))


class TestChatGPTRefreshFailure(unittest.TestCase):
    """ChatGPT 側でトークンの更新が壊れている状態を、そう言えること。

    実測 (2026-08-19)。Cookie は生きていて /api/auth/session は 200 を返し、
    user も expires も正常なのに、こういう応答になることがある:

        {"error": "RefreshAccessTokenError",
         "user": {...}, "expires": "2026-11-17T...",
         "accessToken": <6日前に失効した JWT>}

    error を見ずに accessToken を使うと backend-api が 401 (token_expired)
    を返し、画面には「要再ログイン」とだけ出る。**しかし Cookie は切れて
    いないので、同じブラウザから貼り直しても必ず同じ結果になる。**
    ブラウザで入り直してもらう以外に道が無いことを、その場で言う必要がある。
    """

    make_jwt = staticmethod(TestChatGPTPastedAccessToken.make_jwt)

    def session(self, **overrides):
        payload = {
            "WARNING_BANNER": "DO NOT SHARE",
            "user": {"id": "user-1"},
            "expires": "2026-11-17T01:34:39.690Z",
            "accessToken": self.make_jwt(exp=time.time() + 3600),
        }
        payload.update(overrides)
        return payload

    def fetch(self, session_payload):
        provider = ChatGPTProvider()
        self.urls = []

        def fake_get_json(url, headers, what, params=None):
            self.urls.append(url)
            if url.endswith("/api/auth/session"):
                return session_payload
            return dict(TestChatGPTUsageParsing.USAGE_RESPONSE)

        provider.http.get_json = fake_get_json
        return provider.fetch_usage("__Secure-next-auth.session-token=whatever")

    def test_a_refresh_failure_is_named_rather_than_blamed_on_the_cookie(self):
        with self.assertRaises(UsageError) as ctx:
            self.fetch(self.session(error="RefreshAccessTokenError"))
        self.assertTrue(ctx.exception.auth_error)
        self.assertIn("RefreshAccessTokenError", str(ctx.exception))

    def test_a_refresh_failure_stops_before_spending_a_request_on_a_dead_token(self):
        """送っても 401 が返るだけと分かっているものを送らない。"""
        with self.assertRaises(UsageError):
            self.fetch(self.session(error="RefreshAccessTokenError"))
        self.assertEqual(self.urls, ["https://chatgpt.com/api/auth/session"])

    def test_an_expired_exchanged_token_is_caught_before_the_usage_call(self):
        """交換で受け取った側の期限も見る (以前は貼り付けた JWT しか見ていない)。"""
        with self.assertRaises(UsageError) as ctx:
            self.fetch(self.session(accessToken=self.make_jwt(exp=time.time() - 60)))
        self.assertTrue(ctx.exception.auth_error)
        self.assertEqual(self.urls, ["https://chatgpt.com/api/auth/session"])

    def test_a_healthy_session_is_untouched(self):
        """error が無いときは今までどおり通ること。"""
        result = self.fetch(self.session())
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 41.0)
        self.assertEqual(len(self.urls), 2)

    def test_the_paste_is_refused_before_it_is_ever_saved(self):
        """保存する Cookie は暗号化された JWE で、後から error を読み直せない。

        貼り付けを絞る前のここが、この事実に気づける唯一の機会。
        """
        provider = ChatGPTProvider()
        broken = json.dumps(self.session(error="RefreshAccessTokenError"))
        warning = provider.validate_paste(broken)
        self.assertIn("RefreshAccessTokenError", warning)
        self.assertEqual(provider.validate_paste(json.dumps(self.session())), "")
        self.assertEqual(provider.validate_paste(""), "")

    def test_the_paste_check_survives_a_dirty_copy(self):
        """先頭にゴミが付いていても見落とさないこと。"""
        provider = ChatGPTProvider()
        broken = json.dumps(self.session(error="RefreshAccessTokenError"), indent=2)
        self.assertIn("RefreshAccessTokenError",
                      provider.validate_paste("Pretty-print\n" + broken))

    def test_other_providers_are_unaffected(self):
        """既定は空。口を足しただけで他の取得先の挙動は変えない。"""
        for provider in (ClaudeProvider(), GeminiProvider(), CodexProvider()):
            self.assertEqual(provider.validate_paste('{"error": "whatever"}'), "")


class TestChatGPTSessionIsKeptAlive(unittest.TestCase):
    """取得のたびにセッションを延ばし、その結果を保存し直すこと。

    NextAuth のセッションはローリングで、/api/auth/session を呼ぶたびに
    新しい sessionToken が発行され、有効期限が 90日先へ押し出される
    (実測 2026-08-19: Set-Cookie の有効期限は毎回 2160 時間先。
     発行された sessionToken だけで使用状況を取得できることも確認済み)。

    **保存し直さなければ、最初に貼った1個をずっと使うことになる。**
    更新の鎖が切れた時点で応答が RefreshAccessTokenError になり、
    貼り直しでは直らない状態に落ちる。2026-08 に実際に起きたのがこれ。

    書き戻す先は vscode-extension/backend/cli.py で、fetch_usage の戻り値の
    "credential" を見て保存する (gemini.rotate_cookies と同じ経路)。
    """

    make_jwt = staticmethod(TestChatGPTPastedAccessToken.make_jwt)
    NAME = ChatGPTProvider.session_cookie_name

    def setUp(self):
        # 間引きの記録はモジュール全体で共有される。試験ごとに消す。
        chatgpt_module._last_renewal.clear()
        self.fresh = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..new.cipher.tag"

    def session(self, **overrides):
        payload = {
            "user": {"id": "user-1"},
            "expires": "2026-11-17T01:56:33.415Z",
            "accessToken": self.make_jwt(exp=time.time() + 3600),
            "sessionToken": self.fresh,
        }
        payload.update(overrides)
        return payload

    def fetch(self, credential, session_payload=None):
        provider = ChatGPTProvider()
        payload = self.session() if session_payload is None else session_payload

        def fake_get_json(url, headers, what, params=None):
            if url.endswith("/api/auth/session"):
                return payload
            return dict(TestChatGPTUsageParsing.USAGE_RESPONSE)

        provider.http.get_json = fake_get_json
        return provider.fetch_usage(credential)

    def test_the_freshly_issued_session_is_handed_back_to_be_saved(self):
        result = self.fetch("%s=old-value" % self.NAME)
        self.assertEqual(result["credential"], "%s=%s" % (self.NAME, self.fresh))

    def test_the_renewal_only_happens_after_a_successful_fetch(self):
        """使用状況が取れなかった取得で資格情報を差し替えないこと。"""
        provider = ChatGPTProvider()

        def fake_get_json(url, headers, what, params=None):
            if url.endswith("/api/auth/session"):
                return self.session()
            raise UsageError("usage failed")

        provider.http.get_json = fake_get_json
        with self.assertRaises(UsageError):
            provider.fetch_usage("%s=old-value" % self.NAME)

    def test_a_second_fetch_soon_after_does_not_rewrite_the_settings(self):
        """値は毎回変わる (JWE は呼ぶたびに暗号化し直される)。

        素直に書き戻すと取得のたびに設定ファイルへ書くことになるので間引く。
        """
        first = self.fetch("%s=old-value" % self.NAME)
        self.assertIn("credential", first)
        second = self.fetch(first["credential"])
        self.assertNotIn("credential", second)

    def test_the_throttle_survives_the_credential_changing(self):
        """**鍵に保存値を使うと間引きが効かない。**

        保存し直すと値が変わるため、値を鍵にすると毎回「初回」になる。
        アカウント側の変わらない印 (user.id) を鍵にしていることを固定する。
        """
        self.fetch("%s=old-value" % self.NAME)
        self.fresh = "eyJhbGciOiJkaXIi..another.cipher.tag"
        again = self.fetch("%s=something-else-entirely" % self.NAME)
        self.assertNotIn("credential", again)

    def test_two_accounts_are_throttled_separately(self):
        self.fetch("%s=account-a" % self.NAME)
        other = self.fetch("%s=account-b" % self.NAME,
                           self.session(user={"id": "user-2"}))
        self.assertIn("credential", other)

    def test_an_unchanged_value_is_not_handed_back(self):
        """同じものを書き戻させない (無駄な保存を1回でも減らす)。"""
        result = self.fetch("%s=%s" % (self.NAME, self.fresh))
        self.assertNotIn("credential", result)

    def test_a_session_without_a_session_token_still_reports_usage(self):
        """延命できなくても取得は成功している。例外にしないこと。"""
        payload = self.session()
        del payload["sessionToken"]
        result = self.fetch("%s=old-value" % self.NAME, payload)
        self.assertNotIn("credential", result)
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 41.0)

    def test_a_pasted_access_token_has_nothing_to_renew(self):
        """生の JWT を貼った経路は Cookie を持たないので延ばせない。"""
        result = self.fetch(self.make_jwt(exp=time.time() + 3600))
        self.assertNotIn("credential", result)

    def test_the_key_matches_what_the_settings_writer_looks_for(self):
        """cli.py は data.pop("credential") で受け取る。名前を揃えておく。"""
        cli = os.path.join(EXTENSION_DIR, "backend", "cli.py")
        with open(cli, encoding="utf-8") as f:
            source = f.read()
        self.assertIn('data.pop("credential"', source)


class TestGeminiUsageParsing(unittest.TestCase):
    """Gemini (Web) の batchexecute 応答を解釈できること。

    DevTools で実測した本物の応答をそのまま使っている (Pro プラン)。
    画面には「現在の使用量 1% / 11:57にリセット」「1週間の上限 0% /
    8月11日の12:57にリセット」と出ていた。
    """

    # 実測の生応答。長さの行が挟まる独自形式であることが要点。
    RAW_RESPONSE = (
        ")]}'\n"
        "\n"
        "251\n"
        '[["wrb.fr","jSf9Qc","[2,[[999999,0,5,null,null,[[1786420628,997914000],2]],'
        '[2382,0.01,1,[[1785985028,997821000]]],'
        '[48329,0.00112903,2,[[1786420628,997914000]]]],false]"'
        ',null,null,null,"generic"],["di",176],["af.httprm",176,"-797615228945374",4]]\n'
        "25\n"
        '[["e",4,null,null,287]]\n'
    )

    def payload(self):
        return rpc_payload(self.RAW_RESPONSE, "jSf9Qc")

    def test_length_prefixed_envelope_is_unwrapped(self):
        """長さの行を信じずに読めること。ずれても壊れないのが狙い。"""
        self.assertEqual(self.payload()[0], 2)

    def test_only_the_two_windows_shown_on_screen_become_metrics(self):
        """上限 999999 の枠は Gemini 自身も表示しない。0% として出さないこと。"""
        result = GeminiProvider.parse_usage_response(self.payload())
        self.assertEqual([m["label"] for m in result["metrics"]],
                         ["現在の使用量", "1週間の上限"])

    def test_fraction_is_usage_not_remainder(self):
        """0.01 は「1% 使用中」。Antigravity の remainingFraction とは逆。"""
        result = GeminiProvider.parse_usage_response(self.payload())
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 1.0)
        self.assertAlmostEqual(result["metrics"][1]["utilization"], 0.112903)

    def test_reset_times_match_what_the_screen_showed(self):
        result = GeminiProvider.parse_usage_response(self.payload())
        JST = timezone(timedelta(hours=9))

        resets = [
            datetime.strptime(m["resets_at"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc).astimezone(JST)
            for m in result["metrics"]
        ]
        self.assertEqual(resets[0].strftime("%m/%d %H:%M"), "08/06 11:57")
        self.assertEqual(resets[1].strftime("%m/%d %H:%M"), "08/11 12:57")

    def test_unknown_window_kind_is_labelled_not_dropped(self):
        """枠が増えたことに気づけなくなるので、黙って捨てない。"""
        result = GeminiProvider.parse_usage_response([2, [[100, 0.5, 7, [[1786420628, 0]]]], False])
        self.assertEqual(result["metrics"][0]["label"], "利用枠 (種別7)")
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 50.0)

    def test_unrecognized_payload_raises_instead_of_reporting_zero(self):
        for payload in (None, [], [2, "not-a-list", False], [2, [], False],
                        [2, [[999999, 0, 5]], False]):
            with self.subTest(payload=payload):
                with self.assertRaises(UsageError):
                    GeminiProvider.parse_usage_response(payload)

    def test_missing_rpcid_yields_nothing_rather_than_a_wrong_answer(self):
        self.assertIsNone(rpc_payload(self.RAW_RESPONSE, "someOther"))


class TestGeminiCredential(unittest.TestCase):
    """Google の Cookie ヘッダから、必要な分だけを保存すること。"""

    # SID / SAPISID は Gemini に限らず Google アカウント全体を操作できる。
    PASTED = (
        "SID=aaa; __Secure-1PSID=bbb; HSID=ccc; SAPISID=ddd; "
        "__Secure-1PSIDTS=eee; NID=fff; __Secure-3PSID=ggg"
    )

    def test_only_the_needed_cookies_are_kept(self):
        provider = GeminiProvider()
        normalized = provider.normalize_credential(self.PASTED)

        # __Secure-3PSID / NID は 1.2.2 で KEPT_COOKIES に加わったため、
        # いまは正規の保存対象になる (絞りすぎて延命に失敗していた不具合の
        # 修正 — vscode-extension/CHANGELOG.md の 1.2.2 節を参照)。
        self.assertEqual(
            normalized,
            "__Secure-1PSID=bbb; __Secure-1PSIDTS=eee; __Secure-3PSID=ggg; NID=fff",
        )
        # Gemini に限らず Google アカウント全体を操作できるものは、
        # 引き続き破棄すること。
        for leaked in ("SID=aaa", "HSID", "SAPISID"):
            self.assertNotIn(leaked, normalized)

    def test_every_way_of_copying_from_devtools_is_accepted(self):
        """持ち出し方を1つに限定しない。

        DevTools の見え方は環境で違い、利用者に正しい手順を1つだけ
        覚えてもらうのは無理がある。どれで来ても同じ結果になること。
        """
        provider = GeminiProvider()
        # __Secure-3PSID / NID も 1.2.2 で KEPT_COOKIES に加わったため保存される。
        expected = "__Secure-1PSID=bbb; __Secure-1PSIDTS=eee; __Secure-3PSID=ggg; NID=fff"

        variants = {
            "Copy value (そのまま)": self.PASTED,
            "Copy as cURL (bash)":
                f"curl 'https://gemini.google.com/usage' \\\n"
                f"  -H 'accept: text/html' \\\n"
                f"  -H 'cookie: {self.PASTED}' \\\n"
                f"  -H 'user-agent: Mozilla/5.0'",
            "Copy as cURL (PowerShell/cmd)":
                f'curl "https://gemini.google.com/usage" ^\n'
                f'  -H "cookie: {self.PASTED}" ^\n'
                f'  -H "accept: */*"',
            "-b で渡す形": f"curl https://gemini.google.com/usage -b '{self.PASTED}'",
            # Windows の Chrome / Edge の「Copy as cURL (cmd)」。
            # 引用符が ^" になり、Cookie は -H ではなく -b で来る。
            # ^ を見落とすと -b の値を取り出せず、貼り付けの先頭が
            # そのまま最初の Cookie 名に食い込む (実際に起きた)。
            "Copy as cURL (cmd)":
                f'curl ^"https://gemini.google.com/usage^" ^\n'
                f'  -H ^"accept: */*^" ^\n'
                f'  -b ^"{self.PASTED}^" ^\n'
                f'  -H ^"origin: https://gemini.google.com^"',
            "ヘッダ名つき1行": f"cookie: {self.PASTED}",
        }
        for name, pasted in variants.items():
            with self.subTest(copied_as=name):
                self.assertEqual(provider.normalize_credential(pasted), expected)

    def test_picking_the_wrong_network_row_names_the_actual_host(self):
        """一覧には別ドメイン宛ての通信も混ざり、並び順も毎回変わる。

        「見つかりません」だけでは何を直せばよいか分からず、同じ操作を
        繰り返すことになる。実際に起きた失敗なので、宛先を名指しする。
        """
        provider = GeminiProvider()
        wrong_row = (
            "curl 'https://www.gstatic.com/_/mss/boq-bard-web/_/js/k=boq.js' \\\n"
            "  -H 'accept: */*' \\\n"
            "  -H 'user-agent: Mozilla/5.0'"
        )
        warning = provider.validate_credential(wrong_row)
        self.assertIn("www.gstatic.com", warning)
        self.assertIn("usage", warning)

    def test_any_google_row_works_since_the_cookie_is_domain_wide(self):
        """__Secure-1PSID は .google.com の Cookie。行が違っても付いてくる。"""
        provider = GeminiProvider()
        other_row = (
            f"curl 'https://gemini.google.com/_/BardChatUi/data/batchexecute' "
            f"-H 'cookie: {self.PASTED}'"
        )
        self.assertEqual(
            provider.normalize_credential(other_row),
            "__Secure-1PSID=bbb; __Secure-1PSIDTS=eee; __Secure-3PSID=ggg; NID=fff",
        )
        self.assertEqual(provider.validate_credential(other_row), "")

    def test_pasting_the_page_text_is_explained_rather_than_rejected_flatly(self):
        """Cookie は HttpOnly なのでページ本文には出ない。実際に起きた取り違え。"""
        provider = GeminiProvider()
        page_text = "使用量上限 PRO\nGemini を使用できる時間は、プランの上限によって決まります。"
        warning = provider.validate_credential(page_text)
        self.assertIn("ページに表示されない", warning)

    def test_paste_without_the_session_cookie_is_reported(self):
        provider = GeminiProvider()
        pasted = "NID=fff; OTZ=ggg"
        # NID は 1.2.2 で KEPT_COOKIES に加わったため、いまは保存される
        # (以前は無関係な Cookie として無視され、貼り付け全体がそのまま
        # 素通りしていた)。それでも __Secure-1PSID が無い限り、
        # セッションの Cookie が無いことは変わらず報告される。
        self.assertEqual(provider.normalize_credential(pasted), "NID=fff")
        self.assertIn("__Secure-1PSID", provider.validate_credential(pasted))

    def test_a_correct_paste_passes_silently(self):
        provider = GeminiProvider()
        self.assertEqual(
            provider.validate_credential(provider.normalize_credential(self.PASTED)), ""
        )

    def test_manual_fallback_is_offered(self):
        """Google ログインは埋め込みブラウザから通らないので必須。"""
        provider = GeminiProvider()
        self.assertTrue(provider.has_manual_fallback)
        self.assertTrue(provider.manual_url.startswith("https://gemini.google.com"))


class TestGeminiRequestShape(unittest.TestCase):
    """batchexecute へ送る形。実測した DevTools の内容と一致させる。

    ここが狂っても応答は 200 で返り、中身が空になるだけで静かに失敗する。
    実機で試せない以上、送る形自体を固定しておく。
    """

    PAGE_HTML = (
        '<html><script>window.WIZ_global_data = {"SNlM0e":"AT-token-123",'
        '"cfb2h":"boq_assistant-bard-web-server_20260804.05_p0",'
        '"FdrFJe":"-1921946400893555212","other":"x"};</script></html>'
    )

    def call(self):
        provider = GeminiProvider()
        seen = {}

        def fake_get_text(url, headers, what, params=None):
            seen["page_url"] = url
            seen["page_cookie"] = headers.get("Cookie")
            return self.PAGE_HTML

        def fake_post_text(url, headers, what, data=None, params=None):
            seen["rpc_url"] = url
            seen["params"] = params
            seen["body"] = data
            seen["content_type"] = headers.get("Content-Type")
            return TestGeminiUsageParsing.RAW_RESPONSE

        provider.http.get_text = fake_get_text
        provider.http.post_text = fake_post_text
        result = provider.fetch_usage("SID=drop; __Secure-1PSID=keep")
        return seen, result

    def test_request_matches_what_the_browser_sent(self):
        seen, result = self.call()

        self.assertEqual(seen["rpc_url"],
                         "https://gemini.google.com/_/BardChatUi/data/batchexecute")
        self.assertEqual(seen["body"]["f.req"], '[[["jSf9Qc","[]",null,"generic"]]]')
        self.assertEqual(seen["body"]["at"], "AT-token-123")
        self.assertEqual(seen["params"]["rpcids"], "jSf9Qc")
        self.assertEqual(seen["params"]["source-path"], "/usage")
        self.assertEqual(seen["params"]["rt"], "c")
        self.assertIn("form-urlencoded", seen["content_type"])
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 1.0)

    def test_build_label_and_session_id_come_from_the_page_not_a_constant(self):
        """bl を決め打ちすると Google 側の更新で黙って古くなる。"""
        seen, _ = self.call()
        self.assertEqual(seen["params"]["bl"],
                         "boq_assistant-bard-web-server_20260804.05_p0")
        self.assertEqual(seen["params"]["f.sid"], "-1921946400893555212")

    def test_unneeded_cookies_are_not_sent(self):
        seen, _ = self.call()
        self.assertEqual(seen["page_cookie"], "__Secure-1PSID=keep")

    def test_login_page_instead_of_tokens_is_an_auth_error(self):
        """Cookie 失効時はログイン画面の HTML が返る。401 にはならない。"""
        provider = GeminiProvider()
        provider.http.get_text = lambda *a, **k: "<html>Sign in</html>"
        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage("__Secure-1PSID=stale")
        self.assertTrue(ctx.exception.auth_error)


class TestGeminiCookieRotation(unittest.TestCase):
    """短命な __Secure-1PSIDTS を延命し、貼り直しの頻度を下げられること。

    実測 (2026-08) で分かった肝は、RotateCookies が **有効な 1PSIDTS を
    要求する**ことです (期限切れ・壊れた値・値なしはいずれも 401)。
    失効を検知してから取り直すことはできないので、取得に成功した直後
    (= まだ有効と分かっている時点) に呼ぶ、という順序が要件になります。

    **1.2.2 で入った回帰修正 (vscode-extension/CHANGELOG.md 参照) をここで
    固定します。** 変更点は3つ:

      1. スロットリングのキーが __Secure-1PSID 単独ではなく、
         __Secure-1PSID + __Secure-1PSIDTS の組み合わせになった
         (_rotation_key + _last_rotation の指紋比較)。Cookie を貼り直した
         直後は 1PSIDTS が入れ替わるので、ROTATE_INTERVAL_SEC (600秒) を
         待たずに更新される。
      2. 保存対象の許可リスト KEPT_COOKIES が
         __Secure-1PSIDRTS / NID / __Secure-3PSID 系まで広がった。
      3. 更新リクエストで送る Cookie も (rotate_cookies 自身が絞るのではなく)
         保存してあるものをそのまま送るようになり、上の拡張分もまとめて
         Google へ送られるようになった。
    """

    LIVE = "__Secure-1PSID=long-lived; __Secure-1PSIDTS=live; __Secure-1PSIDCC=old"

    def setUp(self):
        # 更新間隔の記録はモジュール変数なので、テスト間で持ち越さない
        gemini._last_rotation.clear()

    def _provider(self, rotate_cookies, page_html=None):
        provider = GeminiProvider()
        seen = {"rotate_calls": 0, "rotate_cookie_sent": None}

        def fake_post_response(url, headers, what, data=None, params=None):
            seen["rotate_calls"] += 1
            seen["rotate_url"] = url
            seen["rotate_body"] = data
            seen["rotate_headers"] = headers
            seen["rotate_cookie_sent"] = headers.get("Cookie")
            return SimpleNamespace(cookies=rotate_cookies)

        provider.http.get_text = lambda *a, **k: page_html or TestGeminiRequestShape.PAGE_HTML
        provider.http.post_text = lambda *a, **k: TestGeminiUsageParsing.RAW_RESPONSE
        provider.http.post_response = fake_post_response
        return provider, seen

    def test_cookie_is_refreshed_after_a_successful_fetch(self):
        """有効なうちにしか取り直せないので、取得成功の直後に呼ぶこと。"""
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})

        result = provider.fetch_usage(self.LIVE)

        self.assertEqual(seen["rotate_calls"], 1)
        # 使用状況そのものは普通に取れている
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 1.0)

    def test_expired_cookie_does_not_trigger_a_pointless_rotation(self):
        """失効後に叩いても 401 が返るだけ。無駄な1往復を足さない。"""
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"},
                                        page_html="<html>Sign in</html>")

        with self.assertRaises(UsageError) as ctx:
            provider.fetch_usage(self.LIVE)

        self.assertTrue(ctx.exception.auth_error)
        self.assertEqual(seen["rotate_calls"], 0)

    def test_refreshed_cookie_is_handed_back_for_saving(self):
        """保存されないと次回も古い値のままで、延命にならない。"""
        provider, _ = self._provider(
            {"__Secure-1PSIDTS": "fresh", "__Secure-1PSIDCC": "new"})

        result = provider.fetch_usage(self.LIVE)

        updated = gemini.parse_cookie_header(result["credential"])
        self.assertEqual(updated["__Secure-1PSIDTS"], "fresh")
        self.assertEqual(updated["__Secure-1PSIDCC"], "new")
        # 長命な 1PSID は更新対象ではないので、そのまま引き継ぐ
        self.assertEqual(updated["__Secure-1PSID"], "long-lived")

    def test_nothing_is_returned_for_saving_when_the_refresh_failed(self):
        """更新できなかったのに保存させると、古い値で上書きしてしまう。"""
        provider, _ = self._provider({"__Secure-3PSIDTS": "irrelevant"})

        result = provider.fetch_usage(self.LIVE)

        self.assertNotIn("credential", result)
        # 取得自体は成功しているので、更新の失敗で落としてはいけない
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 1.0)

    def test_request_shape_is_the_one_google_accepts(self):
        """ボディの形が違うと 401 ではなく 400 が返る。失効と区別できず、はまった箇所。"""
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})

        provider.fetch_usage(self.LIVE)

        self.assertEqual(seen["rotate_url"], "https://accounts.google.com/RotateCookies")
        self.assertEqual(seen["rotate_body"], '[000,"-0000000000000000000"]')
        self.assertEqual(seen["rotate_headers"]["Content-Type"], "application/json")
        self.assertEqual(seen["rotate_headers"]["Origin"], "https://accounts.google.com")

    def test_1psidts_must_be_sent_or_google_answers_401(self):
        """1PSID だけでは更新できない (実測)。送り忘れると静かに延命が止まる。"""
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})

        provider.fetch_usage(self.LIVE)

        sent = gemini.parse_cookie_header(seen["rotate_cookie_sent"])
        self.assertIn("__Secure-1PSIDTS", sent)
        self.assertEqual(sent["__Secure-1PSIDTS"], "live")

    def test_only_the_session_cookies_are_sent_to_google(self):
        """更新先は Google アカウント全体を扱うドメイン。余計な Cookie を渡さない。

        **絞る場所が変わっている。** 現行版の rotate_cookies は、渡された
        jar をそのまま送る (KEPT_COOKIES への絞り込みは normalize_credential
        側の責務になった)。したがって rotate_cookies を直接呼ぶのではなく、
        実際の経路である fetch_usage 経由で確かめないと、この絞り込みを
        バイパスしてしまい意味のあるテストにならない。
        """
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})

        provider.fetch_usage(self.LIVE + "; SAPISID=secret; SID=secret2")

        self.assertEqual(set(gemini.parse_cookie_header(seen["rotate_cookie_sent"])),
                         {"__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC"})

    def test_the_expanded_kept_cookies_are_sent_when_renewing(self):
        """保存してある拡張分の Cookie (RTS / NID 等) も更新リクエストで送ること。

        以前は 1PSID 系の3つだけに絞って送っていたため、__Secure-1PSIDRTS が
        ブラウザのプロファイルには入っているのに更新リクエストには乗らず、
        RotateCookies が新しい __Secure-1PSIDTS を返さない (=延命に失敗する)
        原因になっていた (1.2.2 で修正)。
        """
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})
        credential = self.LIVE + "; __Secure-1PSIDRTS=rts-value; NID=nid-value"

        provider.fetch_usage(credential)

        sent = gemini.parse_cookie_header(seen["rotate_cookie_sent"])
        self.assertEqual(sent.get("__Secure-1PSIDRTS"), "rts-value")
        self.assertEqual(sent.get("NID"), "nid-value")

    def test_the_expanded_kept_cookies_are_carried_through_on_renewal(self):
        """更新の応答に含まれる __Secure-1PSIDRTS / NID / __Secure-3PSID 系も
        保存対象 (次回に使う credential) へ引き継がれること。

        KEPT_COOKIES を広げる前は、これらが応答に含まれていても捨てられて
        おり、次の更新リクエストは古い (あるいは無い) 値を送っていた。
        """
        provider, _ = self._provider({
            "__Secure-1PSIDTS": "fresh",
            "__Secure-1PSIDRTS": "new-rts",
            "NID": "new-nid",
            "__Secure-3PSIDTS": "new-3ts",
        })

        result = provider.fetch_usage(self.LIVE)

        updated = gemini.parse_cookie_header(result["credential"])
        self.assertEqual(updated["__Secure-1PSIDRTS"], "new-rts")
        self.assertEqual(updated["NID"], "new-nid")
        self.assertEqual(updated["__Secure-3PSIDTS"], "new-3ts")

    def test_rotation_is_not_repeated_within_the_interval(self):
        """同じ Cookie のままなら、取得のたびに叩かないこと (429 対策)。

        自動更新が5分間隔でも回りすぎない。__Secure-1PSIDTS が変わって
        いない限り、ROTATE_INTERVAL_SEC (600秒) は抑制される
        (1.2.2 の回帰修正: 次のテストと対になる)。

        **呼び出し元を模して、戻ってきた credential を次回へ引き継ぐ。**
        実際の呼び出し元 (vscode-extension/backend/cli.py) は fetch_usage が
        返した credential を account.cookie へ書き戻し、次回はその更新後の
        値で呼ぶ。ここで毎回 self.LIVE (更新前の古い __Secure-1PSIDTS) を
        送り直すと、「直前に自分が書き込んだ TS」と「いま来た TS」が食い違い、
        次のテスト (貼り直しの検知) と同じ経路に入って**毎回**ローテートして
        しまう — それ自体は 1.2.2 が意図した挙動なので、ここでは実際の
        呼び出し方に合わせて確かめる。
        """
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})

        credential = self.LIVE
        for _ in range(3):
            result = provider.fetch_usage(credential)
            credential = result.get("credential", credential)

        self.assertEqual(seen["rotate_calls"], 1)

    def test_a_freshly_pasted_cookie_is_renewed_immediately_even_within_the_interval(self):
        """Cookie を貼り直した直後は、間隔を待たずに更新されること。

        **1.2.2 の回帰修正の核心。** _rotation_key は __Secure-1PSID の
        ハッシュだけで決まり、貼り直しても (長命な 1PSID 自体は変わらない
        ので) キー自体は変わらない。以前はこれだけで間隔を判定していたため、
        利用者が新しい Cookie を貼った直後 — いちばん延命を急ぎたいところ —
        で最大 ROTATE_INTERVAL_SEC 秒待たされてしまっていた。
        __Secure-1PSIDTS の指紋も一緒に見ることで、TS が入れ替わっていれば
        「別の資格情報が来た」とみなして間隔を無視し、即座に更新へ回る。
        """
        provider, seen = self._provider({"__Secure-1PSIDTS": "second-fresh"})

        provider.fetch_usage(self.LIVE)
        self.assertEqual(seen["rotate_calls"], 1)

        # 同じ __Secure-1PSID (長命) だが、利用者が Cookie を貼り直したことで
        # __Secure-1PSIDTS が変わっている想定。間隔はまだ全く空いていない。
        repasted = ("__Secure-1PSID=long-lived; __Secure-1PSIDTS=brand-new; "
                   "__Secure-1PSIDCC=old2")
        provider.fetch_usage(repasted)

        self.assertEqual(seen["rotate_calls"], 2)

    def test_accounts_are_throttled_independently(self):
        """1つのアカウントの更新が、別アカウントの更新を止めてはいけない。"""
        provider, seen = self._provider({"__Secure-1PSIDTS": "fresh"})

        for psid in ("account-one", "account-two"):
            provider.fetch_usage(f"__Secure-1PSID={psid}; __Secure-1PSIDTS=live")

        self.assertEqual(seen["rotate_calls"], 2)

    def test_a_failed_rotation_never_breaks_the_fetch(self):
        """更新は付随処理。ここで落とすと使用状況まで見られなくなる。"""
        provider, _ = self._provider({})

        def refuse(*a, **k):
            raise UsageError("認証エラー (401)", auth_error=True)

        provider.http.post_response = refuse

        result = provider.fetch_usage(self.LIVE)
        self.assertAlmostEqual(result["metrics"][0]["utilization"], 1.0)


class TestBrowserProfileCookieInjection(unittest.TestCase):
    """貼られた Cookie をブラウザプロファイルへ預ける際の変換。

    ここを間違えるとブラウザ側が黙って Cookie を破棄し、
    「預けたはずなのにセッションが維持されない」という形で表に出ます。
    """

    PASTED = (
        "curl 'https://gemini.google.com/usage' "
        "-H 'accept: */*' "
        "-b '__Secure-1PSID=aaa; SID=bbb; SAPISID=ccc; "
        "__Host-GAPS=ddd; __Secure-1PSIDTS=eee'"
    )

    def build(self, text="__Secure-1PSID=aaa; SID=bbb"):
        return {bytes(c.name()).decode(): c
                for c in browser_profile.build_cookies(text, "google.com")}

    def test_the_first_cookie_survives_a_curl_prefix(self):
        """先頭の Cookie に貼り付けの飾りが食い込む。ここを捨てると主要な1件を失う。"""
        names = set(self.build("curl 'https://x' -b '__Secure-1PSID=aaa; SID=bbb"))
        self.assertIn("__Secure-1PSID", names)

    def test_session_cookies_the_config_throws_away_are_kept_here(self):
        """設定ファイルに残さないものこそ、ここで預ける意味がある。"""
        names = set(self.build(self.PASTED))
        self.assertLessEqual({"__Secure-1PSID", "SID", "SAPISID", "__Secure-1PSIDTS"}, names)

    def test_curl_fragments_are_not_mistaken_for_cookies(self):
        """貼り付けには URL や -H の断片が必ず混ざる。"""
        names = set(self.build(self.PASTED))
        self.assertNotIn("curl 'https://gemini.google.com/usage' -H 'accept", names)
        for name in names:
            self.assertNotIn(" ", name)

    def test_cookies_are_secure_or_the_browser_discards_them(self):
        """__Secure- / __Host- 接頭辞は Secure でないと破棄される。"""
        for name, cookie in self.build(self.PASTED).items():
            self.assertTrue(cookie.isSecure(), f"{name} が Secure になっていない")

    def test_domain_is_shared_across_google_hosts(self):
        """gemini.google.com だけに絞ると accounts.google.com へ送られない。"""
        cookie = self.build()["__Secure-1PSID"]
        self.assertEqual(cookie.domain(), ".google.com")

    def test_host_prefixed_cookies_carry_no_domain(self):
        """__Host- はドメイン指定があると仕様上破棄される。"""
        cookie = self.build(self.PASTED)["__Host-GAPS"]
        self.assertEqual(cookie.domain(), "")

    def test_expiry_is_set_so_the_cookie_survives_a_restart(self):
        """期限を入れないとセッション Cookie 扱いになり、終了時に消える。"""
        cookie = self.build()["__Secure-1PSID"]
        self.assertFalse(cookie.isSessionCookie())


class TestAntigravityCreditsParsing(unittest.TestCase):
    """Antigravity の loadCodeAssist レスポンスを解釈できること。

    UNSUPPORTED_CLIENT は tier 情報が丸ごと消えるため、
    「残高0」と見分けがつかない。必ず別のエラーにすることが要。
    """

    OK_RESPONSE = {
        "currentTier": {"id": "free-tier", "name": "Antigravity"},
        "paidTier": {
            "id": "g1-pro-tier",
            "name": "Google AI Pro",
            "availableCredits": [
                {"creditType": "GOOGLE_ONE_AI", "creditAmount": "1200"},
                {"creditType": "SOMETHING_ELSE", "creditAmount": "999"},
            ],
        },
    }

    def test_g1_credits_are_summed_and_parsed_from_strings(self):
        """creditAmount は int64 の文字列表現。数値として来ない。"""
        result = AntigravityProvider.parse_load_code_assist(self.OK_RESPONSE)
        metric = result["metrics"][0]
        self.assertEqual(metric["value"], 1200)
        self.assertEqual(metric["kind"], AMOUNT)
        self.assertIn("Google AI Pro", metric["label"])

    def test_other_credit_types_are_excluded(self):
        result = AntigravityProvider.parse_load_code_assist(self.OK_RESPONSE)
        self.assertNotEqual(result["metrics"][0]["value"], 2199)

    def test_no_total_means_no_gauge(self):
        """分母が無いので 0-100% のゲージには載せない。"""
        result = AntigravityProvider.parse_load_code_assist(self.OK_RESPONSE)
        self.assertIsNone(result["metrics"][0]["utilization"])
        self.assertIsNone(result["max_utilization"])

    def test_zero_balance_is_reported_not_treated_as_missing(self):
        """残高0と「情報が無い」は別物。"""
        response = {"paidTier": {"id": "g1-pro-tier", "availableCredits": [
            {"creditType": "GOOGLE_ONE_AI", "creditAmount": "0"}]}}
        result = AntigravityProvider.parse_load_code_assist(response)
        self.assertEqual(result["metrics"][0]["value"], 0)

    def test_unsupported_client_raises_instead_of_showing_zero(self):
        """UA 判定で弾かれたときに「クレジット0」と表示しないこと。"""
        response = {
            "reasonCode": "UNSUPPORTED_CLIENT",
            "reasonMessage": "This client is no longer supported...",
            "tierId": "free-tier",
        }
        with self.assertRaises(UsageError) as ctx:
            AntigravityProvider.parse_load_code_assist(response)
        self.assertIn("UNSUPPORTED_CLIENT", str(ctx.exception))

    def test_missing_credits_raises_with_the_tier_shown(self):
        response = {"paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"}}
        with self.assertRaises(UsageError) as ctx:
            AntigravityProvider.parse_load_code_assist(response)
        self.assertIn("g1-pro-tier", str(ctx.exception))

    def test_quota_fraction_is_inverted_into_a_usage_rate(self):
        """remainingFraction は「残り割合」。使用率にするには 1 から引く。

        そのまま出すと意味が逆になり、使い切り寸前を「95%空き」と表示する。
        """
        response = {"buckets": [
            {"bucketId": "five_hour", "displayName": "5時間", "window": "5h",
             "remainingFraction": 0.05, "resetTime": "2030-01-01T00:00:00Z"},
        ]}
        result = AntigravityProvider.parse_quota_summary(response)
        metric = result["metrics"][0]
        self.assertEqual(metric["kind"], PERCENT)
        self.assertAlmostEqual(metric["utilization"], 95.0)
        self.assertEqual(metric["label"], "5時間 (5h)")
        self.assertEqual(metric["resets_at"], "2030-01-01T00:00:00Z")

    def test_remaining_amount_becomes_a_count_not_a_percent(self):
        response = {"buckets": [
            {"bucketId": "requests", "displayName": "リクエスト",
             "remainingAmount": "250"},
        ]}
        metric = AntigravityProvider.parse_quota_summary(response)["metrics"][0]
        self.assertEqual(metric["kind"], AMOUNT)
        self.assertEqual(metric["value"], 250)
        self.assertIsNone(metric["utilization"])

    def test_disabled_buckets_are_skipped(self):
        response = {"buckets": [
            {"bucketId": "a", "remainingFraction": 0.5},
            {"bucketId": "b", "remainingFraction": 0.5, "disabled": True},
        ]}
        result = AntigravityProvider.parse_quota_summary(response)
        self.assertEqual([m["key"] for m in result["metrics"]], ["a"])

    def test_grouped_buckets_are_flattened_with_the_group_name(self):
        response = {"groups": [
            {"displayName": "モデル別", "buckets": [
                {"bucketId": "pro", "displayName": "Pro", "remainingFraction": 0.25}]},
        ]}
        metric = AntigravityProvider.parse_quota_summary(response)["metrics"][0]
        self.assertEqual(metric["key"], "モデル別/pro")
        self.assertEqual(metric["label"], "モデル別 / Pro")
        self.assertAlmostEqual(metric["utilization"], 75.0)

    def test_bucket_without_remaining_is_not_reported_as_zero(self):
        """remaining は oneof。どちらも無いのは「情報が無い」で 0% ではない。"""
        with self.assertRaises(UsageError):
            AntigravityProvider.parse_quota_summary(
                {"buckets": [{"bucketId": "a", "displayName": "A"}]}
            )

    def test_empty_quota_response_raises(self):
        for data in ({}, {"buckets": []}, {"description": "x"}):
            with self.subTest(data=data):
                with self.assertRaises(UsageError):
                    AntigravityProvider.parse_quota_summary(data)

    def test_user_agent_must_contain_antigravity(self):
        """UA は取得可否を決めるので、うっかり変えられないよう固定する。"""
        from services.providers import antigravity
        self.assertIn("antigravity", antigravity.USER_AGENT.lower())


class TestAnthropicCostParsing(unittest.TestCase):
    """Anthropic Admin API の cost_report を解釈できること。

    amount は「セント単位の10進文字列」で、リファレンスに
    「"123.45" in "USD" represents $1.23」と明記されています。
    ドルとして扱うと 100 倍になるため、ここを固定するのが最重要です。
    """

    # 公式リファレンス (get-cost-report) 記載のレスポンス例そのまま
    DOC_EXAMPLE = {
        "data": [
            {
                "ending_at": "2025-08-02T00:00:00Z",
                "results": [
                    {
                        "amount": "123.78912",
                        "context_window": "0-200k",
                        "cost_type": "tokens",
                        "currency": "USD",
                        "description": "Claude Sonnet 4 Usage - Input Tokens",
                        "inference_geo": "global",
                        "model": "claude-opus-4-6",
                        "service_tier": "standard",
                        "token_type": "uncached_input_tokens",
                        "workspace_id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ",
                    }
                ],
                "starting_at": "2025-08-01T00:00:00Z",
            }
        ],
        "has_more": True,
        "next_page": "page_MjAyNS0wNS0xNFQwMDowMDowMFo=",
    }

    def parse(self, pages, period=""):
        return AnthropicCostProvider.parse_cost_report(pages, period=period)

    def test_amount_is_cents_not_dollars(self):
        """"123.78912" セント = $1.2378912 であること (100倍しない)。"""
        result = self.parse([self.DOC_EXAMPLE])
        self.assertAlmostEqual(result["total_usd"], 1.2378912)
        self.assertEqual(result["metrics"][0]["display"], "$1.24")
        # ドル扱いだと 123.79 になる。それを踏まないことを固定する。
        self.assertNotAlmostEqual(result["total_usd"], 123.78912)

    def test_breakdown_uses_the_model_name(self):
        result = self.parse([self.DOC_EXAMPLE])
        self.assertEqual(result["metrics"][1]["key"], "item:claude-opus-4-6")

    def test_amounts_are_summed_across_pages_and_buckets(self):
        page2 = {
            "data": [{
                "starting_at": "2025-08-02T00:00:00Z",
                "ending_at": "2025-08-03T00:00:00Z",
                "results": [
                    {"amount": "100.00", "currency": "USD", "cost_type": "tokens",
                     "model": "claude-opus-4-6"},
                    {"amount": "50.00", "currency": "USD", "cost_type": "web_search",
                     "description": "Web Search Usage", "model": None},
                ],
            }],
            "has_more": False,
        }
        result = self.parse([self.DOC_EXAMPLE, page2])
        # (123.78912 + 100 + 50) セント
        self.assertAlmostEqual(result["total_usd"], 2.7378912)
        # model が null の項目は description で見出しを作る
        self.assertIn("Web Search Usage", result["breakdown"])
        self.assertAlmostEqual(result["breakdown"]["claude-opus-4-6"], 2.2378912)

    def test_no_usage_is_not_an_error(self):
        """当月まだ使っていない状態を「仕様変更」と誤判定しないこと。"""
        result = self.parse([{"data": [], "has_more": False}])
        self.assertAlmostEqual(result["total_usd"], 0.0)
        self.assertEqual(len(result["metrics"]), 1)

    def test_missing_data_key_raises(self):
        """仕様変更を「0ドル」と偽らないこと。"""
        for pages in ([], [{}], [{"error": "nope"}]):
            with self.subTest(pages=pages):
                with self.assertRaises(UsageError):
                    self.parse(pages)

    def test_unreadable_amount_is_skipped_not_zeroed(self):
        pages = [{"data": [{"results": [
            {"amount": "100.00", "currency": "USD", "model": "a"},
            {"amount": None, "currency": "USD", "model": "b"},
            {"amount": "とても高い", "currency": "USD", "model": "c"},
        ]}], "has_more": False}]
        result = self.parse(pages)
        self.assertAlmostEqual(result["total_usd"], 1.0)
        self.assertNotIn("b", result["breakdown"])

    def test_mixed_currency_raises_instead_of_summing(self):
        pages = [{"data": [{"results": [
            {"amount": "100.00", "currency": "USD", "model": "a"},
            {"amount": "100.00", "currency": "JPY", "model": "b"},
        ]}], "has_more": False}]
        with self.assertRaises(UsageError):
            self.parse(pages)

    def test_cost_has_no_gauge(self):
        result = self.parse([self.DOC_EXAMPLE])
        self.assertIsNone(result["max_utilization"])

    def test_month_to_date_covers_today(self):
        """ending_at を今日の00:00にすると当日分が落ちる。翌日になっていること。"""
        start, end = AnthropicCostProvider.month_to_date(
            datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(start, "2026-08-01T00:00:00Z")
        self.assertEqual(end, "2026-08-06T00:00:00Z")

    def test_empty_credential_is_an_auth_error(self):
        with self.assertRaises(UsageError) as ctx:
            AnthropicCostProvider().fetch_usage("")
        self.assertTrue(ctx.exception.auth_error)


class TestNoProxyIsHonored(unittest.TestCase):
    """NO_PROXY のホストを社内プロキシへ投げないこと。

    manual モードでは明示的な proxies 辞書を返すため、これを見ないと
    「system では取れるのに manual にすると失敗する」という不具合になる。
    """

    INTERNAL = "https://internal.example.jp/bill/billing"
    EXTERNAL = "https://claude.ai/api/organizations"

    # 実環境の HTTP(S)_PROXY を残すと credentials() が本物の資格情報を拾い、
    # 期待値が環境依存になる (かつ失敗時の差分にパスワードが出る)。必ず退避する。
    _ENV_KEYS = ("NO_PROXY", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "http_proxy", "https_proxy")

    def setUp(self):
        from services import proxy_manager
        self.pm = proxy_manager
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = "internal.example.jp"
        os.environ["no_proxy"] = "internal.example.jp"
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "", "proxy_password": "",
        })

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.pm.configure(dict(self.pm._settings, proxy_mode="system",
                               proxy_username="", proxy_password=""))

    def test_internal_host_bypasses_the_proxy(self):
        self.assertEqual(
            self.pm.requests_proxies(self.INTERNAL), {"http": None, "https": None}
        )

    def test_external_host_still_uses_the_proxy(self):
        self.assertEqual(
            self.pm.requests_proxies(self.EXTERNAL)["https"], "http://proxy.example.jp:8080"
        )

    def test_no_url_keeps_the_previous_behaviour(self):
        self.assertEqual(
            self.pm.requests_proxies()["https"], "http://proxy.example.jp:8080"
        )


class TestProviderRegistry(unittest.TestCase):
    def test_default_is_claude(self):
        self.assertEqual(providers.DEFAULT_PROVIDER_ID, "claude")
        self.assertIs(type(providers.get("claude")), ClaudeProvider)

    def test_unknown_provider_falls_back_to_default(self):
        """設定を手編集された場合でも起動できること。"""
        self.assertEqual(providers.get("だれ？").id, providers.DEFAULT_PROVIDER_ID)
        self.assertEqual(providers.get("").id, providers.DEFAULT_PROVIDER_ID)
        self.assertEqual(providers.get(None).id, providers.DEFAULT_PROVIDER_ID)

    def test_retired_providers_are_hidden_but_still_resolvable(self):
        """一覧から外した取得先を、既定 (Claude) に化けさせないこと。

        設定ファイルに古いアカウントが残っていた場合、Claude として
        扱うと「Claude の取得に失敗しました」という嘘の失敗が出る。
        """
        listed = [p.id for p in providers.all_providers()]
        for retired in ("codex", "antigravity"):
            with self.subTest(provider=retired):
                self.assertNotIn(retired, listed)
                self.assertEqual(providers.get(retired).id, retired)
                self.assertTrue(providers.is_known(retired))

    def test_ids_are_unique_and_labelled(self):
        all_ids = [p.id for p in providers.all_providers()]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        for provider in providers.all_providers():
            self.assertTrue(provider.id, "id が空のプロバイダがある")
            self.assertTrue(provider.label, f"{provider.id} に label が無い")

    def test_cookie_providers_declare_what_the_login_flow_needs(self):
        """ログイン画面はこのメタデータだけで組み立てるので、欠けていると動かない。"""
        for provider in providers.all_providers():
            if provider.auth_kind != providers.AUTH_COOKIE:
                continue
            with self.subTest(provider=provider.id):
                self.assertTrue(provider.login_url)
                self.assertTrue(provider.home_url)
                self.assertTrue(provider.cookie_domain)
                self.assertTrue(provider.session_cookie_name)

    def test_unimplemented_providers_raise_a_readable_error(self):
        for provider in providers.all_providers():
            if provider.implemented:
                continue
            with self.subTest(provider=provider.id):
                with self.assertRaises(UsageError) as ctx:
                    provider.fetch_usage("dummy")
                self.assertIn("実装されていません", str(ctx.exception))


class TestBudget(unittest.TestCase):
    """課金系の取得先に設定する上限金額。

    予算はアカウント側の設定であって取得先の応答ではないため、
    取得後に apply_budget() で被せる (fetch_usage のシグネチャは触らない)。
    """

    def cost_result(self, currency="JPY"):
        return build_result([
            money_metric("total", "当月コスト", 4521, currency=currency),
            money_metric("item:gpt-4", "gpt-4", 3000, currency=currency),
        ])

    def test_without_a_budget_there_is_no_gauge(self):
        """上限の無い金額を 0-100% のバーに載せると嘘になる。"""
        result = self.cost_result()
        self.assertIsNone(result["max_utilization"])
        self.assertIs(apply_budget(result, 0), result)
        self.assertIs(apply_budget(result, -5), result)

    def test_budget_turns_the_total_into_a_gauge(self):
        result = apply_budget(self.cost_result(), 10000)
        total = result["metrics"][0]
        self.assertAlmostEqual(total["utilization"], 45.21)
        self.assertAlmostEqual(result["max_utilization"], 45.21)
        self.assertEqual(total["display"], "¥4,521.00 / ¥10,000.00")

    def test_breakdown_rows_are_left_alone(self):
        """内訳ごとに上限があるわけではないので、合計にだけ効かせる。"""
        result = apply_budget(self.cost_result(), 10000)
        self.assertIsNone(result["metrics"][1]["utilization"])

    def test_the_original_result_is_not_mutated(self):
        """ワーカーと GUI スレッドが同じ辞書を触らないようにする。"""
        original = self.cost_result()
        apply_budget(original, 10000)
        self.assertIsNone(original["metrics"][0]["utilization"])
        self.assertIsNone(original["max_utilization"])

    def test_currency_is_preserved(self):
        result = apply_budget(self.cost_result(currency="USD"), 500)
        self.assertEqual(result["metrics"][0]["display"], "$4,521.00 / $500.00")

    def test_a_percent_result_is_untouched(self):
        """利用枠の取得先に予算が紛れ込んでも壊れないこと。"""
        result = build_result([percent_metric("five_hour", "5時間", 40.0)])
        self.assertIs(apply_budget(result, 10000), result)

    def test_only_cost_providers_offer_the_field(self):
        offered = {p.id for p in providers.all_providers() if p.supports_budget}
        self.assertEqual(offered, {"aoai-cost", "anthropic-cost"})
        self.assertEqual(providers.get("aoai-cost").currency, "JPY")
        self.assertEqual(providers.get("anthropic-cost").currency, "USD")


class TestAccountBudgetField(unittest.TestCase):
    def test_budget_round_trips_through_the_config_file(self):
        account = Account(name="AOAI", organization_id="", cookie="k", budget=10000)
        self.assertAlmostEqual(Account.from_dict(account.to_dict()).budget, 10000.0)

    def test_existing_config_without_the_key_is_readable(self):
        """このフィールドを持たない既存の設定ファイルが壊れないこと。"""
        account = Account.from_dict({"name": "旧", "organization_id": "", "cookie": "c"})
        self.assertEqual(account.budget, 0.0)

    def test_broken_values_fall_back_to_unset_instead_of_crashing(self):
        """手編集された設定ファイルで起動できなくならないこと。"""
        for broken in ("たくさん", None, -100, [], {}):
            with self.subTest(value=broken):
                self.assertEqual(
                    Account(name="n", organization_id="", cookie="c", budget=broken).budget,
                    0.0,
                )


class TestProviderLabels(unittest.TestCase):
    def test_labels_carry_no_parenthetical_suffix(self):
        """(Web) / (課金) は取得先名ではなく分類の説明で、一覧を読みにくくする。"""
        for provider in providers.all_providers():
            with self.subTest(provider=provider.id):
                self.assertNotIn("(", provider.label)

    def test_descriptions_do_not_point_at_retired_providers(self):
        """選べない取得先へ誘導すると、探して見つからないことになる。"""
        listed = {p.label for p in providers.all_providers()}
        for provider in providers.all_providers():
            for retired in ("Codex CLI", "Antigravity"):
                with self.subTest(provider=provider.id, retired=retired):
                    if retired in listed:
                        continue
                    self.assertNotIn(retired, provider.description)


@unittest.skip("デスクトップGUI版はリポジトリ分離により対象外 (ui/styles.py の scale_css/build_theme が現行版に存在しない)")
class TestUiScaling(unittest.TestCase):
    """拡大率。Qt を起動せずに検証できる部分だけをここで固定する。"""

    def test_all_px_values_scale_together(self):
        """フォントだけ拡大すると余白が取り残されて文字がはみ出す。"""
        css = "QWidget { font-size: 14px; padding: 8px; border-radius: 6px; }"
        self.assertEqual(
            scale_css(css, 150),
            "QWidget { font-size: 21px; padding: 12px; border-radius: 9px; }",
        )

    def test_hairlines_never_collapse_to_zero(self):
        """縮小時に 1px が 0px になると、枠線が消えてパネルの境目が分からなくなる。"""
        self.assertEqual(scale_css("border: 1px solid #000;", 30), "border: 1px solid #000;")

    def test_deliberate_zero_stays_zero(self):
        """0px は「消す」という意思表示で、縮小の巻き添えではない。

        スクロールバーの矢印ボタンは height: 0px で隠しているため、
        これを 1px に持ち上げると 100% 以外の倍率で矢印が現れる。
        """
        self.assertEqual(scale_css("height: 0px;", 150), "height: 0px;")
        self.assertEqual(scale_css("margin: 0px 0px 0px 0px;", 80),
                         "margin: 0px 0px 0px 0px;")

    def test_the_theme_keeps_its_hidden_scrollbar_arrows_at_every_scale(self):
        for percent in (80, 100, 150, 200):
            with self.subTest(percent=percent):
                self.assertIn("height: 0px", build_theme(percent))

    def test_unscaled_is_returned_untouched(self):
        css = "QWidget { font-size: 14px; }"
        self.assertIs(scale_css(css, 100), css)

    def test_theme_carries_no_korean_font(self):
        """Segoe UI に漢字が無いため、次の候補が実際の日本語描画に使われる。

        以前は Malgun Gothic (韓国語) が次に来ており、漢字が
        日本語と異なる字形で描画されていた。
        """
        self.assertNotIn("Malgun", build_theme(100))
        self.assertIn("Yu Gothic UI", build_theme(100))

    def test_theme_has_no_hardcoded_font_size_left_unscaled(self):
        """px を含む指定がスタイルシート側にあること (拡大率が効く条件)。"""
        self.assertIn("font-size: 21px", build_theme(150))


class TestStatusDot(unittest.TestCase):
    """一覧の丸印。以前は有効なら常に緑で、上限に達しても気づけなかった。"""

    def test_dot_follows_utilization(self):
        self.assertEqual(get_status_dot(0.0), "🟢")
        self.assertEqual(get_status_dot(59.9), "🟢")
        self.assertEqual(get_status_dot(60.0), "🟡")
        self.assertEqual(get_status_dot(100.0), "🔴")

    def test_only_a_used_up_quota_counts_as_limited(self):
        """「制限中」は使い切った状態だけ。

        以前は 85% で赤にしていたが、まだ 15% 残っているものを
        「制限中」と呼ぶのは嘘だった。
        """
        for still_usable in (85.0, 95.0, 99.0, 99.9):
            with self.subTest(percentage=still_usable):
                self.assertFalse(is_limited(still_usable))
                self.assertEqual(get_status_dot(still_usable), "🟡")

        self.assertTrue(is_limited(100.0))
        self.assertEqual(get_status_dot(100.0), "🔴")

    def test_anything_displayed_as_100_percent_is_limited(self):
        """表示は小数第1位まで。

        99.96% は画面に「100.0%」と出るので、これを黄色のままにすると
        「100.0% なのに制限中ではない」という食い違いが起きる。
        """
        self.assertEqual(percent_metric("k", "枠", 99.96)["display"], "100.0%")
        self.assertTrue(is_limited(99.96))
        self.assertFalse(is_limited(99.94))

    @unittest.skip("デスクトップGUI版はリポジトリ分離により対象外 (ui/styles.get_status_color が現行版に存在しない)")
    def test_thresholds_match_the_status_text(self):
        """丸印と文字が食い違うと、どちらが本当か分からなくなる。"""
        for percentage in (0.0, 59.9, 60.0, 84.9, 85.0, 99.9, 100.0):
            with self.subTest(percentage=percentage):
                text, _ = get_status_color(percentage)
                self.assertTrue(text.startswith(get_status_dot(percentage)))

    @unittest.skip("デスクトップGUI版はリポジトリ分離により対象外 (ui/styles.get_progress_bar_style が現行版に存在しない)")
    def test_the_bar_colour_uses_the_same_rule(self):
        """バーだけ赤で丸印は黄、という食い違いを防ぐ。"""
        limited_bar = get_progress_bar_style(100.0)
        self.assertNotEqual(get_progress_bar_style(99.0), limited_bar)
        self.assertEqual(get_progress_bar_style(99.96), limited_bar)

    def test_no_gauge_is_not_reported_as_healthy(self):
        """課金額だけの取得先を緑にすると、使用量が少ないという嘘になる。"""
        self.assertNotEqual(get_status_dot(None), "🟢")


class TestUsageStatus(unittest.TestCase):
    """VSCode 拡張へ渡す「判定済みの状態」。

    しきい値の判定は Python にしかありません。以前は同じ数値が
    store.ts と media/main.js にも書かれており、片方だけ直せば
    exe と VSCode で違う色に見える状態でした。
    """

    @unittest.skip(
        "デスクトップGUI版はリポジトリ分離により対象外 "
        "(ui/styles.py が無くなったため、検証対象だった「2箇所が同じ関数を指す」"
        "という前提自体が現行版には存在しない)"
    )
    def test_desktop_and_extension_share_one_definition(self):
        """ui/styles.py が持っているのは同じ関数そのものであること。

        値を書き写した2つ目の定義があると、片方だけ直したときに
        気づけません。
        """
        self.assertIs(get_status_dot, usage_status.status_dot)
        self.assertIs(is_limited, usage_status.is_limited)

    def test_level_matches_the_dot(self):
        """段階と丸印が食い違うと、バーの色と丸印が別のことを言う。"""
        pairs = ((0.0, LEVEL_OK), (59.9, LEVEL_OK), (60.0, LEVEL_CAUTION),
                 (99.94, LEVEL_CAUTION), (99.96, LEVEL_LIMITED), (100.0, LEVEL_LIMITED),
                 (None, LEVEL_UNKNOWN))
        for percentage, expected in pairs:
            with self.subTest(percentage=percentage):
                self.assertEqual(usage_status.level(percentage), expected)
                self.assertEqual(usage_status.status_dot(percentage),
                                 get_status_dot(percentage))

    def test_level_names_are_the_webview_css_classes(self):
        """level をそのままクラス名にしている (main.js は繋げるだけ)。

        ここが食い違うと、バーの色だけが付かないという分かりにくい壊れ方を
        します。CSS 側の名前を変えたらこのテストが落ちます。
        """
        css = os.path.join(EXTENSION_DIR, "media", "main.css")
        with open(css, encoding="utf-8") as f:
            text = f.read()
        for name in (LEVEL_OK, LEVEL_CAUTION, LEVEL_LIMITED):
            with self.subTest(level=name):
                self.assertIn(f".bar-fill.{name}", text)

    def test_annotate_adds_the_judgement_to_every_metric(self):
        result = build_result([
            percent_metric("five_hour", "5時間", 12.3),
            money_metric("total", "当月", 5.0),
        ])
        annotated = usage_status.annotate(result)

        self.assertEqual([m["level"] for m in annotated["metrics"]],
                         [LEVEL_OK, LEVEL_UNKNOWN])
        self.assertEqual(annotated["status"]["level"], LEVEL_OK)
        self.assertEqual(annotated["status"]["dot"], "🟢")

    def test_annotate_does_not_touch_the_original(self):
        """取得スレッドと呼び出し元が同じ辞書を触らないこと (apply_budget と同じ)。"""
        result = build_result([percent_metric("five_hour", "5時間", 12.3)])
        usage_status.annotate(result)
        self.assertNotIn("status", result)
        self.assertNotIn("level", result["metrics"][0])

    def test_summary_reports_the_most_pressed_quota(self):
        result = build_result([
            percent_metric("five_hour", "5時間", 12.3),
            percent_metric("week", "週間", 88.8),
        ])
        self.assertEqual(usage_status.summarize(result), "88.8%")

    def test_summary_keeps_the_provider_order_on_a_tie(self):
        """同率なら取得先が返した順で先に来たものを採る。"""
        result = build_result([
            percent_metric("first", "先", 50.0),
            percent_metric("second", "後", 50.0),
        ])
        result["metrics"][0]["display"] = "先の枠"
        result["metrics"][0]["kind"] = MONEY
        self.assertEqual(usage_status.summarize(result), "先の枠")

    def test_summary_shows_money_as_money(self):
        """上限は利用者が決めた任意の値なので、率だけでは判断できない。"""
        result = build_result([money_metric("total", "当月", 12.5, budget=100.0)])
        self.assertEqual(usage_status.summarize(result), "$12.50 / $100.00")

    def test_summary_without_a_gauge_falls_back_to_the_first_metric(self):
        result = build_result([money_metric("total", "当月", 3.0)])
        self.assertEqual(usage_status.summarize(result), "$3.00")
        self.assertEqual(usage_status.summarize(build_result([])), "-")

    def test_the_extension_javascript_holds_no_threshold(self):
        """拡張側に判定が戻っていないこと。

        **数値だけでなく判定する関数も置かないでください。** 拡張が
        自分で色を決め始めると、exe と VSCode で違う色に見える状態へ
        逆戻りします。

        しきい値の比較そのもの (`< 60` / `>= 100`) も見ます。名前を変えて
        書き直されても気づけるようにするためです。**コメントは対象外です。**
        「以前はここにあった」という説明は残す価値があるので、消えたことを
        確かめるのは実際のコードだけにします。
        """
        forbidden_names = ("CAUTION_PERCENT", "LIMITED_PERCENT", "statusDot", "is_limited")
        forbidden_patterns = (r"<\s*60\b", r">=\s*100\b")
        for path in _extension_javascript():
            with open(path, encoding="utf-8") as f:
                code = _strip_js_comments(f.read())
            for name in forbidden_names:
                with self.subTest(path=os.path.basename(path), name=name):
                    self.assertNotIn(name, code)
            for pattern in forbidden_patterns:
                with self.subTest(path=os.path.basename(path), pattern=pattern):
                    self.assertIsNone(re.search(pattern, code))


def _extension_javascript():
    """vsix に入る JavaScript の一覧 (拡張直下と media/)。"""
    paths = []
    for directory in (EXTENSION_DIR, os.path.join(EXTENSION_DIR, "media")):
        for name in sorted(os.listdir(directory)):
            if name.endswith(".js"):
                paths.append(os.path.join(directory, name))
    return paths


def _strip_js_comments(source: str) -> str:
    """コメントを落とします (完全な字句解析ではありません)。

    `https://` を行コメントと取り違えないように、直前が `:` の `//` は
    残します。この用途 (しきい値が本文に書かれていないかを見る) には
    これで足ります。
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", source)


class TestAccountProvider(unittest.TestCase):
    def test_missing_provider_defaults_to_claude(self):
        """provider を持たない既存の config.json をそのまま読めること。"""
        acc = Account.from_dict({"name": "既存", "organization_id": "", "cookie": "c"})
        self.assertEqual(acc.provider, "claude")

    def test_provider_roundtrips_through_config(self):
        directory = tempfile.mkdtemp(prefix="cum_provider_")
        path = os.path.join(directory, "config.json")

        cm = ConfigManager(path)
        self.assertTrue(cm.save([Account("課金", "", "sk-ant-admin01-x",
                                         provider="anthropic-cost")]))
        accounts, _ = ConfigManager(path).load()
        self.assertEqual(accounts[0].provider, "anthropic-cost")

    def test_repr_redacts_credential(self):
        acc = Account("n", "", "sk-ant-admin01-SECRET", provider="anthropic-cost")
        self.assertNotIn("SECRET", repr(acc))
        self.assertIn("anthropic-cost", repr(acc))


class TestBrowserProfilePaths(unittest.TestCase):
    def test_profile_dir_is_per_account_and_under_config_dir(self):
        from services import browser_profile
        from services.config_manager import default_config_dir

        a = browser_profile.profile_dir("11111111-2222-3333-4444-555555555555")
        b = browser_profile.profile_dir("99999999-8888-7777-6666-555555555555")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith(default_config_dir()))

    def test_profile_dir_rejects_path_traversal(self):
        from services import browser_profile
        directory = browser_profile.profile_dir("../../evil")
        self.assertNotIn("..", directory)
        self.assertTrue(directory.startswith(browser_profile.profiles_root()))


class TestProxyManager(unittest.TestCase):
    """社内プロキシの資格情報が壊れずに届くこと。"""

    def setUp(self):
        from services import proxy_manager
        self.pm = proxy_manager
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.pm.configure(dict(self.pm._settings, proxy_mode="system",
                               proxy_username="", proxy_password=""))

    def test_special_characters_are_escaped(self):
        """ドメイン付きIDや記号入りパスワードでも URL が壊れないこと。"""
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "corp\\user", "proxy_password": "p@ss:word//1",
        })
        url = self.pm.requests_proxies()["https"]
        # 生の記号が残っていると host/port の解釈が壊れる
        self.assertNotIn("\\", url)
        self.assertIn("%5C", url)
        self.assertIn("%40", url)
        self.assertTrue(url.endswith("@proxy.example.jp:8080"))

    def test_no_credentials_produces_bare_url(self):
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "proxy.example.jp", "proxy_port": 3128,
            "proxy_username": "", "proxy_password": "",
        })
        self.assertEqual(self.pm.requests_proxies()["http"], "http://proxy.example.jp:3128")

    def test_none_mode_bypasses_environment(self):
        os.environ["HTTPS_PROXY"] = "http://proxy.example.jp:8080"
        self.pm.configure({"proxy_mode": "none"})
        self.assertEqual(self.pm.requests_proxies(), {"http": None, "https": None})

    def test_system_mode_supplies_missing_credentials(self):
        """環境変数に資格情報が無いときだけ、保存した資格情報で補うこと。"""
        os.environ["HTTPS_PROXY"] = "http://proxy.example.jp:8080"
        self.pm.configure({
            "proxy_mode": "system", "proxy_username": "u", "proxy_password": "p",
            "proxy_host": "", "proxy_port": 8080,
        })
        self.assertEqual(self.pm.requests_proxies()["https"], "http://u:p@proxy.example.jp:8080")

    def test_system_mode_leaves_credentialed_environment_alone(self):
        os.environ["HTTPS_PROXY"] = "http://envuser:envpass@proxy.example.jp:8080"
        self.pm.configure({
            "proxy_mode": "system", "proxy_username": "u", "proxy_password": "p",
            "proxy_host": "", "proxy_port": 8080,
        })
        # None を返す = requests が環境変数をそのまま使う
        self.assertIsNone(self.pm.requests_proxies())

    def test_redacted_url_hides_password(self):
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "u", "proxy_password": "TOP-SECRET",
        })
        self.assertNotIn("TOP-SECRET", self.pm.redacted_proxy_url())

    def test_detect_from_environment(self):
        os.environ["HTTPS_PROXY"] = "http://envuser:envpass@proxy.example.jp:8888"
        detected = self.pm.detect_from_environment()
        self.assertEqual(detected["host"], "proxy.example.jp")
        self.assertEqual(detected["port"], 8888)
        self.assertEqual(detected["username"], "envuser")


class TestProxyCredentialHostScoping(unittest.TestCase):
    """[C-03] 環境変数由来のプロキシ資格情報を、無関係なホストへ渡さないこと。

    manual モードで環境変数とは別のプロキシを指定した場合、環境変数に入っている
    (多くは社内 AD の) 資格情報を使い回すと、それが無関係なホストへ送信されて
    しまう。credentials(host) は host が環境変数のプロキシと一致するときだけ
    資格情報を補う。
    """

    _ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

    def setUp(self):
        from services import proxy_manager
        self.pm = proxy_manager
        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HTTPS_PROXY"] = "http://envuser:envpass@ad-proxy.example.jp:8080"

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.pm.configure(dict(self.pm._settings, proxy_mode="system",
                               proxy_username="", proxy_password=""))

    def test_matching_host_uses_environment_credentials(self):
        """接続先が環境変数のプロキシと同じホストなら、資格情報を補うこと。"""
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "ad-proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "", "proxy_password": "",
        })
        self.assertEqual(self.pm.credentials("ad-proxy.example.jp"), ("envuser", "envpass"))
        # ホスト比較は大文字小文字を無視する
        self.assertEqual(self.pm.credentials("AD-PROXY.example.jp"), ("envuser", "envpass"))

    def test_mismatched_host_does_not_use_environment_credentials(self):
        """manual モードで環境変数とは別のプロキシを指定した場合、補わないこと。"""
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "other-proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "", "proxy_password": "",
        })
        self.assertEqual(self.pm.credentials("other-proxy.example.jp"), ("", ""))
        # requests_proxies() 経由でも社内資格情報が漏れないこと
        url = self.pm.requests_proxies()["https"]
        self.assertEqual(url, "http://other-proxy.example.jp:8080")
        self.assertNotIn("envuser", url)
        self.assertNotIn("envpass", url)

    def test_explicit_credentials_are_always_used_regardless_of_host(self):
        """設定画面で明示的に入力した資格情報は、ホストが一致しなくても常に使う。"""
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "other-proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "manual-user", "proxy_password": "manual-pass",
        })
        self.assertEqual(
            self.pm.credentials("other-proxy.example.jp"), ("manual-user", "manual-pass")
        )

    def test_host_omitted_keeps_the_previous_behaviour(self):
        """host を渡さない既存の呼び出し (has_credentials() 等) は従来どおり無条件に補う。"""
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "other-proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "", "proxy_password": "",
        })
        self.assertEqual(self.pm.credentials(), ("envuser", "envpass"))
        self.assertTrue(self.pm.has_credentials())


@unittest.skip("デスクトップGUI版はリポジトリ分離により対象外 (PySide6 QtNetwork のデスクトップ用低レベルAPI検証)")
class TestQtApplicationProxy(unittest.TestCase):
    """system モードで Qt の applicationProxy を壊さないこと。

    型 DefaultProxy を applicationProxy に設定すると自己参照になり、
    Qt が「プロキシなし」に倒す。すると Chromium はシステムのプロキシ設定を
    使わなくなり、claude.ai を自力で名前解決しようとして
    ERR_NAME_NOT_RESOLVED になる (実際に起きた不具合)。
    """

    def setUp(self):
        from PySide6.QtNetwork import QNetworkProxy
        from services import proxy_manager

        self.QNetworkProxy = QNetworkProxy
        self.pm = proxy_manager
        self._saved_proxy = QNetworkProxy.applicationProxy()
        self._saved_flag = proxy_manager._qt_proxy_overridden

    def tearDown(self):
        self.QNetworkProxy.setApplicationProxy(self._saved_proxy)
        self.pm._qt_proxy_overridden = self._saved_flag

    def test_system_mode_does_not_touch_application_proxy(self):
        # Qt が環境変数 / Windows の設定から拾った値を模す
        seed = self.QNetworkProxy()
        seed.setType(self.QNetworkProxy.ProxyType.HttpProxy)
        seed.setHostName("proxy.example.jp")
        seed.setPort(8080)
        self.QNetworkProxy.setApplicationProxy(seed)

        self.pm._qt_proxy_overridden = False
        self.pm.configure({"proxy_mode": "system", "proxy_username": "", "proxy_password": ""})

        current = self.QNetworkProxy.applicationProxy()
        self.assertEqual(current.type(), self.QNetworkProxy.ProxyType.HttpProxy)
        self.assertEqual(current.hostName(), "proxy.example.jp")
        self.assertEqual(current.port(), 8080)

    def test_manual_mode_sets_application_proxy(self):
        self.pm._qt_proxy_overridden = False
        self.pm.configure({
            "proxy_mode": "manual", "proxy_host": "manual.example.jp", "proxy_port": 3128,
            "proxy_username": "u", "proxy_password": "p",
        })
        current = self.QNetworkProxy.applicationProxy()
        self.assertEqual(current.type(), self.QNetworkProxy.ProxyType.HttpProxy)
        self.assertEqual(current.hostName(), "manual.example.jp")
        self.assertEqual(current.port(), 3128)
        self.assertEqual(current.user(), "u")

    def test_none_mode_disables_proxy(self):
        self.pm._qt_proxy_overridden = False
        self.pm.configure({"proxy_mode": "none"})
        self.assertEqual(
            self.QNetworkProxy.applicationProxy().type(),
            self.QNetworkProxy.ProxyType.NoProxy,
        )


@unittest.skip("デスクトップGUI版はリポジトリ分離により対象外 (ui.settings_dialog.ConnectionTestWorker が現行版に存在しない)")
class TestConnectionTestClassification(unittest.TestCase):
    """接続テストの判定。

    claude.ai は Cookie を送らないと 403 を返すため、
    「403 = 失敗」としてしまうと、プロキシが正常でも失敗と誤判定する。
    """

    class _Response:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

        def json(self):
            return json.loads(self.text)

    def _classify(self, status_code, text):
        from ui.settings_dialog import ConnectionTestWorker
        return ConnectionTestWorker._classify(self._Response(status_code, text))

    def test_api_reachable_but_unauthenticated_is_success(self):
        ok, message = self._classify(
            403, '{"type":"error","error":{"message":"Invalid authorization"}}'
        )
        self.assertTrue(ok)
        self.assertIn("接続に成功", message)

    def test_proxy_auth_failure(self):
        ok, _ = self._classify(407, "")
        self.assertFalse(ok)

    def test_cloudflare_challenge_is_not_success(self):
        ok, message = self._classify(403, "<html><title>Just a moment...</title></html>")
        self.assertFalse(ok)
        self.assertIn("Cloudflare", message)

    def test_html_block_page_is_not_success(self):
        ok, _ = self._classify(403, "<html>Access Denied by corporate policy</html>")
        self.assertFalse(ok)

    def test_plain_success(self):
        ok, _ = self._classify(200, '[{"uuid": "x"}]')
        self.assertTrue(ok)


class TestProxyPasswordStorage(unittest.TestCase):
    def test_password_is_encrypted_on_disk(self):
        directory = tempfile.mkdtemp(prefix="cum_proxy_")
        path = os.path.join(directory, "config.json")

        cm = ConfigManager(path)
        self.assertTrue(cm.save([], {
            "proxy_mode": "manual", "proxy_host": "proxy.example.jp", "proxy_port": 8080,
            "proxy_username": "corp\\user", "proxy_password": "TOP-SECRET",
        }))

        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("TOP-SECRET", raw)

        _, settings = ConfigManager(path).load()
        self.assertEqual(settings["proxy_password"], "TOP-SECRET")
        self.assertEqual(settings["proxy_username"], "corp\\user")
        self.assertEqual(settings["proxy_mode"], "manual")


class TestWindowSizeSettings(unittest.TestCase):
    """ウィンドウサイズが保存・復元されること。"""

    def test_window_size_roundtrip(self):
        directory = tempfile.mkdtemp(prefix="cum_size_")
        path = os.path.join(directory, "config.json")

        cm = ConfigManager(path)
        self.assertTrue(cm.save([], {"window_width": 1200, "window_height": 800}))

        _, settings = ConfigManager(path).load()
        self.assertEqual(settings["window_width"], 1200)
        self.assertEqual(settings["window_height"], 800)

    def test_broken_size_falls_back_to_default(self):
        from services.config_manager import DEFAULT_SETTINGS

        directory = tempfile.mkdtemp(prefix="cum_size_")
        path = os.path.join(directory, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"settings": {"window_width": "とても大きい"}, "accounts": []}, f,
                      ensure_ascii=False)

        _, settings = ConfigManager(path).load()
        self.assertEqual(settings["window_width"], DEFAULT_SETTINGS["window_width"])


class TestDatetimeUtil(unittest.TestCase):
    def test_naive_string_is_treated_as_utc(self):
        """タイムゾーンなしの文字列を UTC とみなすこと (JST で9時間ズレない)。"""
        aware = parse_utc_to_local("2026-08-05T13:45:00Z")
        naive = parse_utc_to_local("2026-08-05T13:45:00")
        self.assertEqual(aware, naive)

    def test_invalid_input(self):
        self.assertIsNone(parse_utc_to_local("not-a-date"))
        self.assertIsNone(parse_utc_to_local(None))
        self.assertIsNone(parse_utc_to_local(""))
        self.assertEqual(format_datetime(None), "N/A")

    def test_sub_minute_shows_seconds(self):
        """1秒タイマーで呼ばれるので、残り1分未満は秒まで出すこと。"""
        now = datetime.now(timezone.utc)
        self.assertEqual(get_remaining_time_str(now + timedelta(seconds=30)), "30秒後")
        self.assertEqual(get_remaining_time_str(now + timedelta(seconds=59)), "59秒後")

    def test_larger_units(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(get_remaining_time_str(now + timedelta(seconds=61)), "1分後")
        self.assertEqual(get_remaining_time_str(now + timedelta(seconds=3661)), "1時間1分後")
        self.assertEqual(get_remaining_time_str(now + timedelta(seconds=90061)), "1日1時間1分後")

    def test_past_and_none(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(get_remaining_time_str(now - timedelta(seconds=5)), "制限解除済み")
        self.assertEqual(get_remaining_time_str(None), "")


if __name__ == "__main__":
    unittest.main()
