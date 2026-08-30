"""バックエンド (vscode-extension/backend/cli.py) の RPC を、実際に起動して確かめます。

**test_core.py とは別に置いています。** あちらは services/ models/ の原本を
import して中核ロジックを見るもので、**backend/ のコピーを掴んでいないこと**を
冒頭でアサートしています。こちらは逆に cli.py そのものを動かすので、同じ
ファイルに同居させると、その約束が読みにくくなります。

**import ではなく別プロセスで動かします。** cli.py は import しただけで
sys.stdout を差し替え、ログの出力先を決めます (プロトコルを守るための仕掛けで、
あれ自体は正しい)。テストの中でそれをやると、pytest の出力まで巻き込みます。

**本物の設定は触りません。** LOCALAPPDATA を一時フォルダに差し替えて起動する
ので、config.json もログもそちらへ作られます。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

CLI = os.path.join(PROJECT_ROOT, "vscode-extension", "backend", "cli.py")

from models.account import Account                                # noqa: E402
from services.config_manager import ConfigManager                 # noqa: E402


class BackendProcess:
    """cli.py を1本起動し、1行1 JSON で会話します。"""

    def __init__(self, env):
        self._next_id = 0
        self.proc = subprocess.Popen(
            [sys.executable, "-u", CLI],
            cwd=tempfile.gettempdir(), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8")
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("バックエンドが ready を返しませんでした")
            if json.loads(line).get("event") == "ready":
                return

    def call(self, method, params=None):
        self._next_id += 1
        request = {"id": self._next_id, "method": method}
        if params is not None:
            request["params"] = params
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("応答が返る前にバックエンドが終わりました")
            message = json.loads(line)
            # 通知 (id を持たない) は読み飛ばす
            if message.get("id") == self._next_id:
                return message

    def close(self):
        try:
            self.call("shutdown")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class BackendRpcTestCase(unittest.TestCase):
    """1本のバックエンドを共有し、テストごとに config.json を置き直します。"""

    # 設定フォルダの決まり方は services/config_manager.py の _config_base_dir
    # を参照。Windows は LOCALAPPDATA、それ以外は XDG_CONFIG_HOME。
    _CONFIG_HOME_VARS = ("LOCALAPPDATA", "XDG_CONFIG_HOME")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="aiusage-rpc-test-")
        # **このプロセスの環境も差し替えます。** setUp が ConfigManager を
        # 直接使って config.json を置くので、子プロセスの env だけ差し替えても
        # 足りません。**忘れると本物の設定を書き潰します。**
        cls._saved = {name: os.environ.get(name) for name in cls._CONFIG_HOME_VARS}
        for name in cls._CONFIG_HOME_VARS:
            os.environ[name] = cls.tmp
        cls.env = dict(os.environ)
        cls.env["PYTHONIOENCODING"] = "utf-8"
        cls.env["PYTHONUTF8"] = "1"
        cls.backend = BackendProcess(cls.env)

    @classmethod
    def tearDownClass(cls):
        cls.backend.close()
        for name, value in cls._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # **テストごとに置き直します。** 前のテストが書き換えたものを次が
        # 引き継ぐと、単体で走らせたときだけ落ちるテストになります。
        manager = ConfigManager()
        manager.save([
            Account(name="テスト Claude", organization_id="org-old",
                    cookie="sessionKey=old-value", provider="claude", id="acc-1"),
            Account(name="テスト AOAI", organization_id="https://example.invalid/",
                    cookie="old-api-key", provider="aoai-cost", id="acc-2",
                    budget=1234.0),
            # **資格情報が空のアカウント。** 作り話ではありません。config.json を
            # 手で書き換えた場合と、別の端末や別の Windows ユーザーから持ってきて
            # 復号できなかった場合に、実際にこうなります (secret_store は復号
            # できないと空文字を返し、config_manager がそれを cookie に入れる)。
            Account(name="資格情報なし", organization_id="", cookie="",
                    provider="claude", id="acc-3"),
        ], manager.settings)
        self.backend.call("list_accounts", {"reload": True})

    # ---- 助け ----

    def update(self, **params):
        return self.backend.call("update_account", params)

    @staticmethod
    def account(snapshot, account_id):
        return next(a for a in snapshot["accounts"] if a["id"] == account_id)

    @staticmethod
    def confirm_of(response):
        return (response.get("result") or {}).get("confirm")

    def current(self, account_id):
        return self.account(
            self.backend.call("list_accounts")["result"], account_id)


class TestUpdateAccount(BackendRpcTestCase):
    """登録内容の書き換え。**ログイン画面 (PySide6) は要りません。**"""

    def test_name_can_be_changed(self):
        response = self.update(accountId="acc-1", name="新しい名前")
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(self.account(response["result"], "acc-1")["name"],
                         "新しい名前")

    def test_empty_name_is_refused(self):
        response = self.update(accountId="acc-1", name="   ")
        self.assertFalse(response["ok"])

    def test_unknown_account_is_refused(self):
        response = self.update(accountId="居ないアカウント", name="x")
        self.assertFalse(response["ok"])

    def test_unknown_provider_is_refused(self):
        response = self.update(accountId="acc-1", provider="存在しない取得先")
        self.assertFalse(response["ok"])

    def test_credential_survives_when_it_is_not_sent(self):
        """**送らなかったものを消さないこと。**

        資格情報を毎回入力させないための約束です。ここが崩れると、名前を
        変えただけで保存済みの Cookie が消えます。
        """
        response = self.update(accountId="acc-1", name="名前だけ変更")
        account = self.account(response["result"], "acc-1")
        self.assertTrue(account["hasCredential"])
        self.assertEqual(account["name"], "名前だけ変更")

    def test_odd_credential_asks_first_and_saves_nothing(self):
        response = self.update(accountId="acc-1", credential="まったく別の文字列")
        self.assertTrue(self.confirm_of(response),
                        "形の違う資格情報は尋ねてくるはず")
        # 尋ねている間は保存していないこと
        self.assertTrue(self.current("acc-1")["hasCredential"])

    def test_odd_credential_is_saved_once_confirmed(self):
        response = self.update(accountId="acc-1", credential="まったく別の文字列",
                               confirmed=True)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertIsNone(self.confirm_of(response))

    def test_an_account_without_a_credential_can_still_be_edited(self):
        """**触っていない項目まで弾かないこと。**

        資格情報が空のまま保存されているアカウントは実在します (setUp の
        acc-3 の説明を参照)。そこで名前を変えるだけの編集を「Cookie を
        入力してください」で断ると、直す手立てがその場にありません。
        **同じ有効/無効の切り替えが、一覧からは通るのにここからは失敗する**
        ことにもなります (一覧は set_enabled を使うため)。
        """
        self.assertFalse(self.current("acc-3")["hasCredential"], "前提が違う")

        response = self.update(accountId="acc-3", name="名前を変えるだけ")
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(self.account(response["result"], "acc-3")["name"],
                         "名前を変えるだけ")

        response = self.update(accountId="acc-3", enabled=False)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertFalse(self.account(response["result"], "acc-3")["enabled"])

    def test_a_credential_cannot_be_emptied(self):
        """空にしようとしたときだけは断ること。今あるものを捨てる指示なので。"""
        response = self.update(accountId="acc-1", credential="   ")
        self.assertFalse(response["ok"])
        self.assertTrue(self.current("acc-1")["hasCredential"],
                        "断ったのに消えていてはいけない")

    def test_extra_is_only_checked_when_it_is_sent(self):
        """**触っていない項目の警告を出さないこと。**

        名前を変えただけで Organization ID の書式を問われると、その場に
        直す手立てがありません (ここは1項目ずつ直す画面です)。
        """
        response = self.update(accountId="acc-2", name="名前だけ")
        self.assertIsNone(self.confirm_of(response))

        response = self.update(accountId="acc-2", extra="https://example.invalid/")
        self.assertTrue(self.confirm_of(response),
                        "渡した追加項目は確かめられるはず")

    def test_switching_provider_resets_extra_and_budget(self):
        """**取得先を変えたら持ち越さないこと。**

        追加項目は意味が変わり (Organization ID / エンドポイント URL)、
        上限金額は通貨が変わります。
        """
        before = self.current("acc-2")
        self.assertGreater(before["budget"], 0)

        response = self.update(accountId="acc-2", provider="claude",
                               credential="sessionKey=sk-ant-sid01-新しい値")
        account = self.account(response["result"], "acc-2")
        self.assertEqual(account["provider"], "claude")
        self.assertEqual(account["budget"], 0)
        self.assertEqual(account["extra"], "")

    def test_switching_provider_requires_a_fresh_credential(self):
        """**取得先を変えたら、資格情報も持ち越さないこと。**

        Claude の Cookie は Azure の API キーではありません。持ち越すと、
        一覧では「設定済み」に見えるのに取得は必ず失敗する、という
        いちばん分かりにくい壊れ方をします。追加項目や上限金額を
        持ち越さないのと同じ理由です。
        """
        self.assertTrue(self.current("acc-2")["hasCredential"], "前提が違う")

        response = self.update(accountId="acc-2", provider="claude")
        self.assertFalse(response["ok"])

        # **断ったのだから、何も変わっていないこと。**
        after = self.current("acc-2")
        self.assertEqual(after["provider"], "aoai-cost")
        self.assertTrue(after["hasCredential"])

    def test_budget_must_be_a_number(self):
        response = self.update(accountId="acc-2", budget="いくらか")
        self.assertFalse(response["ok"])

    def test_a_budget_typed_as_text_is_accepted(self):
        """**打たれた文字のまま届くこと。**

        画面は上限金額を文字として送ります (media/main.js の submitForm)。
        数値へ直してから送ると、「1,000」や全角の数字が NaN になり、JSON へ
        載せる段で null に化けて、**利用者が打った内容が消えます。** 判定を
        こちらに一本化しているのは、そこで握り潰させないためです。
        """
        response = self.update(accountId="acc-2", budget="12345")
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(self.account(response["result"], "acc-2")["budget"], 12345)

    def test_a_budget_with_a_thousands_separator_is_refused(self):
        """**桁区切りは断ること。** 黙って 0 (上限なし) にしないこと。

        0 は「上限を設けない」の意味なので、そこへ倒すとゲージが消えます。
        画面には数字が見えているのに、です。
        """
        before = self.current("acc-2")["budget"]
        response = self.update(accountId="acc-2", budget="1,000")
        self.assertFalse(response["ok"])
        self.assertEqual(self.current("acc-2")["budget"], before,
                         "断ったのだから変わっていないこと")

    def test_budget_is_dropped_for_providers_without_one(self):
        """上限金額を持たない取得先に入れても、黙って捨てられること。"""
        response = self.update(accountId="acc-1", budget=999)
        self.assertEqual(self.account(response["result"], "acc-1")["budget"], 0)

    def test_enabled_can_be_toggled(self):
        response = self.update(accountId="acc-1", enabled=False)
        self.assertFalse(self.account(response["result"], "acc-1")["enabled"])

    def test_changes_survive_a_reload(self):
        self.update(accountId="acc-1", name="保存されたか", enabled=False)
        snapshot = self.backend.call("list_accounts", {"reload": True})["result"]
        account = self.account(snapshot, "acc-1")
        self.assertEqual(account["name"], "保存されたか")
        self.assertFalse(account["enabled"])


class TestListProviders(BackendRpcTestCase):
    """取得先の一覧。編集画面が「何を尋ねるか」を決めるのに使います。"""

    def test_lists_the_choices(self):
        result = self.backend.call("list_providers")["result"]
        by_id = {p["id"]: p for p in result["providers"]}
        self.assertIn("claude", by_id)
        self.assertIn("aoai-cost", by_id)
        self.assertFalse(any(p["retired"] for p in result["providers"]),
                         "取り下げた取得先は、頼まれない限り出さない")

    def test_labels_are_translated(self):
        """**訳すのはバックエンドです。** 拡張側にはもう訳す手立てがありません。"""
        result = self.backend.call("list_providers")["result"]
        claude = next(p for p in result["providers"] if p["id"] == "claude")
        self.assertTrue(claude["credentialLabel"])
        self.assertTrue(claude["needsCredential"], "Claude は資格情報が要る")

    def test_include_returns_retired_providers(self):
        """一覧から取り下げた取得先も、使っているアカウントがあるなら返すこと。"""
        result = self.backend.call(
            "list_providers", {"include": ["codex"]})["result"]
        codex = next((p for p in result["providers"] if p["id"] == "codex"), None)
        self.assertIsNotNone(codex, "include で頼んだものは返るはず")
        self.assertTrue(codex["retired"])

    def test_include_returns_unknown_ids_as_asked(self):
        """知らない ID でも、**頼まれた ID のまま**返すこと。

        書き換えられた設定ファイルには一覧に無い ID が入っていることがあり、
        返さないと編集画面がラベルすら出せません。id を既定のものへ書き換えて
        返すと、拡張側がそのアカウントの取得先として引けなくなります。
        """
        result = self.backend.call(
            "list_providers", {"include": ["存在しない取得先"]})["result"]
        found = next((p for p in result["providers"]
                      if p["id"] == "存在しない取得先"), None)
        self.assertIsNotNone(found)
        self.assertTrue(found["retired"])


class TestCreateAccount(BackendRpcTestCase):
    """アカウントの新規作成。**ログイン画面 (PySide6) は要りません。**

    削除・編集と同じ取り違えが、追加にも残っていました。窓を開いて作る道
    (add_account) は残してありますが、資格情報を手で貼るだけなら通りません。
    """

    def create(self, **params):
        return self.backend.call("create_account", params)

    def count(self):
        return len(self.backend.call("list_accounts")["result"]["accounts"])

    def test_an_api_key_account_can_be_created_without_a_window(self):
        before = self.count()
        response = self.create(provider="anthropic-cost", name="新しい API キー",
                               credential="sk-ant-admin-xxxxxxxxxxxx")
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(self.count(), before + 1)

        created = response["result"]["accountId"]
        self.assertTrue(created)
        account = self.account(response["result"], created)
        self.assertEqual(account["name"], "新しい API キー")
        self.assertTrue(account["hasCredential"])

    def test_defaults_of_the_provider_are_filled_in(self):
        """取得先ごとの既定値 (上限金額・追加項目) が入っていること。"""
        response = self.create(provider="aoai-cost", name="既定値の確認",
                               credential="dummy-key")
        self.assertTrue(response["ok"], response.get("error"))
        account = self.account(response["result"], response["result"]["accountId"])
        self.assertGreater(account["budget"], 0, "上限金額の既定が入るはず")

    def test_a_name_is_required(self):
        before = self.count()
        response = self.create(provider="anthropic-cost", name="   ",
                               credential="sk-ant-admin-xxxx")
        self.assertFalse(response["ok"])
        self.assertEqual(self.count(), before, "作りかけを残さないこと")

    def test_a_credential_is_required(self):
        before = self.count()
        response = self.create(provider="anthropic-cost", name="鍵なし")
        self.assertFalse(response["ok"])
        self.assertEqual(self.count(), before, "作りかけを残さないこと")

    def test_an_unknown_provider_is_refused(self):
        before = self.count()
        response = self.create(provider="存在しない取得先", name="x", credential="y")
        self.assertFalse(response["ok"])
        self.assertEqual(self.count(), before)

    def test_an_odd_credential_asks_first_and_creates_nothing(self):
        """**尋ねている間は作らないこと。**

        器だけ先に足して戻し忘れると、名前も資格情報も無いアカウントが
        一覧に居座ります。
        """
        before = self.count()
        response = self.create(provider="anthropic-cost", name="形が違う鍵",
                               credential="まったく別の文字列")
        self.assertTrue(self.confirm_of(response), "形の違う鍵は尋ねてくるはず")
        self.assertEqual(self.count(), before, "尋ねている間は作らないこと")

    def test_it_is_created_once_confirmed(self):
        before = self.count()
        response = self.create(provider="anthropic-cost", name="形が違う鍵",
                               credential="まったく別の文字列", confirmed=True)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(self.count(), before + 1)

    def test_it_survives_a_reload(self):
        response = self.create(provider="anthropic-cost", name="保存されたか",
                               credential="sk-ant-admin-xxxx")
        created = response["result"]["accountId"]
        snapshot = self.backend.call("list_accounts", {"reload": True})["result"]
        self.assertEqual(self.account(snapshot, created)["name"], "保存されたか")


class TestReorderAccounts(BackendRpcTestCase):
    """画面に出す並び順。**accounts 配列そのものは動かしません。**

    あちらの格納順は「追加した順」そのもので、並べ替えの基準の1つが
    それです。上書きしてしまうと二度と復元できないので、並び順は
    settings.account_order という別のキーに持ちます。
    """

    def reorder(self, order):
        return self.backend.call("reorder_accounts", {"order": order})

    @staticmethod
    def ids(snapshot):
        return [a["id"] for a in snapshot["accounts"]]

    def test_the_order_is_returned_in_the_snapshot(self):
        response = self.reorder(["acc-3", "acc-1", "acc-2"])
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["result"]["accountOrder"],
                         ["acc-3", "acc-1", "acc-2"])

    def test_the_accounts_array_keeps_the_order_it_was_added_in(self):
        """**ここが本題です。** 並べ替えても accounts の順は変わらないこと。

        変わってしまうと「追加した順」に並べ直す手立てが無くなります。
        """
        response = self.reorder(["acc-3", "acc-2", "acc-1"])
        self.assertEqual(self.ids(response["result"]), ["acc-1", "acc-2", "acc-3"])

    def test_the_order_survives_a_reload(self):
        """保存されていること (画面を開き直しても並びが戻らない)。"""
        self.reorder(["acc-2", "acc-3", "acc-1"])
        snapshot = self.backend.call("list_accounts", {"reload": True})["result"]
        self.assertEqual(snapshot["accountOrder"], ["acc-2", "acc-3", "acc-1"])
        self.assertEqual(self.ids(snapshot), ["acc-1", "acc-2", "acc-3"])

    def test_the_order_is_kept_without_a_reload(self):
        """読み直さずに引き直しても残っていること (手元の設定も更新されている)。"""
        self.reorder(["acc-3", "acc-1"])
        snapshot = self.backend.call("list_accounts")["result"]
        self.assertEqual(snapshot["accountOrder"], ["acc-3", "acc-1"])

    def test_a_partial_order_is_accepted(self):
        """**全件そろっている必要はありません。**

        突き合わせは画面側が毎回行うので、載っていない id は末尾へ回ります。
        ここで弾くと、取得の途中で1件増えただけで保存が通らなくなります。
        """
        response = self.reorder(["acc-2"])
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["result"]["accountOrder"], ["acc-2"])

    def test_an_empty_order_is_accepted(self):
        response = self.reorder([])
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["result"]["accountOrder"], [])

    def test_unknown_account_is_refused(self):
        """知らない id が混ざるのは、画面と手元の一覧がずれている印です。"""
        response = self.reorder(["acc-1", "居ないアカウント"])
        self.assertFalse(response["ok"])

    def test_a_duplicated_account_is_refused(self):
        response = self.reorder(["acc-1", "acc-1", "acc-2"])
        self.assertFalse(response["ok"])

    def test_a_refused_order_does_not_overwrite_the_saved_one(self):
        """弾いたときに、保存済みの並びを壊さないこと。"""
        self.reorder(["acc-3", "acc-2", "acc-1"])
        self.assertFalse(self.reorder(["acc-1", "acc-1"])["ok"])
        snapshot = self.backend.call("list_accounts", {"reload": True})["result"]
        self.assertEqual(snapshot["accountOrder"], ["acc-3", "acc-2", "acc-1"])

    def test_an_old_config_without_the_key_still_works(self):
        """**後方互換。** account_order を知らない設定ファイルでも動くこと。

        v1.7.0 やデスクトップ版が書いた config.json には、このキーが
        ありません。**そこから読んで、そのまま並べ替えられる**必要が
        あります。無い状態を作るためにファイルから消してから読み直します。
        """
        path = ConfigManager().config_path
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["settings"].pop("account_order", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        snapshot = self.backend.call("list_accounts", {"reload": True})["result"]
        self.assertEqual(snapshot["accountOrder"], [],
                         "キーが無ければ空の並びとして扱うこと")
        self.assertEqual(self.ids(snapshot), ["acc-1", "acc-2", "acc-3"])
        self.assertTrue(self.reorder(["acc-2", "acc-1"])["ok"],
                        "古い設定ファイルからでも並べ替えられること")


if __name__ == "__main__":
    unittest.main()
