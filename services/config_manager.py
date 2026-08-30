import json
import logging
import os
import shutil
import sys
import tempfile
from typing import List, Tuple

from models.account import Account
from services import secret_store
from services.i18n import t

logger = logging.getLogger(__name__)

# 環境変数でも設定ファイルの場所を差し替えられるようにしておく (検証・複数プロファイル用)
ENV_CONFIG_PATH = "AI_USAGE_MANAGER_CONFIG"
# アプリ名変更前の環境変数名。既存の設定を壊さないようフォールバックとして残す。
LEGACY_ENV_CONFIG_PATH = "CLAUDE_USAGE_MANAGER_CONFIG"

APP_DIR_NAME = "AI-UsageManager"
# アプリ名変更前 (Claude Usage Manager 時代) のデータフォルダ名
LEGACY_APP_DIR_NAME = "ClaudeUsageManager"

DEFAULT_SETTINGS = {
    "auto_update_enabled": False,
    "auto_update_interval_minutes": 5,
    # プロキシ設定
    #   system : Windows のプロキシ設定 / 環境変数に従う (既定)
    #   manual : 下記のホスト・ポートを使う
    #   none   : プロキシを使わない
    "proxy_mode": "system",
    "proxy_host": "",
    "proxy_port": 8080,
    "proxy_username": "",
    "proxy_password": "",
    # Azure OpenAI ゲートウェイの API キーを送ってよいドメイン。カンマ区切り。
    #   例: "example.com, gateway.example.co.jp"
    # example.com と書けば example.com 自身と *.example.com が通る。
    # 空なら制限しない (保存時の確認だけが歯止めになる)。
    # 詳細は services/providers/aoai_cost.py を参照。
    "aoai_allowed_hosts": "",
    # 前回終了時のウィンドウサイズ
    "window_width": 1040,
    "window_height": 720,
    # 表示まわり
    "ui_scale_percent": 100,   # 画面全体の拡大率 (%)
    "show_sidebar": True,      # 左のアカウント一覧を出すか
    # 前回サマリーを見ていたか。初回は全体像から入れるようサマリーで開く。
    "show_summary": True,
    # 画面に出すアカウントの並び (accountId の配列)。**accounts 配列そのものは
    # 並べ替えません。** あちらの格納順は「追加した順」そのもので、上書きすると
    # 「追加した順」に並べ替える手立てが無くなります。
    "account_order": [],
}


def _default_settings() -> dict:
    """DEFAULT_SETTINGS の複製を作ります。

    **list の既定値は必ず作り直します。** dict(DEFAULT_SETTINGS) は浅い
    コピーなので、可変の list をそのまま持つとモジュール全体で1つの
    オブジェクトを共有し、あるインスタンスの並べ替えが他のインスタンスにも
    既定値にも伝わってしまいます。
    """
    return {key: (list(value) if isinstance(value, list) else value)
            for key, value in DEFAULT_SETTINGS.items()}

# 保存時に暗号化する設定項目
SECRET_SETTINGS = {"proxy_password"}


class ConfigLoadError(Exception):
    """設定ファイルそのものが読み込めなかった (壊れている等) ことを表します。"""


def _config_base_dir() -> str:
    """アプリ用データフォルダを置く親ディレクトリ (%LOCALAPPDATA% 等) を返します。"""
    if sys.platform == "win32":
        return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def default_config_dir() -> str:
    """設定ファイルを置くディレクトリを返します。

    ソースディレクトリ直下に置くと OneDrive 等の同期対象になったり、
    フォルダごと共有・圧縮配布された際にセッション Cookie が流出するため、
    ユーザープロファイル配下 (%LOCALAPPDATA%) を既定にします。
    """
    return os.path.join(_config_base_dir(), APP_DIR_NAME)


def legacy_config_dir() -> str:
    """アプリ名変更前に使っていたデータフォルダのパス。"""
    return os.path.join(_config_base_dir(), LEGACY_APP_DIR_NAME)


def migrate_legacy_app_dir_if_needed() -> str:
    """旧アプリ名のデータフォルダを、新しい名前へリネームして引き継ぎます。

    フォルダごと改名するので、config.json だけでなくアカウントごとの
    ブラウザプロファイル (ログイン状態) もそのまま残ります。

    **ログ設定より先に、他のどのコードよりも早く呼ぶこと。**
    ログファイルもブラウザプロファイルもこのフォルダの中にあるため、
    誰かが先に開いてしまうとフォルダを掴まれてリネームできなくなります。

    戻り値は移行元のパス (移行が不要だった場合は None)。リネームできなかった
    場合は OSError を送出します。ログ設定より前に呼ぶ都合上ここでは logger を
    使えないので、記録は呼び出し側で行ってください。
    """
    legacy = legacy_config_dir()
    current = default_config_dir()

    if os.path.normcase(legacy) == os.path.normcase(current):
        return None
    # 新しい方がすでにあるなら移行済み。中身の統合は行わない (どちらが正か判断できない)。
    if os.path.exists(current) or not os.path.isdir(legacy):
        return None

    os.rename(legacy, current)
    return legacy


def legacy_config_path() -> str:
    """旧バージョンが使っていたソースディレクトリ直下の config.json のパス。"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "config.json")


class ConfigManager:
    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = (
                os.environ.get(ENV_CONFIG_PATH)
                or os.environ.get(LEGACY_ENV_CONFIG_PATH)
                or os.path.join(default_config_dir(), "config.json")
            )
        self.config_path = os.path.abspath(config_path)

        # 直近のロードで壊れていて読み飛ばしたエントリの説明。UI から警告表示に使う。
        self.load_warnings: List[str] = []
        # ロード自体に失敗したか。True の間は保存を抑止して既存データの上書き消失を防ぐ。
        self.load_failed = False
        # 起動時に旧パスから移行した場合、移行元のパスを保持する
        self.migrated_from: str = None

        self.settings = _default_settings()

    # ---------------- 移行処理 ----------------

    def migrate_legacy_config_if_needed(self) -> bool:
        """旧パス (ソースディレクトリ直下) の config.json を新しい保存先へ引き継ぎます。

        新しい保存先にすでにファイルがある場合は何もしません。
        移行元は削除せずに残します (誤って設定を失わないため)。
        戻り値は移行を実施したかどうか。
        """
        legacy = legacy_config_path()
        if os.path.exists(self.config_path) or not os.path.exists(legacy):
            return False
        if os.path.abspath(legacy) == self.config_path:
            return False

        try:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            shutil.copy2(legacy, self.config_path)
        except OSError as e:
            logger.error("旧設定ファイルの移行に失敗しました: %s", e)
            return False

        self.migrated_from = legacy
        logger.info("設定を %s から %s へ移行しました。", legacy, self.config_path)
        return True

    # ---------------- 読み込み ----------------

    def load(self) -> Tuple[List[Account], dict]:
        """アカウント一覧とアプリ設定をまとめて読み込みます。"""
        self.load_warnings = []
        self.load_failed = False
        self.settings = _default_settings()

        if not os.path.exists(self.config_path):
            return [], self.settings

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # ここで [] を返してしまうと、直後の保存操作で設定ファイルが
            # 空で上書きされ Cookie が恒久的に失われるため、失敗を明示する
            self.load_failed = True
            logger.error("設定ファイルを読み込めませんでした: %s", e)
            raise ConfigLoadError(str(e)) from e

        if not isinstance(data, dict):
            self.load_failed = True
            raise ConfigLoadError(
                t("The top level of the settings file is not an object."))

        raw_settings = data.get("settings")
        if isinstance(raw_settings, dict):
            for key, default in DEFAULT_SETTINGS.items():
                value = raw_settings.get(key, default)
                # 型が壊れていても既定値に倒して起動を止めない
                if isinstance(default, bool):
                    self.settings[key] = bool(value)
                elif isinstance(default, int):
                    try:
                        self.settings[key] = int(value)
                    except (TypeError, ValueError):
                        self.settings[key] = default
                elif isinstance(default, str):
                    text = value if isinstance(value, str) else default
                    if key in SECRET_SETTINGS and text:
                        decrypted = secret_store.decrypt(text)
                        if not decrypted and secret_store.is_encrypted(text):
                            self.load_warnings.append(t(
                                "The saved proxy password could not be decrypted. "
                                "Enter it again."
                            ))
                        text = decrypted
                    self.settings[key] = text
                elif isinstance(default, list):
                    # **必ず新しいリストにします。** 既定値の list をそのまま
                    # 持つと DEFAULT_SETTINGS 側の実体を共有してしまい、
                    # 並べ替えが既定値を書き換えます。
                    self.settings[key] = ([str(v) for v in value]
                                          if isinstance(value, list)
                                          else list(default))

        accounts_data = data.get("accounts", [])
        if not isinstance(accounts_data, list):
            self.load_warnings.append(
                t("accounts was ignored because it is not a list."))
            return [], self.settings

        accounts = []
        for index, raw in enumerate(accounts_data):
            # 1件の破損で全アカウントを失わないよう、必ず1件ずつ握る
            try:
                account = Account.from_dict(raw)
            except (ValueError, TypeError, AttributeError) as e:
                self.load_warnings.append(t(
                    "Account definition #{index} was skipped: {reason}",
                    index=index + 1, reason=e))
                logger.warning("アカウント定義 #%d の読み込みに失敗: %s", index + 1, e)
                continue

            if not account.name:
                account.name = t("(unnamed #{index})", index=index + 1)

            if account.cookie:
                decrypted = secret_store.decrypt(account.cookie)
                if not decrypted and secret_store.is_encrypted(account.cookie):
                    self.load_warnings.append(t(
                        "The cookie for '{name}' could not be decrypted. "
                        "Sign in again.",
                        name=account.name,
                    ))
                account.cookie = decrypted

            accounts.append(account)

        return accounts, self.settings

    def load_accounts(self) -> List[Account]:
        """アカウント一覧のみを読み込みます (旧 API との互換用)。"""
        accounts, _ = self.load()
        return accounts

    # ---------------- 保存 ----------------

    def save(self, accounts: List[Account], settings: dict = None) -> bool:
        """アカウントと設定を保存します。書き込みはアトミックに行います。"""
        if self.load_failed:
            # 壊れたファイルを読めなかった直後に上書きすると復旧不能になる
            logger.error("設定の読み込みに失敗しているため、上書き保存を中止しました。")
            return False

        if settings is not None:
            self.settings.update(settings)

        payload = {
            "settings": self._serialize_settings(),
            "accounts": [self._serialize_account(acc) for acc in accounts],
        }

        directory = os.path.dirname(self.config_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            logger.error("設定ディレクトリを作成できませんでした: %s", e)
            return False

        tmp_path = None
        try:
            # 同じディレクトリに一時ファイルを作ってから os.replace で差し替えることで、
            # 書き込み途中でプロセスが落ちても既存の設定が壊れないようにする
            fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_path)
            tmp_path = None
        except (OSError, TypeError, ValueError) as e:
            logger.error("設定の保存に失敗しました: %s", e)
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        self._restrict_permissions()
        return True

    def save_accounts(self, accounts: List[Account]) -> bool:
        """アカウントのみを保存します (旧 API との互換用)。"""
        return self.save(accounts)

    def _serialize_settings(self) -> dict:
        data = dict(self.settings)
        # プロキシのパスワードも Cookie と同じく暗号化して保存する
        for key in SECRET_SETTINGS:
            if data.get(key):
                data[key] = secret_store.encrypt(data[key])
        return data

    def _serialize_account(self, account: Account) -> dict:
        data = account.to_dict()
        # Cookie は保存時に必ず暗号化する (DPAPI が使えない環境では平文のまま)
        data["cookie"] = secret_store.encrypt(data.get("cookie") or "")
        return data

    def _restrict_permissions(self) -> None:
        """設定ファイルのパーミッションを本人のみに絞ります (POSIX のみ)。"""
        if sys.platform == "win32":
            # Windows では %LOCALAPPDATA% 配下が既定でユーザー専用のため追加処理は不要
            return
        try:
            os.chmod(self.config_path, 0o600)
        except OSError as e:  # pragma: no cover
            logger.warning("設定ファイルのパーミッション設定に失敗しました: %s", e)
