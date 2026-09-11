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

**アカウントの操作は、すべてこのプロセスの中で完結します。** 追加・編集・
削除・表示・更新のどれにも別ウィンドウは要りません。入力を受けるのは拡張の
画面 (webview) の中のフォームで、資格情報は利用者が普段のブラウザから
取ってきて、そこへ貼ります。

以前は、追加と再ログインをアプリ内ブラウザ (QtWebEngine) のログイン画面で
行い、それを gui_helper.py という別プロセスに任せていました。**その経路は
畳みました。** 理由は2つあります。

1つ目。**窓が要らない操作まで、同じ道に通していました。** 削除にも編集にも
窓は要らないのに gui_helper.py へ回していたため、PySide6 が入っていない
環境ではアカウントを消すことすらできませんでした。追加も同じで、あちらが
最初に出すのはただのフォームです — ブラウザはその中の「ログイン」を押して
初めて出てきます。**押さない人にまで QtWebEngine の導入を強いていた**
わけです。同じ取り違えを3回繰り返しました。

2つ目。**埋め込みブラウザは、認証側から拒まれることがあります。** Google が
そうです。それは埋め込んだ側がパスワード入力を覗けるという理由で存在する
保護なので、偽装して通そうとするのは筋が悪い。**普段お使いのブラウザで
取ってきてもらう**ほうが正しく、そちらは大抵ログイン済みでもあります。

失ったものが1つあります。**Claude のセッションを黙って延長する仕組み**です。
あれはアプリ内ブラウザのプロファイルに乗っていたので、一緒に無くなりました。
期限切れは利用者に伝わり、貼り直してもらうことになります。

**このプロセスで QtWebEngine のオブジェクトを作らないでください。** ここには
QApplication がありません。QWebEngineProfile などはその場でプロセスごと
落ちることがあり、拡張からは「バックエンドが無言で死んだ」としか見えません。
具体的には services.browser_profile を import しないこと (import しただけで
QtWebEngineCore を引き込みます)。**ただし、保存場所を知ることと消すことに
Qt は要りません。** そのぶんは services.profile_storage に分けてあり
(Qt を import しません)、削除で以前のログイン状態を捨てるときはこちらを
使います。

なお PySide6 が一切登場しないわけではありません。proxy_manager は
QtNetwork を import します (ImportError は握って素通りします) が、
QtNetwork は QApplication を必要としないため問題ありません。
禁じているのは「Qt 全般」ではなく「QApplication を要求するもの」です。
"""

import json
import logging
import os
import queue
import sys
import threading
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
    **app.log へ書くのはこのプロセスだけです。** 2つのプロセスから同じ
    ファイルを開くと、Windows では世代交代 (rename) が相手に掴まれて
    失敗します。
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
from services.providers import AUTH_OAUTH, UsageError  # noqa: E402
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

        **別ウィンドウは出ません** (このファイル冒頭の説明を参照)。

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

    def reorder_accounts(self, order: list) -> dict:
        """画面に出す並び順を保存します。**accounts 配列は動かしません。**

        あちらの格納順は「追加した順」そのもので、書き換えると
        「追加した順」に並べ替える手立てが無くなります (snapshot が
        accounts をそのまま渡し、画面側が accountOrder と突き合わせます)。

        **全件そろっている必要はありません。** 突き合わせは画面側が毎回
        行うので、足りない id があっても壊れません。弾くのは「知らない id が
        混ざっている」「同じ id が二度出る」ときだけです。どちらも画面が
        持っている一覧と手元の一覧がずれている印で、そのまま保存すると
        利用者が見ていたのとは違う並びが残ります。

        **例外は投げません** (要求への応答を必ず1つ返すため)。
        """
        known = {a.id for a in self.accounts}
        wanted = [str(i) for i in (order or [])]
        if len(set(wanted)) != len(wanted) or not set(wanted) <= known:
            return {"ok": False, "error": t(
                "The account list has changed. Open the tab again and try once more.")}

        kept = list(self.config_manager.settings.get("account_order") or [])
        self.config_manager.settings["account_order"] = wanted
        try:
            saved = self.save()
        except Exception:  # noqa: BLE001
            # ConfigManager.save は現実的な失敗 (OSError 等) を自分で握って
            # False を返しますが、それ以外で抜けると下の巻き戻しが飛ばされ、
            # **保存されていない並びが手元だけ正しいものとして残ります。**
            # ここで受けるのは、例外を投げない約束 (docstring) のためでも
            # あります。
            logger.error("並び順の保存に失敗:\n%s", traceback.format_exc())
            saved = False
        if not saved:
            # delete_account と同じ作法です。**保存できなかったものを、
            # 保存できたことにしません。** 手元だけ変えると、次に読み直した
            # ときに前の並びが戻ってきます。
            self.config_manager.settings["account_order"] = kept
            return {"ok": False, "error": t(
                "The settings could not be saved. See the log for details.")}
        return {"ok": True}

    # 変更を頼まれていない項目の印。
    #
    # **None を使えません。** 「名前を変えない」と「名前を空にする」は
    # 別の指示で、None をどちらかに割り当てると、もう片方を表せなく
    # なります。params にキーが載っていなければ触らない、というのが
    # ここの約束です。
    _UNSET = object()

    def update_account(self, params: dict) -> dict:
        """アカウント1件の登録内容を書き換えます。

        **別ウィンドウは出ません** (このファイル冒頭の説明を参照)。

        **params に載っているキーだけを触ります。** 載っていなければ
        「変えない」、空文字なら「空にする」です。資格情報を毎回
        送らせない (送らなければ保存済みのものが残る) ためで、
        デスクトップ版のダイアログが編集モードで欄を空のまま開くのと
        同じ考え方です (ui/account_dialog.py の _effective_cookie)。

        通す検証は ui/account_dialog.py の validate_inputs と同じもの、
        同じ順序です。**入口が変わっても検証の中身は変えないでください。**
        貼り間違いを拾えるかどうかが画面によって変わると、こちらから
        入れた値だけが素通りします。

        **きっかけだけは違います。** あちらは1画面で全項目を確定するので
        全部を見ますが、こちらは1項目ずつ直すので、渡された項目だけを
        見ます。触っていない項目の警告を出しても、直す手立てがその場に
        ありません (名前を変えただけで Organization ID の書式を問われる)。

        利用者に尋ねる必要のある警告 (「〜には見えません」) が出たときは
        **保存せず** {"confirm": [...]} を返します。呼び出し側が尋ねて、
        confirmed を立てて呼び直してください。デスクトップ版が既定を
        「いいえ」にして尋ねているものを、こちらで黙って通さないためです。

        戻り値は次のいずれかです。**例外は投げません。** 呼び出し側は
        要求への応答を必ず1つ返す必要があるためです。

            {"error": "..."}       … 保存できない
            {"confirm": ["..."]}   … 尋ねてから呼び直してほしい
            {"saved": True}        … 保存した
        """
        account = self.find(params.get("accountId") or "")
        if account is None:
            return {"error": t("The account was not found.")}

        # --- 取得先 ---
        # **最初に決めます。** ラベルも既定値も検証も、どの取得先かで
        # 変わるためです。
        provider_id = params.get("provider", self._UNSET)
        if provider_id is self._UNSET:
            provider_id = account.provider
        else:
            provider_id = (provider_id or "").strip()
            if not providers.is_known(provider_id):
                return {"error": t("Unknown provider: {id}", id=provider_id)}
        switched = provider_id != account.provider
        provider = providers.get(provider_id)

        # --- 名前 ---
        name = params.get("name", self._UNSET)
        name = account.name if name is self._UNSET else (name or "").strip()
        if not name:
            return {"error": t("Enter an account name.")}

        confirms = []

        # --- 資格情報 ---
        typed = params.get("credential", self._UNSET)
        if typed is self._UNSET:
            # **取得先を変えたなら持ち越しません** (追加フィールドや上限金額と
            # 同じ理由です)。Claude の Cookie は Azure の API キーではないので、
            # 持ち越すと一覧では「設定済み」に見えるのに取得は必ず失敗する、
            # という状態になります。そのまま下の検査に落ちて、新しいものを
            # 求められます。
            credential = "" if switched else account.cookie
        else:
            typed = (typed or "").strip()
            # **絞る前に見ること。** normalize_credential は貼り付け元が
            # 伝えてきたエラーごと捨てるので、通したあとでは二度と読めません。
            paste_warning = provider.validate_paste(typed) if typed else ""
            credential = provider.normalize_credential(typed) if typed else ""
            if typed:
                if (provider.credential_marker
                        and provider.credential_marker not in credential):
                    confirms.append(t(
                        "Nothing that looks like {marker}... was found in "
                        "what you entered.", marker=provider.credential_marker))
                warning = paste_warning or provider.validate_credential(credential)
                if warning:
                    confirms.append(warning)

        # OAuth 系は別ツールのログイン状態を借りるので、ここに入れるものが無い。
        #
        # **触っていないなら通します。** 資格情報が空のまま保存されている
        # アカウントは実在します — config.json を手で書き換えた場合と、
        # 別の端末や別の Windows ユーザーから持ってきて復号できなかった
        # 場合です (services/secret_store.py は復号できないと空文字を
        # 返し、config_manager がそれを cookie に入れます)。そこで名前を
        # 変えるだけの編集まで「Cookie を入力してください」で弾くと、
        # **直す手立てがその場にありません。** 同じ有効/無効の切り替えが、
        # 一覧からは通るのにこちらからは失敗する、ということにもなります。
        #
        # 断るのは「空にしようとしたとき」と「取得先を変えたのに新しいものが
        # 無いとき」だけです。どちらも、そのまま保存すると使えないものが
        # 残ります。
        if (provider.auth_kind != AUTH_OAUTH and not credential
                and (typed is not self._UNSET or switched)):
            return {"error": t("Enter {credential}.",
                               credential=t(provider.credential_label))}

        # --- 追加フィールド ---
        extra = params.get("extra", self._UNSET)
        extra_supplied = extra is not self._UNSET
        if not extra_supplied:
            # **取得先を変えたら持ち越しません。** 同じ欄でも意味が変わる
            # ため (Organization ID / エンドポイント URL)、持ち越すと URL を
            # Organization ID として保存してしまいます。
            extra = provider.extra_field_default if switched else account.organization_id
        else:
            extra = (extra or "").strip()
        if not provider.uses_extra_field:
            extra = ""
        elif extra_supplied:
            # **新しく入れられたときだけ確かめます。** 保存済みの値は過去に
            # 一度この検証を通っているので、名前を変えるだけの編集で
            # 触ってもいない項目の確認を出さないためです (資格情報と同じ)。
            # 書式の判定はプロバイダに任せます (UUID なのか URL なのかは
            # 取得先の事情で、ここが知っていてよいことではありません)。
            warning = provider.validate_extra(extra)
            if warning:
                confirms.append(warning)

        # --- 上限金額 ---
        budget = params.get("budget", self._UNSET)
        if budget is self._UNSET:
            # **取得先を変えたら引き継ぎません。** 通貨が変わるため、
            # JPY の 10000 を USD の上限として引き継ぐと桁が2つ違います。
            budget = provider.default_budget if switched else account.budget
        else:
            try:
                budget = float(budget)
            except (TypeError, ValueError):
                return {"error": t("The spending cap must be a number.")}
            if budget < 0:
                return {"error": t("The spending cap must be a number.")}
        if not provider.supports_budget:
            budget = 0.0

        # --- 有効 / 無効 ---
        enabled = params.get("enabled", self._UNSET)
        enabled = account.enabled if enabled is self._UNSET else bool(enabled)

        if confirms and not params.get("confirmed"):
            # **まだ保存しません。** ここで通してしまうと、デスクトップ版が
            # 既定「いいえ」で尋ねているものを黙って承諾したことになります。
            return {"confirm": confirms}

        kept = account.to_dict()
        account.name = name
        account.provider = provider.id
        account.organization_id = extra
        account.cookie = credential
        account.budget = budget
        account.enabled = enabled

        if not self.save():
            # **保存できなかったものを、変わったことにしません。** 手元だけ
            # 変えると、画面には新しい値が出るのに config.json は元のままで、
            # 次に読み直したときに戻ってきます (delete_account と同じ)。
            for key, value in kept.items():
                setattr(account, key, value)
            return {"error": t(
                "The settings could not be saved. See the log for details.")}

        # 何を変えたかは残しますが、**資格情報そのものは残しません**
        # (Account.__repr__ が伏せているのと同じ理由)。
        logger.info("アカウントを更新しました: %s (取得先=%s, 資格情報の変更=%s)",
                    account.name, account.provider,
                    typed is not self._UNSET)
        return {"saved": True}

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

    def create_account(self, params: dict) -> dict:
        """アカウントを1件作ります。**別ウィンドウは出ません。**

        **検証は update_account に任せます。** 同じ規則を2箇所に書くと、
        片方だけ直したときに「追加では通るのに編集では弾かれる」ような差が
        生まれます。ここは器を1つ用意して、あとはあちらに通すだけです。

        戻り値は update_account と同じ形で、保存できたときだけ accountId を
        添えます。**例外は投げません。**
        """
        provider_id = (params.get("provider") or "").strip()
        if not providers.is_known(provider_id):
            return {"error": t("Unknown provider: {id}", id=provider_id)}
        provider = providers.get(provider_id)

        # **新規では資格情報を必ず求めます。** update_account は「載って
        # いない項目は触らない」ので、資格情報を載せずに呼ぶと、空のまま
        # 作れてしまいます。あちらがそうなっているのは、すでに空のまま
        # 保存されているアカウント (config.json の手編集、復号の失敗) を
        # 直せるようにするためで、**空のものを新しく増やす理由はありません。**
        # 空で作れると、一覧に出るのに取得はできないアカウントができます。
        if (provider.auth_kind != AUTH_OAUTH
                and not (params.get("credential") or "").strip()):
            return {"error": t("Enter {credential}.",
                               credential=t(provider.credential_label))}

        # 取得先ごとの既定値を入れた器を作ります。これを update_account に
        # 渡すことで、**新規でも編集と同じ検証を通します。**
        account = Account(
            name="", organization_id=provider.extra_field_default, cookie="",
            provider=provider.id,
            budget=provider.default_budget if provider.supports_budget else 0.0,
        )
        self.accounts.append(account)

        result = self.update_account(dict(params, accountId=account.id))
        if not result.get("saved"):
            # 検証に落ちた、または尋ね直しになった。**足したものを戻します。**
            # 残すと、名前も資格情報も無いアカウントが一覧に居座ります。
            self.accounts = [a for a in self.accounts if a.id != account.id]
            return result

        logger.info("アカウントを追加しました: %s (取得先=%s)",
                    account.name, account.provider)
        result["accountId"] = account.id
        return result

    @staticmethod
    def provider_payload(provider, retired: bool = False) -> dict:
        """取得先1つを、画面に出せる形へ直します。

        **プロバイダのクラス属性は訳す前の原文 (英語) です。** 訳すのは
        こうして画面へ渡す瞬間で、拡張側にはもう訳す手立てがありません
        (account_payload と同じ約束)。

        retired は「一覧から取り下げた取得先」の印です。**選択肢には
        出しませんが、いま使っているアカウントがあるなら情報は要ります。**
        引けないと、そのアカウントの編集画面がラベルも検証も出せません。
        """
        supports_budget = provider.supports_budget
        return {
            "id": provider.id,
            "label": provider.label,
            "description": t(provider.description) if provider.description else "",
            "retired": retired,
            "implemented": provider.implemented,
            "needsCredential": provider.auth_kind != AUTH_OAUTH,
            "credentialLabel": t(provider.credential_label),
            "credentialHint": (t(provider.credential_hint)
                               if provider.credential_hint else ""),
            "usesExtraField": provider.uses_extra_field,
            "extraLabel": (t(provider.extra_field_label)
                           if provider.uses_extra_field else ""),
            "extraHint": (t(provider.extra_field_hint)
                          if provider.extra_field_hint else ""),
            "supportsBudget": supports_budget,
            "currency": provider.currency if supports_budget else "",
            "currencySymbol": (providers.currency_symbol(provider.currency)
                               if supports_budget else ""),
            "defaultBudget": provider.default_budget if supports_budget else 0.0,
            # 資格情報を、利用者が普段使っているブラウザから取ってくる
            # 手順。**これが唯一の道です。** 画面はこれをそのまま出します。
            "manualUrl": provider.manual_url,
            "manualSteps": t(provider.manual_steps) if provider.manual_steps else "",
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
            # 画面に出す並び。**accounts はここでは並べ替えません。**
            # あちらの順が「追加した順」そのものなので、突き合わせは
            # 画面側に任せます (reorder_accounts の説明を参照)。
            "accountOrder": list(self.config_manager.settings.get("account_order") or []),
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

            # 失効していた。**黙って復帰させる道はもうありません。**
            # 以前はここでアプリ内ブラウザのプロファイルを使い、画面を出さずに
            # Cookie を取り直していました。その仕組みは PySide6 (QtWebEngine)
            # に乗ったものだったので、あれを外した時点で一緒に無くなりました。
            # 利用者には期限切れとして伝わり、編集画面で貼り直してもらいます。
            #
            # **いつ失効したかは app.log に残します。** 画面に出る文言は
            # 出力チャンネルと一緒に消えるので、あとから「何日持ったか」を
            # 調べる手段がこれしかありません。claude.ai のようにサーバ側で
            # sessionKey が置き換わる取得先では、その時刻をブラウザ側の
            # 最終変更時刻 (sessionKeyLC) と突き合わせるのが切り分けの
            # 出発点になります。資格情報そのものは書きません。
            logger.warning("'%s' (%s) の取得が認証エラーになりました: %s",
                           account.name, provider.id, e)
            reply_error(request_id, str(e), True)
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
            "certificate. The value comes from the environment this process "
            "was started with, so set it first and then start the editor "
            "again — setting it while the editor is running does not reach "
            "here, not even after restarting the backend.\n\n{reason}",
            reason=e)
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

    def do_reorder_accounts(self, request_id, params):
        result = self.backend.reorder_accounts(params.get("order") or [])
        if not result.get("ok"):
            reply_error(request_id, result["error"])
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

        最大20秒かかるので、ここで待つとその間ほかの要求が一切通りません。
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

    # ---- アカウントの登録内容を変える操作 ----

    def do_delete_account(self, request_id, params):
        """アカウントを1件消します。

        **別ウィンドウは出ません。** 以前はここも PySide6 製のログイン画面を
        経由していたため、あれが入っていない環境ではアカウントを消すことすら
        できませんでした。
        """
        result = self.backend.delete_account(params.get("accountId") or "")
        if not result.get("ok"):
            reply_error(request_id, result["error"])
            return

        snapshot = self.backend.snapshot()
        snapshot["accountId"] = result["accountId"]
        reply(request_id, snapshot)

    def do_create_account(self, request_id, params):
        """アカウントを1件作ります。**別ウィンドウは出ません。**

        **これが唯一の道です。** 窓を開いて作る道はもうありません。資格情報は
        利用者が普段のブラウザから取ってきて、拡張の画面へ貼ります。
        """
        result = self.backend.create_account(params or {})
        if result.get("error"):
            reply_error(request_id, result["error"])
            return
        if result.get("confirm"):
            reply(request_id, {"confirm": result["confirm"]})
            return

        snapshot = self.backend.snapshot()
        snapshot["accountId"] = result.get("accountId", "")
        reply(request_id, snapshot)

    def do_list_providers(self, request_id, params):
        """選べる取得先の一覧を返します。

        params["include"] に取得先 ID を並べると、一覧から取り下げたもの
        (services/providers の _RETIRED) の情報も返します。**いまそれを
        使っているアカウントを編集するのに要ります。** 返さないと、その
        アカウントのラベルも検証も引けません。選択肢に出すかどうかは
        retired を見て拡張側が決めます。
        """
        listed = list(providers.all_providers())
        known = {p.id for p in listed}
        payload = [self.backend.provider_payload(p) for p in listed]
        for provider_id in (params or {}).get("include") or []:
            provider_id = (provider_id or "").strip()
            if not provider_id or provider_id in known:
                continue
            known.add(provider_id)
            # **知らない ID でも返します。** 設定ファイルを手で書き換えた等で
            # 一覧に無い ID が入っていることがあり、返さないと編集画面が
            # ラベルすら出せません。providers.get() が既定へ倒したものを、
            # **要求された ID のまま**返します。id を書き換えると、拡張側が
            # そのアカウントの取得先として引けなくなります。
            info = self.backend.provider_payload(
                providers.get(provider_id), retired=True)
            info["id"] = provider_id
            payload.append(info)
        reply(request_id, {"providers": payload})

    def do_update_account(self, request_id, params):
        """アカウント1件の登録内容を書き換えます。

        **別ウィンドウは出ません** (do_delete_account と同じ)。
        """
        result = self.backend.update_account(params or {})
        if result.get("error"):
            reply_error(request_id, result["error"])
            return
        if result.get("confirm"):
            # **保存していません。** 尋ねてから confirmed を立てて呼び直す
            # 番です (update_account の説明を参照)。
            reply(request_id, {"confirm": result["confirm"]})
            return

        snapshot = self.backend.snapshot()
        snapshot["accountId"] = params.get("accountId") or ""
        reply(request_id, snapshot)

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
