"""VSCode 拡張のバックエンド。

拡張ホスト (Node) と標準入出力で会話します。1行が1つの JSON です。

    要求  {"id": 1, "method": "fetch_usage", "params": {...}}
    応答  {"id": 1, "ok": true,  "result": {...}}
          {"id": 1, "ok": false, "error": "...", "authError": false}
    通知  {"event": "...", "data": {...}}          (id を持たない、backend 発)

**標準出力はこのプロトコル専用です。** ライブラリが print したものが
1行でも混ざるとフレームが壊れ、拡張側は「無反応」としか見えなくなります。
起動直後に sys.stdout を stderr へ差し替え、本物の出力先はこのモジュールだけが
持ちます。ログも同じ理由で必ず stderr へ出します。

デスクトップ版 (main.py) とは **同じ config.json を共有します**。

アカウントの登録・編集・再ログインは、すべてアプリ内ブラウザ (QtWebEngine) の
ログイン画面で行います。QtWebEngine はこのプロセスでは動かせないため
(下記)、gui_helper.py を別プロセスとして起動して任せます。

**削除はここで完結させます。** 消すのに窓は要らないのに、これも
gui_helper.py へ回していたため、PySide6 が入っていない環境では削除まで
「PySide6 を入れてください」で止まっていました。窓が要る操作と要らない
操作を、同じ理由で同じ道に通さないでください。

**このプロセスで QtWebEngine のオブジェクトを作らないでください。** ここには
QApplication がありません。QWebEngineProfile などはその場でプロセスごと
落ちることがあり、拡張からは「バックエンドが無言で死んだ」としか見えません。
具体的には services.browser_profile を import しないこと (import しただけで
QtWebEngineCore を引き込みます)。プロファイルが要る操作は gui_helper.py の担当です。
**ただし、保存場所を知ることと消すことに Qt は要りません。** そのぶんは
services.profile_storage に分けてあり (Qt を import しません)、削除で
ログイン状態を捨てるときはこちらを使います。

なお PySide6 が一切登場しないわけではありません。proxy_manager は
QtNetwork を import します (ImportError は握って素通りします) が、
QtNetwork は QApplication を必要としないため問題ありません。
禁じているのは「Qt 全般」ではなく「QApplication を要求するもの」です。
"""

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))

# services / models の探し先。
#   vsix に固めたあと … build_vsix.py がこのファイルの隣へコピーする
#   リポジトリで直接動かすとき … まだコピー前なのでプロジェクト直下にある
# **コピーを取るのは、vsix が拡張ディレクトリの外を参照できないためです。**
# 取得ロジックを二重に持つわけではなく、配布時に同じものを同梱します。
for _root in (_HERE, os.path.abspath(os.path.join(_HERE, "..", ".."))):
    if os.path.isdir(os.path.join(_root, "services")):
        sys.path.insert(0, _root)
        break

# --- 標準出力の保護 ---------------------------------------------------------
# アプリ本体を import するより前に退避する。import しただけで何かを print する
# モジュールがあっても、プロトコルへ漏れないようにするため。
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

# **プロトコルは行き帰りとも UTF-8 で固定します。** 拡張ホストからの起動時は
# backend.js が PYTHONIOENCODING=utf-8 を渡してきますが、それが渡ってこない
# 起動 (手で動かす、別のランチャから呼ぶ) では Windows の既定が cp932 になり、
# どちらの向きも壊れます。**環境変数に頼らず、こちら側で決めます。**
#
#   書き (stdout) … cp932 に無い文字を1つ書いた時点で UnicodeEncodeError に
#                   なり、応答が丸ごと落ちて「バックエンドが無言で死んだ」
#                   ように見えます。応答にはアカウント名や取得先が返した
#                   エラー文が載ります。**どちらもこちらが決める値ではない**
#                   ので、cp932 に収まる保証はありません (¥ (U+00A5) や
#                   絵文字は実際に入りません)。
#   読み (stdin)  … 要求の JSON は UTF-8 のバイト列で届きます。cp932 として
#                   読むと壊れ、しかも**その場では失敗しません**。壊れた文字列
#                   (サロゲート) が保存まで運ばれ、書き出しの段で初めて落ちます。
#                   いま要求に載るのは UUID などの ASCII だけなので、こちらは
#                   予防です。**片方向だけ直しても意味がありません**
#                   — 自由入力を受け取るメソッドを足した瞬間に、
#                   静かに壊れる側へ戻ります。
for _stream in (_PROTOCOL_OUT, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        # 差し替え済みのストリーム等で reconfigure が無い場合は、
        # 環境変数任せのまま続行する (ここで止める理由はない)。
        pass

# 2 で fetch_usage の usage に status と metrics[].level / dot を足しました
# (しきい値の判定を拡張側から引き取ったため)。拡張はこれが載っている前提で
# 描画するので、片方だけ差し替えても動くようには作っていません
# (vsix には両方が同梱されるので、食い違うのは開発中に混ぜたときだけです)。
PROTOCOL_VERSION = 2
MIN_PYTHON = (3, 9)

_write_lock = threading.Lock()

logger = logging.getLogger("backend")


# app.log の書き出し。大きさと世代数はデスクトップ版 (旧 main.py) と同じです。
LOG_FILENAME = "app.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3


def _install_file_handler(root: logging.Logger) -> None:
    """app.log への書き出しを足します。**失敗しても起動は止めません。**

    ログを残せないことは、バックエンドが動かない理由にはならないためです。
    """
    from logging.handlers import RotatingFileHandler

    path = os.path.join(default_config_dir(), LOG_FILENAME)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8", delay=True,
        )
    except OSError as e:
        root.warning("%s へ書き出せません (出力チャンネルにだけ残します): %s", path, e)
        return
    # 出力チャンネルと違い、あとから読み返すためのものなので時刻を入れます。
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)


def setup_logging():
    """ログは stderr と app.log の両方へ。

    stderr は拡張側が出力チャンネルへ流します。**それだけでは足りません。**
    出力チャンネルは VSCode を閉じると消えるので、閉じたあとに
    「さっき失敗したときのログ」を読む手段がありませんでした。
    gui_helper.py が想定外のエラーで出す案内 (「詳細は app.log を」) の
    宛先でもあり、書き出しを止めていた間、あの案内は空振りしていました。

    **app.log へ書くのはこのプロセスだけです。** gui_helper.py は別プロセス
    ですが、あちらの stderr は run_gui_helper がこちらの logger へ流し込むので、
    それでこのファイルに載ります。2つのプロセスから同じファイルを開くと、
    Windows では世代交代 (rename) が相手に掴まれて失敗します。
    """
    level_name = (os.environ.get("AI_USAGE_MANAGER_LOGLEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.addHandler(handler)
    _install_file_handler(root)


def send(payload: dict):
    """1行の JSON を拡張へ送ります。

    改行は JSON 側に現れない (ensure_ascii=False でも \\n はエスケープされる)
    ので、行区切りがそのままフレーム区切りになります。
    """
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _write_lock:
        _PROTOCOL_OUT.write(line + "\n")
        _PROTOCOL_OUT.flush()


def reply(request_id, result):
    send({"id": request_id, "ok": True, "result": result})


def reply_error(request_id, message: str, auth_error: bool = False):
    send({"id": request_id, "ok": False, "error": message, "authError": auth_error})


def notify(event: str, data=None):
    send({"event": event, "data": data if data is not None else {}})


# --- 起動前の点検 -----------------------------------------------------------
# **アプリ本体を import する前に済ませます。** import してから
# ModuleNotFoundError を拾うと、拡張側には「終了コード 1」としか見えず、
# 利用者は何を入れれば直るのか分からないままになります。

# **翻訳だけは点検より前に入れます。** ここで出す文言は「Python が古い」
# 「requests が無い」という、この拡張で最初に、そして最も多く読まれる
# メッセージです。これが読めない言語で出ると、直し方に辿り着けません。
# services/i18n.py が import するのは標準ライブラリだけなので、
# 「本体を import する前に点検する」という上の約束は破りません。
from services.i18n import t  # noqa: E402


def preflight() -> str:
    """動かせない理由があれば、その説明を返します (無ければ空文字)。"""
    if sys.version_info < MIN_PYTHON:
        return t(
            "Python {required} or later is required "
            "(found: {found} / {executable}).",
            required=f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            found=sys.version.split()[0], executable=sys.executable,
        )

    import importlib.util

    missing = [
        name for name in ("requests", "urllib3")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        return t(
            "Required packages were not found: {packages}\n"
            "Python in use: {executable}\n\n"
            "Install them with:\n{command}",
            packages=', '.join(missing), executable=sys.executable,
            command=f'"{sys.executable}" -m pip install ' + " ".join(missing),
        )
    return ""


_problem = preflight()
if _problem:
    notify("fatal", {"message": _problem})
    sys.exit(2)


from models.account import Account                      # noqa: E402
from services import (                                  # noqa: E402
    profile_storage, providers, proxy_manager, usage_status,
)
from services.providers import aoai_cost  # noqa: E402
from services.config_manager import (                   # noqa: E402
    ConfigLoadError, ConfigManager, default_config_dir,
)
from services.providers import AUTH_COOKIE, AUTH_OAUTH, UsageError  # noqa: E402
from services.secret_store import is_encryption_available  # noqa: E402


# ---------------------------------------------------------------------------


class Backend:
    """設定の読み書きと使用状況の取得をまとめた本体。

    **取得は1件ずつ順番に行います** (デスクトップ版の process_auto_queue と
    同じ方針)。同時に投げると、同じ取得先へ並行してリクエストが飛び、
    レート制限に当たったときに何件目で当たったのか分からなくなります。
    """

    def __init__(self):
        self.config_manager = ConfigManager()
        self.accounts = []
        self.load_error = None
        self._fetch_queue = queue.Queue()
        self._worker = threading.Thread(target=self._fetch_loop, daemon=True)
        self._stopping = False
        # ログイン画面を開いている間は、その1つだけを走らせる。同じ設定
        # ファイルを2つのダイアログが書き戻すと、後から閉じた方が
        # 先の変更を消してしまう。
        self._gui_lock = threading.Lock()
        # 中止 (cancel_gui) の受け渡し。
        #
        # **_gui_lock とは別の錠が要ります。** あちらは利用者がログイン画面を
        # 操作している間ずっと握られたままなので、中止を受け付けるために
        # 取ろうとすると、まさに中止したい場面で待たされることになります。
        # こちらは下の3つを読み書きする一瞬だけ握ります。
        self._gui_state_lock = threading.Lock()
        # 走っているヘルパーの Popen。掴んでおかないと外から終了させられません
        # (subprocess.run では戻り値しか手に入らない)。
        self._gui_process = None
        # ヘルパーの起動を決めてから片付け終えるまで True。Popen を掴む前でも
        # 中止を受け付けられるようにするための印です。
        self._gui_running = False
        # 中止を頼まれたか。**Popen を掴む前に届くことがあります。** その場合は
        # run_gui_helper が起動直後にこれを見て、その場で終了させます。
        self._gui_cancelled = False

    # ---------------- 設定 ----------------

    def load(self):
        self.config_manager.migrate_legacy_config_if_needed()
        try:
            self.accounts, _ = self.config_manager.load()
            self.load_error = None
        except ConfigLoadError as e:
            # デスクトップ版と同じ方針: 空リストのまま保存に進むと Cookie を
            # 失うので、読めなかったことを持ち回って保存を止める。
            self.accounts = []
            self.load_error = str(e)

        proxy_manager.configure(self.config_manager.settings)
        # API キーの送信を許すドメイン。設定を読んだ側から渡す
        # (プロバイダに設定ファイルを読みに行かせない)。
        aoai_cost.configure(self.config_manager.settings)

    def save(self) -> bool:
        if self.load_error:
            logger.error("設定を読み込めていないため、保存しません。")
            return False
        return self.config_manager.save(self.accounts, self.config_manager.settings)

    def find(self, account_id: str):
        return next((a for a in self.accounts if a.id == account_id), None)

    def delete_account(self, account_id: str) -> dict:
        """アカウントを1件消します。

        **gui_helper.py は使いません。** 削除にログイン画面は要らないので、
        PySide6 が入っていない環境でも消せるようにここで完結させます
        (このファイル冒頭の説明を参照)。

        戻り値は {"ok": True, "accountId": ...} か
        {"ok": False, "error": ...} です。**例外は投げません。**
        呼び出し側は要求への応答を必ず1つ返す必要があるためです。
        """
        account = self.find(account_id)
        if account is None:
            return {"ok": False, "error": t("The account was not found.")}

        kept = self.accounts
        self.accounts = [a for a in kept if a.id != account.id]
        if not self.save():
            # **保存できなかったものを、消えたことにしません。** 手元だけ
            # 消すと、画面からは消えたのに config.json には残り、次に
            # 読み直したときに戻ってきます。
            self.accounts = kept
            return {"ok": False, "error": t(
                "The settings could not be saved. See the log for details.")}

        logger.info("アカウントを削除しました: %s", account.name)

        # 保存されたログイン状態 (Cookie を含む) も一緒に捨てる。**消し残す
        # と、消したはずのセッションがディスクに残ります。** 利用者が消す
        # と言ったのはまさにこれなので、応答を返す前にここで済ませます。
        #
        # 中身はブラウザのキャッシュを含むので、消し終わるまで数秒かかる
        # ことがあります。その間この読み取りループは止まります (取得は
        # 別スレッドなので、遅れるのは次の要求を受け取る時刻だけです)。
        # **別スレッドへ逃がさないこと。** 逃がすと、削除の直後に VSCode を
        # 閉じた場合にプロセスごと消えて、後始末が黙って行われません。
        profile_storage.remove_profile(account.id)
        return {"ok": True, "accountId": account.id}

    # ---------------- 直列化 ----------------

    @staticmethod
    def account_payload(account: Account) -> dict:
        """アカウント1件を、画面に出せる形へ直します。

        **資格情報そのものは決して載せません。** Webview は DevTools で中身を
        覗ける実行環境なので、Cookie や API キーを送る理由がありません。
        画面が必要とするのは「設定済みかどうか」だけです。
        """
        provider = account.get_provider()
        needs_credential = provider.auth_kind != AUTH_OAUTH
        return {
            "id": account.id,
            "name": account.name,
            "provider": account.provider,
            "providerLabel": provider.label,
            "enabled": account.enabled,
            "implemented": provider.implemented,
            "canRelogin": provider.auth_kind == AUTH_COOKIE,
            # プロバイダのクラス属性は訳す前の原文 (英語) です。画面へ渡す
            # ここが訳す場所になります (拡張側にはもう訳す手立てがありません)。
            "credentialLabel": t(provider.credential_label),
            "needsCredential": needs_credential,
            "hasCredential": bool(account.cookie),
            "extraLabel": (t(provider.extra_field_label)
                           if provider.uses_extra_field else ""),
            "extra": account.organization_id if provider.uses_extra_field else "",
            "budget": account.budget,
        }

    def is_fetchable(self, account: Account) -> bool:
        """今この場で取得を試せるアカウントかどうか (デスクトップ版と同じ判定)。"""
        if not account.enabled:
            return False
        provider = account.get_provider()
        if not provider.implemented:
            return False
        if provider.auth_kind != AUTH_OAUTH and not account.cookie:
            return False
        return True

    def snapshot(self) -> dict:
        return {
            "accounts": [self.account_payload(a) for a in self.accounts],
            "fetchable": [a.id for a in self.accounts if self.is_fetchable(a)],
            "configPath": self.config_manager.config_path,
            "configDir": default_config_dir(),
            "loadError": self.load_error,
            "encryptionAvailable": is_encryption_available(),
        }

    # ---------------- 取得 ----------------

    def start(self):
        self._worker.start()

    def stop(self):
        self._stopping = True
        self._fetch_queue.put(None)
        if self._worker.is_alive():
            # 取得中のものは応答を返しきってから終わる。待たずに抜けると、
            # 拡張側は「投げたのに返事が来ない」状態のまま取り残される。
            # 相手が応答しない場合に備えて上限は設ける。
            self._worker.join(timeout=20)

    def enqueue_fetch(self, request_id, account_id: str):
        self._fetch_queue.put((request_id, account_id))

    def _fetch_loop(self):
        while not self._stopping:
            item = self._fetch_queue.get()
            if item is None:
                break
            request_id, account_id = item
            try:
                self._fetch_one(request_id, account_id)
            except Exception as e:  # 取得スレッドは何があっても落とさない
                logger.exception("使用状況の取得中に想定外のエラー")
                reply_error(request_id, t("Unexpected error: {reason}", reason=e))

    def _fetch_one(self, request_id, account_id: str):
        account = self.find(account_id)
        if account is None:
            reply_error(request_id, t(
                "The account was not found (the settings may have changed)."))
            return
        if not self.is_fetchable(account):
            reply_error(request_id, t(
                "This account cannot be fetched right now "
                "(disabled, not implemented, or missing its credential)."))
            return

        provider = account.get_provider()
        # ワーカーと同じく、値をコピーしてから使う
        credential = account.cookie
        organization_id = account.organization_id

        notify("fetching", {"accountId": account_id})

        try:
            data = provider.fetch_usage(credential, organization_id)
        except UsageError as e:
            if not getattr(e, "auth_error", False):
                reply_error(request_id, str(e), False)
                return

            # 失効していた。**いきなりログイン画面を出す前に、画面を出さない
            # 復帰を1回だけ試します。** ブラウザプロファイルにログイン状態が
            # 残っていることは多く、その場合は利用者が何もしなくても戻せます。
            if not self.try_silent_refresh(account):
                reply_error(request_id, str(e), True)
                return

            # ヘルパーが config.json を書き換えたので読み直しています。
            # **掴み直さないと、古い方の Cookie を書き戻してしまいます。**
            account = self.find(account_id)
            if account is None:
                reply_error(request_id, t(
                    "The account was not found (the settings may have changed)."))
                return
            credential = account.cookie
            organization_id = account.organization_id
            try:
                data = provider.fetch_usage(credential, organization_id)
            except UsageError as e:
                reply_error(request_id, str(e),
                            bool(getattr(e, "auth_error", False)))
                return

        # 取得中に判明した値を設定へ書き戻す。デスクトップ版の
        # on_fetch_success と同じ内容 (片方だけ更新されると、両方から
        # 使ったときに Gemini の短命な Cookie が期限切れのまま残る)。
        refreshed_credential = data.pop("credential", None)
        changed = False

        resolved = data.get("organization_id")
        if resolved and resolved != account.organization_id:
            account.organization_id = resolved
            changed = True

        if refreshed_credential and refreshed_credential != account.cookie:
            account.cookie = refreshed_credential
            changed = True
            logger.info("'%s' の %s を自動更新しました。", account.name, provider.credential_label)

        if changed:
            self.save()

        # 上限金額はアカウント側の設定なので、取得後にここで被せる
        if account.budget:
            data = providers.apply_budget(data, account.budget)

        # しきい値の判定 (色の段階・丸印・1行要約) はここで済ませてから渡します。
        # **拡張の JavaScript 側には判定を持たせません。** 同じ数値を2つの言語に
        # 書き写すことになり、片方だけ直したときに exe と VSCode で違う色に
        # 見えるためです。予算を被せた後に呼ぶのは、予算から出る利用率
        # (money_metric の utilization) も判定の対象に含めるためです。
        data = usage_status.annotate(data)

        reply(request_id, {"accountId": account_id, "usage": data})

    def try_silent_refresh(self, account) -> bool:
        """失効した Cookie を、窓を出さずに取り直せないか1回だけ試します。

        デスクトップ版の SessionRefresher の移植です。**ここでは Qt に触れ
        ません** (このファイルの冒頭を参照)。窓を出さないだけで中身は
        QtWebEngine なので、ほかのログイン操作と同じく gui_helper.py を
        別プロセスで起動して任せます。

        **ログイン画面が開いている間は試しません。** あちらは開いたときの
        アカウント一覧を最後に丸ごと書き戻すので、その最中にこちらが
        config.json を書くと、先に書いた方が消えます。錠 (begin_gui) を
        取れなければ、今回は諦めて通常どおり失効として返します。

        取れたかどうかだけを返します。新しい Cookie は gui_helper が
        config.json へ書いているので、呼び出し元は読み直して拾います。
        """
        provider = account.get_provider()
        if provider.auth_kind != AUTH_COOKIE or not provider.home_url:
            return False
        if not self.begin_gui():
            logger.info("ログイン画面が開いているため、画面なしの復帰は見送ります。")
            return False
        try:
            logger.info("'%s' の %s が失効しました。画面を出さずに取り直せないか試します。",
                        account.name, provider.credential_label)
            result = self.run_gui_helper("silent_refresh", account.id)
        finally:
            self.end_gui()

        if not result.get("ok") or not result.get("changed"):
            logger.info("画面なしでは戻せませんでした (account=%s, 状態=%s)。",
                        account.id, result.get("status") or result.get("error"))
            return False

        logger.info("画面を出さずに '%s' の %s を取り直しました。",
                    account.name, provider.credential_label)
        # ヘルパーが書いた config.json を読み直す。
        self.load()
        return True

    # ---------------- ブラウザ画面が要る操作 ----------------

    def begin_gui(self) -> bool:
        """ログイン画面の操作を1つ始めてよいかを決め、始めるなら印を立てます。

        **必ず読み取りループのスレッドから、要求が届いた順のまま呼んでください。**
        錠を取るのを gui_operation の中 (別スレッド) に置いていた時期があり、
        add_account の直後に届いた cancel_gui が「中止できる操作はありません」と
        言って素通りする取りこぼしがありました。中止はまさに「押してすぐ」
        届くものなので、要求を捌く順番のまま印が立っていないと、この隙間は
        必ず突かれます。

        始められないとき (すでに1つ走っている) は False を返します。**待ちません。**
        あちらは利用者がログイン画面を操作している数分間ずっと錠を持つので、
        待つ作りにすると、その間この要求が返らないまま拡張側が固まります。
        """
        if not self._gui_lock.acquire(blocking=False):
            return False
        with self._gui_state_lock:
            self._gui_running = True
            self._gui_process = None
            self._gui_cancelled = False
        return True

    def end_gui(self) -> None:
        """begin_gui() で立てた印を片付け、錠を返します。

        **必ず finally から呼ぶこと。** 返し忘れると、以後の追加・編集・
        再ログインが全部「別の操作が進行中です」で弾かれ続けます。
        """
        with self._gui_state_lock:
            self._gui_running = False
            self._gui_process = None
            self._gui_cancelled = False
        self._gui_lock.release()

    def gui_in_progress(self) -> bool:
        """ログイン画面の操作が1つ走っているか。

        **削除の可否を決めるのに使います。** ログイン画面は開いた時点の
        アカウント一覧を丸ごと抱えていて、閉じるときにそれを書き戻します。
        その最中にこちらが1件消しても、あとから上書きされて戻ってきます。
        """
        with self._gui_state_lock:
            return self._gui_running

    def run_gui_helper(self, mode: str, account_id: str = "") -> dict:
        """gui_helper.py を別プロセスで起動し、終わるまで待ちます。

        **begin_gui() で場所を取ってから呼びます** (取るのは呼び出し側の
        仕事です。理由は begin_gui の説明を参照)。

        **待ち時間に上限を設けません。** 利用者がログイン画面で操作している
        時間そのものなので、こちらの都合で打ち切ると、ログインし終えた頃には
        結果が捨てられていた、ということになります。打ち切ってよいのは
        利用者自身が中止したときだけで、その入口が cancel_gui です。

        **subprocess.run ではなく Popen + communicate() で書いています。**
        run() は終わるまで Popen を返さないので、走っているプロセスを外の
        スレッドから終了させる手段がありません。ウィンドウを一つも作らない
        まま固まった子プロセスが実際にあり、その状態では中止するしか
        利用者に打つ手がないため、掴める形にしてあります。communicate() にも
        timeout は渡しません (上と同じ理由)。

        受け渡しはファイル経由です。QtWebEngine (Chromium) は Python の
        sys.stdout を経由せず OS のファイル記述子へ直接書くことがあり、
        標準出力に混ざると結果を読めなくなります。
        """
        script = os.path.join(_HERE, "gui_helper.py")
        if not os.path.exists(script):
            return {"ok": False,
                    "error": t("The helper was not found: {path}", path=script)}

        python = os.environ.get("AI_USAGE_MANAGER_GUI_PYTHON") or sys.executable

        handle, result_path = tempfile.mkstemp(prefix="aiusage-gui-", suffix=".json")
        os.close(handle)
        try:
            command = [python, "-u", script, mode, "--result", result_path]
            if account_id:
                command += ["--id", account_id]
            logger.info("ログイン画面を開きます: %s", " ".join(command))

            try:
                process = subprocess.Popen(
                    # 作業ディレクトリを拡張の中に置かない (backend.js の spawn と
                    # 同じ理由: Windows では掴んだフォルダを rename できず、
                    # 拡張の更新・アンインストールが失敗する)。
                    command, cwd=tempfile.gettempdir(),
                    # **標準入力は必ず切り離します (既定の None にしないこと)。**
                    #
                    # 既定では子がこちらの標準入力をそのまま引き継ぎます。それは
                    # 拡張ホストから要求が流れてくるパイプそのもので、渡してよい
                    # ものではありません。ヘルパーは標準入力を読みませんが、
                    # 「読まないから害はない」とは言えません:
                    #
                    #   1. 実測では、これを引き継がせると子が Qt の初期化まで
                    #      到達せずに止まります (ワーキングセット 9.5MB、CPU
                    #      0.03 秒、ウィンドウなし)。当時ここを通っていた、窓を
                    #      出さない削除でも再現し、DEVNULL にすると再現しなく
                    #      なります。
                    #   2. 仮に止まらなくても、要求の JSON が流れているパイプを
                    #      2 つのプロセスが読める状態にしてはいけません。1 バイト
                    #      でも子に渡ると、こちらはフレームを失います。
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                )
            except OSError as e:
                return {"ok": False,
                        "error": t("The sign-in window could not be started "
                                   "({python}): {reason}", python=python, reason=e)}

            with self._gui_state_lock:
                self._gui_process = process
                cancel_now = self._gui_cancelled

            # **起動したことをここで1つだけ知らせます。** 拡張側はこれを見て
            # 「ウィンドウの準備をしています」から「別ウィンドウで操作して
            # ください」へ進めます。起動もしていないうちからそう案内すると、
            # まだ存在しない窓を探させることになります。
            # (窓が出たことの保証ではありません。それはこちらからは分かりません。)
            notify("gui_started", {"mode": mode, "accountId": account_id,
                                   "pid": process.pid})

            if cancel_now:
                # 起動する前に中止が届いていた。ここで拾わないと、誰も
                # 止められないプロセスが1つ残ります。
                logger.info("起動前に中止されていたため、すぐ終了させます。")
                self._kill_gui_process(process)

            stdout_text, stderr_text = process.communicate()
            for stream, label in ((stdout_text, "out"), (stderr_text, "err")):
                for line in (stream or "").splitlines():
                    if line.strip():
                        logger.info("[gui:%s] %s", label, line)

            with self._gui_state_lock:
                cancelled = self._gui_cancelled

            if cancelled:
                # **結果ファイルは読みません。** 利用者がやめると言った以上、
                # 中途半端に書かれていたものを採用する理由がありません。
                return {"ok": True, "cancelled": True}

            try:
                with open(result_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {
                    "ok": False,
                    "error": t(
                        "The sign-in window process exited without leaving a "
                        "result (exit code {code}). "
                        'Run "AI-UsageManager: Show Log" to see the details.',
                        code=process.returncode,
                    ),
                }
        finally:
            # 印と錠の片付けは end_gui() (gui_operation の finally) の担当です。
            # ここで消すと、まだ応答を返していないうちに次の操作を受け付けて
            # しまいます。ここで片付けるのは自分で作った一時ファイルだけ。
            try:
                os.remove(result_path)
            except OSError:
                pass

    @staticmethod
    def _kill_gui_process(process) -> None:
        """ログイン画面のプロセスを終了させます。

        **終了要求 (terminate) だけでは足りないことがあります。** 相手は
        QtWebEngine を抱えていて、固まっているときは要求を処理する
        イベントループが回っていません。Windows の terminate() は
        TerminateProcess なのでそれでも効きますが、POSIX の SIGTERM は
        握り潰されうるので、少し待って残っていれば kill() します。

        **communicate() で待っているスレッドはそのままです。** プロセスが
        消えればパイプが閉じ、あちらは自然に返ります。こちらから
        wait() を呼んで待ち合わせに割り込む必要はありません。
        """
        try:
            process.terminate()
        except OSError as e:
            # すでに終わっている場合もここへ来る。残っていれば下の poll で拾う。
            logger.warning("ログイン画面のプロセスに終了を要求できませんでした: %s", e)

        # 3 秒。人が待つ時間ではなく、OS がプロセスを畳む時間なので短くてよい。
        for _ in range(30):
            if process.poll() is not None:
                return
            time.sleep(0.1)

        logger.warning("終了要求に応じないため強制終了します (pid=%s)。", process.pid)
        try:
            process.kill()
        except OSError as e:
            logger.warning("強制終了できませんでした: %s", e)

    def cancel_gui(self) -> dict:
        """走っているログイン画面のプロセスを終了させます。

        **_gui_lock は取りません。** あの錠はログイン画面を開いている間
        ずっと握られたままなので、中止のために取ろうとすると、まさに中止
        したい場面で永久に待つことになります。守る対象も違います
        (あちらは config.json、こちらは Popen の受け渡しだけ)。

        待っている add_account 等への応答はここでは返しません。kill された
        gui_operation 側が「中止されました」として返します。要求ひとつに
        応答ひとつ、という約束を崩さないためです。
        """
        with self._gui_state_lock:
            if not self._gui_running:
                return {"cancelled": False,
                        "message": t("There is no operation to cancel.")}
            self._gui_cancelled = True
            process = self._gui_process

        if process is None:
            # 起動を決めてから Popen を掴むまでの隙間。印だけ残しておけば、
            # run_gui_helper が掴んだ直後に終了させます。
            logger.info("ログイン画面の起動前に中止を受け付けました。")
            return {"cancelled": True, "message": t("Cancelled.")}

        logger.info("ログイン画面を中止します (pid=%s)。", process.pid)
        self._kill_gui_process(process)
        return {"cancelled": True, "message": t("Cancelled.")}

    def gui_operation(self, request_id, mode: str, account_id: str = ""):
        """ヘルパーを走らせ、終わったら設定を読み直して結果を返します。

        **呼ぶ前に begin_gui() が True を返していること** (錠を取るのは
        読み取りループのスレッドの仕事です。理由は begin_gui の説明を参照)。

        中止 (cancel_gui) されたときも、ここは**必ず応答を返します。**
        返さずに黙って抜けると、拡張側は要求を投げたまま待ち続け、
        「中止したのに通知が消えない」という元の症状に戻ります。
        中止は失敗ではないので、キャンセルされたダイアログと同じく
        cancelled を立てた snapshot として返します。
        """
        try:
            result = self.run_gui_helper(mode, account_id)
        finally:
            # 中止されて途中で抜けても、ここを通って必ず解放します。握った
            # ままにすると、以後の追加・編集・再ログインが全部弾かれます。
            self.end_gui()

        if not result.get("ok"):
            reply_error(request_id, result.get("error") or t("The operation failed."))
            return

        # ヘルパーが config.json を書き換えているので、必ず読み直す。
        # 読み直さないと、こちらの手元は操作前のままになる。
        self.load()

        snapshot = self.snapshot()
        snapshot["cancelled"] = bool(result.get("cancelled"))
        snapshot["accountId"] = result.get("accountId", "")
        reply(request_id, snapshot)


# ---------------------------------------------------------------------------


# 接続テストの宛先。**Cookie を送らずに叩ける先**であることが条件です。
PROXY_TEST_URL = "https://claude.ai/api/organizations"


def _test_proxy_connection() -> tuple:
    """プロキシを通って外まで届くかを確かめ、(可否, 表示用の説明) を返します。

    デスクトップ版の設定画面にあった接続テストの移植です。**Cookie は
    送りません。** 送らなければ API は「認証されていない」と答えるので、
    その答えが返ってくること自体が「プロキシを抜けて相手まで届いた」
    証拠になります。認証情報を持ち出さずに経路だけを試せます。

    落ちた理由をここで見分けるのは、利用者に打つ手を示すためです。
    「つながりません」だけでは、プロキシの設定・資格情報・社内の CA 証明書の
    どれを直せばよいのか分かりません。
    """
    import requests

    from services.providers import resolve_verify
    from services.providers.claude import ClaudeProvider

    # 実際の取得と同じヘッダを使う (Cookie だけ外す)。ヘッダが違うと、
    # 本番では弾かれるのにテストだけ通る、という食い違いが起きます。
    headers = ClaudeProvider()._headers("")
    headers.pop("Cookie", None)

    try:
        response = requests.get(
            PROXY_TEST_URL, timeout=20,
            proxies=proxy_manager.requests_proxies(PROXY_TEST_URL),
            verify=resolve_verify(), headers=headers,
        )
    except requests.exceptions.ProxyError as e:
        return False, t("The proxy could not be reached.\n\n{reason}", reason=e)
    except requests.exceptions.SSLError as e:
        return False, t(
            "An SSL error occurred. If a corporate proxy inspects traffic, "
            "point the REQUESTS_CA_BUNDLE environment variable at its CA "
            "certificate.\n\n{reason}", reason=e)
    except requests.RequestException as e:
        return False, t("The connection failed.\n\n{reason}", reason=e)

    if response.status_code == 407:
        return False, t(
            "Proxy authentication failed (407). Check the user name and "
            "password.")

    body = response.text or ""
    if "Just a moment" in body or "cf-browser-verification" in body:
        return False, t(
            "The proxy was passed, but Cloudflare's browser check blocked the "
            "request. The in-app sign-in window (a real browser) may still get "
            "through.")

    try:
        response.json()
    except ValueError:
        return False, t(
            "claude.ai answered HTTP {code}, but the reply could not be read. "
            "A corporate proxy may be returning a block page instead.",
            code=response.status_code)

    if response.status_code in (401, 403):
        return True, t(
            "Connected (HTTP {code}). The request was refused because no "
            "credentials were sent, which means the proxy was passed and "
            "claude.ai was reached.", code=response.status_code)
    return True, t("Connected (HTTP {code}).", code=response.status_code)


class Dispatcher:
    def __init__(self, backend: Backend):
        self.backend = backend

    def handle(self, request: dict):
        request_id = request.get("id")
        method = request.get("method") or ""
        params = request.get("params") or {}

        handler = getattr(self, f"do_{method}", None)
        if handler is None:
            reply_error(request_id, t("Unknown method: {method}", method=method))
            return
        handler(request_id, params)

    # ---- メソッド ----

    def do_ping(self, request_id, params):
        reply(request_id, {
            "protocol": PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "configDir": default_config_dir(),
        })

    def do_list_accounts(self, request_id, params):
        if params.get("reload"):
            self.backend.load()
        reply(request_id, self.backend.snapshot())

    def do_fetch_usage(self, request_id, params):
        account_id = params.get("accountId") or ""
        self.backend.enqueue_fetch(request_id, account_id)

    def do_set_enabled(self, request_id, params):
        account = self.backend.find(params.get("accountId") or "")
        if account is None:
            reply_error(request_id, t("The account was not found."))
            return
        account.enabled = bool(params.get("enabled"))
        if not self.backend.save():
            reply_error(request_id, t(
                "The settings could not be saved. See the log for details."))
            return
        reply(request_id, self.backend.snapshot())

    # ---- プロキシ ----
    #
    # **設定の主は拡張側 (VSCode の設定) です。** ここは受け取って
    # config.json へ書き写し、proxy_manager へ反映するだけにしています。
    # 同じ値が2箇所に別々に住むと、どちらが効いているのか誰にも分からなく
    # なるためです。
    #
    # **パスワードだけは例外で、ここが主です。** VSCode の settings.json は
    # 平文で、Settings Sync により他の端末へも複製され得ます。パスワードは
    # secret_store (Windows では DPAPI) で暗号化してから保存します。

    def _save_proxy(self, request_id) -> bool:
        """書き換えた設定を保存し、走っている取得にも反映します。"""
        if not self.backend.save():
            reply_error(request_id, t(
                "The settings could not be saved. See the log for details."))
            return False
        proxy_manager.configure(self.backend.config_manager.settings)
        return True

    def do_get_proxy(self, request_id, params):
        settings = self.backend.config_manager.settings
        reply(request_id, {
            "mode": settings.get("proxy_mode", "system"),
            "host": settings.get("proxy_host", ""),
            "port": settings.get("proxy_port", 8080),
            "username": settings.get("proxy_username", ""),
            # **パスワードそのものは返しません。** 有無さえ分かれば、
            # 拡張側は「まだ設定されていません」と案内できます。
            "hasPassword": bool(settings.get("proxy_password", "")),
        })

    def do_set_proxy(self, request_id, params):
        settings = self.backend.config_manager.settings
        mode = params.get("mode") or "system"
        if mode not in ("system", "manual", "none"):
            reply_error(request_id, t("Unknown proxy mode: {mode}", mode=mode))
            return
        try:
            port = int(params.get("port", settings.get("proxy_port", 8080)))
        except (TypeError, ValueError):
            reply_error(request_id, t("The proxy port must be a number."))
            return

        settings["proxy_mode"] = mode
        settings["proxy_host"] = str(params.get("host") or "").strip()
        settings["proxy_port"] = port
        settings["proxy_username"] = str(params.get("username") or "").strip()
        if self._save_proxy(request_id):
            reply(request_id, {"saved": True})

    def do_set_proxy_password(self, request_id, params):
        # 空文字は「消す」という指示です。拡張側は空欄の確定を消去として
        # 案内しているので、ここで未指定と区別しません。
        self.backend.config_manager.settings["proxy_password"] = str(
            params.get("password") or "")
        if self._save_proxy(request_id):
            reply(request_id, {"saved": True})

    def do_detect_proxy(self, request_id, params):
        """環境変数 / Windows の設定から入力候補を拾います。**保存はしません。**

        どこに入れるかは拡張側 (VSCode の設定) の担当なので、ここで
        config.json へ書くと、設定画面に出ていない値が効いてしまいます。
        """
        found = proxy_manager.detect_from_environment()
        reply(request_id, {
            "found": bool(found.get("host")),
            "host": found.get("host", ""),
            "port": found.get("port", 8080),
        })

    def do_test_proxy(self, request_id, params):
        """接続テスト。**読み取りループでは待ちません。**

        最大20秒かかるので、ここで待つとその間ほかの要求が一切通りません
        (_start_gui と同じ理由です)。
        """
        threading.Thread(
            target=self._test_proxy_worker, args=(request_id,), daemon=True,
        ).start()

    @staticmethod
    def _test_proxy_worker(request_id):
        try:
            reachable, message = _test_proxy_connection()
        except Exception as e:
            logger.error("接続テストに失敗:\n%s", traceback.format_exc())
            reply_error(request_id, t("Unexpected error: {reason}", reason=e))
            return
        reply(request_id, {"reachable": reachable, "message": message})

    # ---- ブラウザ画面が要る操作 ----

    def _start_gui(self, request_id, mode: str, account_id: str = ""):
        """ヘルパーの完了を別スレッドで待ちます。

        **読み取りループを止めないこと。** ここで待つと、利用者がログイン画面を
        開いている数分間、一覧の取得を含むほかの要求が一切通らなくなります。

        **場所取り (begin_gui) だけはこのスレッドで、スレッドを起こす前に
        済ませます。** 向こう側に任せると、直後に届いた cancel_gui が
        「中止できる操作はありません」と言って素通りします (begin_gui の
        説明を参照)。
        """
        if not self.backend.begin_gui():
            reply_error(request_id, t(
                "Another sign-in operation is in progress. "
                "Finish that one first."))
            return

        try:
            threading.Thread(
                target=self.backend.gui_operation,
                args=(request_id, mode, account_id),
                daemon=True,
            ).start()
        except RuntimeError as e:
            # スレッドを作れなかった。取った場所をここで返さないと、以後の
            # ログイン操作が全部「別の操作が進行中です」で弾かれ続けます。
            self.backend.end_gui()
            reply_error(request_id, t(
                "The sign-in operation could not be started: {reason}", reason=e))

    def do_add_account(self, request_id, params):
        self._start_gui(request_id, "add")

    def do_edit_account(self, request_id, params):
        self._start_gui(request_id, "edit", params.get("accountId") or "")

    def do_relogin(self, request_id, params):
        self._start_gui(request_id, "relogin", params.get("accountId") or "")

    def do_delete_account(self, request_id, params):
        """アカウントを1件消します。

        **ここだけは _start_gui を通しません。** 追加・編集・再ログインは
        ログイン画面が要るので PySide6 も要りますが、削除に窓は要りません。
        以前はこれも同じ道を通していたため、PySide6 が入っていない環境では
        「PySide6 を入れてください」と言われ、拡張の中だけではアカウントを
        消せませんでした。

        **ログイン画面が開いている間は断ります** (gui_in_progress の説明)。
        """
        if self.backend.gui_in_progress():
            reply_error(request_id, t(
                "Another sign-in operation is in progress. "
                "Finish that one first."))
            return

        result = self.backend.delete_account(params.get("accountId") or "")
        if not result.get("ok"):
            reply_error(request_id, result["error"])
            return

        snapshot = self.backend.snapshot()
        snapshot["accountId"] = result["accountId"]
        reply(request_id, snapshot)

    def do_cancel_gui(self, request_id, params):
        """進行中のログイン画面を終了させます。

        **通知を閉じるだけでは足りないので、拡張から呼ばれます。** 子プロセスが
        ウィンドウを一つも作らないまま固まることがあり、放っておくと
        _gui_lock を握り続けて、以後の追加・編集・再ログインが全部
        「別の操作が進行中です」で弾かれ続けます。

        **この応答はすぐ返します。** 読み取りループのスレッドで動くので、
        ここで待つと中止の要求そのものが届かなくなります (実際に待つのは
        _kill_gui_process の中の数百ミリ秒だけ)。
        """
        reply(request_id, self.backend.cancel_gui())

    def do_shutdown(self, request_id, params):
        reply(request_id, {})
        self.backend.stop()
        raise SystemExit(0)


def main():
    setup_logging()

    backend = Backend()
    backend.load()
    backend.start()
    dispatcher = Dispatcher(backend)

    notify("ready", {"protocol": PROTOCOL_VERSION})

    # readline で1行ずつ読む。拡張側が閉じると EOF (空文字) が返るので、
    # そこで素直に終了する (親が死んだのに残り続けないため)。
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            logger.error("JSON として読めない入力を無視しました: %.120s", line)
            continue
        try:
            dispatcher.handle(request)
        except SystemExit:
            raise
        except Exception as e:
            logger.error("要求の処理に失敗:\n%s", traceback.format_exc())
            reply_error(request.get("id"), t("Unexpected error: {reason}", reason=e))

    backend.stop()


if __name__ == "__main__":
    main()
