"""ブラウザ画面が要る操作の実行係。**もう誰も起動しません。**

**1.7.0 で役目を終えました。** 拡張はアプリ内ブラウザでのログインをやめ、
資格情報は利用者が普段のブラウザから取ってきて、拡張の画面のフォームへ
貼るようになりました。cli.py にこのファイルを起こすコードはもうありません
(subprocess も Popen も残っていません)。vsix にも入りません
(vscode-extension/.vscodeignore を参照)。

**このファイルは、いまの置き場所からは動きません。** 起動時の sys.path 解決
(下の _root の決め方) は、隣に services/ があればそこを根と見なします。
ビルドを1回でも走らせると backend/services/ ができるので根は backend/ に
決まりますが、**ui/ はもうコピーされない**ため
(build_vsix.py の _COPY_FILES)、ui.account_dialog の import で止まります。
動かすならリポジトリ直下から起動してください。

以下は、当時どう動いていたかの記録です。

**なぜ別プロセスなのか。** Qt は自分のイベントループをメインスレッドで
回す必要があります。cli.py のメインスレッドは標準入力を読み続けているので、
そこに Qt を同居させられません。加えて、QtWebEngine (Chromium) が落ちても
バックエンドまで巻き込まれずに済みます。

    python gui_helper.py add        --result <path>
    python gui_helper.py relogin    --result <path> --id <account_id>

**ここに来るのは、ログイン画面が要る操作だけでした。** 削除と編集は
バックエンド (cli.py) の中で完結します。窓の要らない操作をここへ回すと、
PySide6 が入っていない環境でそれまでできなくなります — 削除も編集も追加も
実際にそうなっていて、それを1つずつ剥がした先が、この経路そのものを畳む
判断でした。

**結果は --result で渡されたファイルへ書きます。標準出力は使いません。**
QtWebEngine (Chromium) は Python の sys.stdout を経由せず OS のファイル
記述子へ直接書くことがあり、標準出力に混ざると結果を読めなくなるためです。

**設定ファイルへの書き込みはこのプロセスが行います。** cli.py へ結果を
返して向こうに書かせると、Cookie がプロセス間のパイプを流れることになります。
書き終わったら cli.py が config.json を読み直します。
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# services / models / ui の探し先 (cli.py と同じ方針)。
#   vsix に固めたあと … build_vsix.py がこのファイルの隣へコピーする
#   リポジトリで直接動かすとき … プロジェクト直下にある
for _root in (_HERE, os.path.abspath(os.path.join(_HERE, "..", ".."))):
    if os.path.isdir(os.path.join(_root, "services")):
        sys.path.insert(0, _root)
        break

# 呼び出し元は結果ファイルだけを見ます。ここへ出るものは診断用のログです。
sys.stdout = sys.stderr

MIN_PYTHON = (3, 9)

# 翻訳。**PySide6 の点検より前に入れます** (cli.py と同じ理由: ここで出す
# 文言が最初に読まれるものなので、読めない言語で出しては直し方に辿り着け
# ません)。services/i18n.py が import するのは標準ライブラリだけなので、
# 「PySide6 を import する前に確かめる」という下の約束は破りません。
from services.i18n import t  # noqa: E402


def preflight() -> str:
    """動かせない理由があれば、その説明を返します (無ければ空文字)。

    **PySide6 を import する前に確かめます。** import してから
    ModuleNotFoundError を拾うと、呼び出し側には終了コードしか見えず、
    利用者は何を入れれば直るのか分からないままになります。
    """
    if sys.version_info < MIN_PYTHON:
        return t(
            "Python {required} or later is required "
            "(found: {found} / {executable}).",
            required=f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            found=sys.version.split()[0], executable=sys.executable,
        )

    import importlib.util

    # QtWebEngineWidgets は PySide6-Addons が提供します。PySide6 だけを
    # 入れた環境ではログイン画面が作れないので、個別に確かめます。
    required = [
        ("PySide6.QtWidgets", "PySide6"),
        ("PySide6.QtWebEngineWidgets", "PySide6-Addons"),
    ]
    missing = []
    for module, package in required:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except (ImportError, ValueError):
            missing.append(package)

    if missing:
        packages = " ".join(sorted(set(missing)))
        return t(
            "PySide6 is required to show the sign-in window.\n"
            "Not found: {packages}\n"
            "Python in use: {executable}\n\n"
            "Install it with:\n{command}\n\n"
            "PySide6 is not needed just to view and refresh usage. "
            # **この設定はもうありません** (1.7.0 で削除)。文言も
            # 翻訳カタログから外れているので、英語のまま出ます。
            # このファイル自体が到達不能なので、直す先もありません。
            "PySide6 is not installed in this Python.",
            packages=packages, executable=sys.executable,
            command=f'"{sys.executable}" -m pip install {packages}',
        )
    return ""


def write_result(path: str, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def log_step(message: str):
    """進み具合を stderr へ1行出します。呼び出し元は [gui:err] として拾います。

    **logging ではなく sys.stderr へ直接書き、毎回 flush します。**
    理由は2つあります。1つは、この関数を setup_logging() より前
    (起動直後の印) からも呼ぶため。もう1つは、子プロセスの stderr が
    パイプにつながっていると Python がブロック単位でバッファリングし、
    途中で固まったときに一行も呼び出し元へ届かないためです。
    「どこまで進んで固まったのか」を見せるのがこの出力の目的なので、
    溜めてから出すのでは意味がありません。

    **標準出力は使いません** (このファイル冒頭の説明のとおり、
    結果は --result のファイルだけで受け渡します)。
    """
    try:
        sys.stderr.write(f"[gui_helper] {message}\n")
        sys.stderr.flush()
    except Exception:
        # 呼び出し元が stderr を閉じている場合など。
        # 進捗ログのために操作そのものを失敗させない。
        pass


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["add", "relogin", "silent_refresh"])
    parser.add_argument("--result", required=True, help="結果を書き出す JSON のパス")
    parser.add_argument("--id", default="", help="対象アカウントの ID")
    return parser.parse_args()


_ARGS = parse_args()
_PROBLEM = preflight()
if _PROBLEM:
    write_result(_ARGS.result, {"ok": False, "error": _PROBLEM, "kind": "dependency"})
    sys.exit(2)


# **QtWebEngine を使うモジュールは QApplication を作る前に import します。**
# デスクトップ版 (main.py) も同じ順序で、ここだけ変えると
# 「exe では出るのに拡張からは出ない」といった差が生まれます。
import ctypes                                              # noqa: E402
import logging                                             # noqa: E402

from PySide6.QtCore import Qt, QTimer                        # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402
from PySide6.QtGui import QIcon                             # noqa: E402

from services import browser_profile, proxy_manager         # noqa: E402
from services.config_manager import ConfigManager, default_config_dir  # noqa: E402
from services.providers import AUTH_COOKIE                  # noqa: E402
from services.resources import app_icon_path                # noqa: E402
from ui import session_refresher                            # noqa: E402
from ui.account_dialog import AccountDialog, LoginDialog    # noqa: E402

logger = logging.getLogger("gui_helper")

# デスクトップ版と同じ値。タスクバーで python.exe に混ざらないようにする。
APP_USER_MODEL_ID = "AntigravityDev.AIUsageManager"


def setup_logging():
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _force_foreground_win32(hwnd: int) -> bool:
    """Windows で窓を一度だけ最前面へ持ち上げ、入力フォーカスを奪います。

    戻り値は「実際に前面を取れたか」。取れなくても例外は投げません
    (見た目の問題のために操作そのものを失敗させない。この方針は
    SetCurrentProcessExplicitAppUserModelID の扱いと同じです)。

    **なぜ Qt の raise_() / activateWindow() では足りないのか。**
    Windows の SetForegroundWindow は「今フォアグラウンドを持っている
    プロセス」か「直前に入力を受け取ったプロセス」からしか成功しません。
    VSCode のボタンから起動したこの子プロセスはそのどちらでもないため、
    Qt が内部で呼ぶ SetForegroundWindow は OS に黙って無視され、
    代わりにタスクバーが点滅するだけになります。

    **なぜ WindowStaysOnTopHint を付けっぱなしにしないのか。**
    常時最前面にすると、利用者が Cookie を取りに普段のブラウザへ
    切り替えたときにこの窓が居座って作業できなくなります。ここでは
    HWND_TOPMOST → HWND_NOTOPMOST と一往復させ、「この瞬間だけ全部の窓の
    上へ出す」に留めます。Qt の setWindowFlag(WindowStaysOnTopHint) で
    同じことをすると、フラグを変えるたびに widget が hide されて
    show し直しになる (Qt の仕様) ため、ちらつく上に exec() 直前の
    状態が崩れます。OS の API を直接叩けばフラグの付け外しは起きません。

    **なぜ AllowSetForegroundWindow を使わないのか。** あれは「前面を
    持っている側が、他のプロセスへ前面化を許可する」API です。許可を
    出せるのは VSCode 側であって、前面を欲しがっているこちらから
    呼んでも効きません。cli.py 側には手を入れられないので、この
    プロセスだけで完結する AttachThreadInput を使います。

    **なぜ SPI_SETFOREGROUNDLOCKTIMEOUT を 0 にしないのか。** よく出てくる
    手ですが、システム全体の設定を書き換えるため、途中で落ちると
    元に戻せないまま利用者の環境に残ります。AttachThreadInput なら
    この関数を抜けた時点で影響が消えます。
    """
    # 非 Windows では ctypes.wintypes は import 自体が失敗するので遅延させる
    from ctypes import wintypes

    # **ctypes.windll.user32 ではなく自前の WinDLL を作ります。** windll の
    # 方はプロセス内で共有されるキャッシュなので、そこへ argtypes を
    # 書き込むと同じ DLL を使う他のコードの呼び出し方まで変えてしまいます。
    #
    # argtypes を省略できないのは、64bit で HWND を int のまま渡すと
    # ctypes が 32bit の c_int とみなしてハンドルを切り詰めてしまうためです
    # (症状は「何も起きない」なので、原因に気付きにくい)。
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND

    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    SW_RESTORE = 9

    # 最小化された状態で開いた場合、以降の前面化は全部成功するのに
    # 画面には何も出ない (アイコンのまま前面になる) ので先に復元する。
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    move_flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, move_flags)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, move_flags)

    # ここまでで「見える」ようにはなりますが、キーボード入力は前面を
    # 持っているプロセス (VSCode) に行ったままです。入力キューを一時的に
    # 相手のスレッドへ繋ぐと、OS からは「同じ入力の流れの中にいる」
    # ように見えるため SetForegroundWindow が通ります。
    foreground = user32.GetForegroundWindow()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    attached = False
    if foreground_thread and foreground_thread != target_thread:
        attached = bool(user32.AttachThreadInput(foreground_thread, target_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        # 繋いだままにすると、相手 (VSCode) の入力処理までこちらの
        # スレッドの生存に巻き込まれるので、成否によらず必ず外す。
        if attached:
            user32.AttachThreadInput(foreground_thread, target_thread, False)

    return user32.GetForegroundWindow() == hwnd


def _bring_to_front(dialog, report: bool = False):
    """前面化を1回試みます。失敗しても例外は投げません。"""
    try:
        # 最小化された状態から開かれたときのために通常状態へ戻す。
        # WindowActive も一緒に立てるのは、Qt 側の状態と OS 側の状態を
        # 食い違わせないため (下の activateWindow と対になる)。
        dialog.setWindowState(
            (dialog.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
        )
        dialog.raise_()
        dialog.activateWindow()

        if sys.platform != "win32":
            return

        # winId() はネイティブウィンドウを (無ければ) 作ってから返すので、
        # ここでハンドルが無いということは無い。
        if _force_foreground_win32(int(dialog.winId())):
            if report:
                log_step("ダイアログを前面に出しました。")
        elif report:
            # ここに来ても操作は続けられる。利用者が窓を探せるように
            # 「どこを見ればよいか」まで書く。
            log_step("ダイアログを前面に出せませんでした。"
                     "タスクバーで点滅している項目をクリックしてください。")
    except Exception as e:
        # 前面に出せないのは見た目の問題であって、操作の失敗ではない。
        # ここで例外を上げると「アカウントを追加できない」に化けてしまう。
        logger.warning("ダイアログの前面化に失敗しました: %s", e)


def show_in_front(dialog):
    """ダイアログを最前面に出します。

    VSCode から起動した子プロセスの窓は、Windows では前面に出ずに
    タスクバーで点滅するだけのことがあります。押した本人は VSCode を
    見ているので、そのままでは「押しても何も起きない」ように見えます。

    前面化の具体的な手当ては _force_foreground_win32 のコメントを参照。
    """
    dialog.show()
    _bring_to_front(dialog)
    log_step(f"ダイアログを表示しました: {dialog.windowTitle()}")

    # **exec() の後にもう一度試します。** QDialog.exec() は自分でも
    # show() を呼び、モーダル属性を付け直します。その過程で前後関係や
    # アクティブ状態が入れ替わることがあり、exec() の前にどれだけ
    # 前面化しても取り消されてしまいます。0ms のタイマーは exec() が
    # イベントループを回し始めた直後 (＝窓が完全に出そろった後) に
    # 一度だけ発火するので、そこで仕上げます。
    #
    # sleep して待つ手は採れません。exec() を呼ぶ前のこのスレッドは
    # イベントループを回していないため、待っている間ずっと窓が
    # 描画されないままになります。
    QTimer.singleShot(0, lambda: _bring_to_front(dialog, report=True))


# ---------------- 各操作 ----------------


def do_add(args, manager: ConfigManager, accounts: list) -> dict:
    dialog = AccountDialog(None, None)
    show_in_front(dialog)
    if dialog.exec() != QDialog.Accepted:
        return {"ok": True, "cancelled": True}

    account = dialog.get_account_data()
    accounts.append(account)
    if not manager.save(accounts, manager.settings):
        return {"ok": False,
                "error": t("The settings could not be saved. Check the log.")}
    return {"ok": True, "accountId": account.id, "changed": True}


def do_relogin(args, manager: ConfigManager, accounts: list) -> dict:
    """デスクトップ版の open_login_dialog と同じ手順で Cookie を取り直します。

    手順を変えると、片方で再ログインしたのにもう片方では失効したまま、
    という食い違いが起きます。
    """
    account = next((a for a in accounts if a.id == args.id), None)
    if account is None:
        return {"ok": False, "error": t("The account was not found.")}

    provider = account.get_provider()
    if provider.auth_kind != AUTH_COOKIE:
        return {"ok": False,
                "error": t('{provider} does not support signing in through a '
                           'browser. Set {credential} from "Edit Account".',
                           provider=provider.label,
                           credential=t(provider.credential_label))}

    dialog = LoginDialog(account.id, provider, None)
    show_in_front(dialog)
    if dialog.exec() != QDialog.Accepted or not dialog.cookie_header:
        return {"ok": True, "cancelled": True}

    # 手動で貼り付けた場合だけ、セッション維持に要る Cookie 一式を
    # プロファイルへ預ける。アプリ内でログインした場合は、ブラウザが
    # 既に同じプロファイルへ書き込んでいるので何もしなくてよい。
    for_profile = provider.cookies_for_profile(dialog.pasted or "")
    if for_profile:
        try:
            browser_profile.inject_cookies(account.id, for_profile, provider.cookie_domain)
        except Exception as e:
            # 預けられなくても、取得そのものは新しい Cookie で行える
            logger.warning("ブラウザプロファイルへ Cookie を預けられませんでした: %s", e)

    account.cookie = dialog.cookie_header
    if not manager.save(accounts, manager.settings):
        return {"ok": False,
                "error": t("The settings could not be saved. Check the log.")}
    return {"ok": True, "accountId": account.id, "changed": True}


def do_silent_refresh(args, manager: ConfigManager, accounts: list) -> dict:
    """ログイン画面を出さずに Cookie を取り直せないか、1回だけ試します。

    プロファイルにログイン状態が残っていれば、対象サイトを一度読み込むだけで
    新しいセッション Cookie が発行されます。残っていなければログイン画面へ
    飛ばされるので、そのときだけ利用者に手で入り直してもらいます。

    **窓は出しません。** 出さないことがこのモードの目的です。取れなかった
    ときも黙って戻り、ログイン画面を出すかどうかは呼び出し元 (cli.py) が
    決めます。

    **ここだけ app.exec() を回します。** ほかのモードは dialog.exec() が
    自前で入れ子の待ち合わせを作りますが、こちらは表に出す窓が無いので、
    読み込みが終わるまで回してくれるものが他にありません。
    """
    account = next((a for a in accounts if a.id == args.id), None)
    if account is None:
        return {"ok": False, "error": t("The account was not found.")}

    provider = account.get_provider()
    if provider.auth_kind != AUTH_COOKIE or not provider.home_url:
        # Cookie で入る取得先でなければ、やり直せるものが無い。
        return {"ok": True, "status": session_refresher.LOGIN_REQUIRED,
                "changed": False}

    outcome = {}

    def on_finished(account_id: str, cookie_header: str, status: str):
        outcome["cookie"] = cookie_header
        outcome["status"] = status
        QApplication.instance().quit()

    refresher = session_refresher.SessionRefresher(account.id, provider)
    refresher.finished.connect(on_finished)
    # exec() が回り始めてから読み込みを始める。先に始めると、終わるのが
    # 早かったときに quit() が exec() より前に来て、誰も止めない待ちが残る。
    QTimer.singleShot(0, refresher.start)
    QApplication.instance().exec()

    status = outcome.get("status", session_refresher.FAILED)
    cookie = outcome.get("cookie", "")
    log_step(f"画面なしでの復帰: status={status}")

    if status != session_refresher.REFRESHED or not cookie:
        # **失敗ではありません。** 「自動では戻せなかった」というだけで、
        # 呼び出し元はこのあと利用者にログインを促します。
        return {"ok": True, "status": status, "changed": False}

    account.cookie = cookie
    if not manager.save(accounts, manager.settings):
        return {"ok": False,
                "error": t("The settings could not be saved. Check the log.")}
    return {"ok": True, "status": status, "accountId": account.id,
            "changed": True}


HANDLERS = {"add": do_add, "relogin": do_relogin,
            "silent_refresh": do_silent_refresh}


def main():
    # **起動直後の印。まだ Qt には一切触れていない時点で必ず1行出します。**
    # PySide6 の import (このファイルの中ほど) で固まる事例が実測であるため、
    # この行が出ていなければ「Qt に届く前に止まった」、出ていれば
    # 「import は通って main まで来ている」と切り分けられます。
    # どの Python で動いているかを一緒に出すのは、起動に使った Python に
    # PySide6 が無い、という食い違いが一番多いためです。
    log_step(f"起動しました: pid={os.getpid()} mode={_ARGS.mode} python={sys.executable}")

    setup_logging()

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass  # アイコンの見た目だけの問題なので続行する

    app = QApplication(sys.argv)
    app.setApplicationName("AI-UsageManager")
    app.setApplicationDisplayName("AI-UsageManager")
    try:
        app.setWindowIcon(QIcon(app_icon_path()))
    except Exception:
        pass
    log_step("Qt の初期化が完了しました。")

    manager = ConfigManager()
    manager.migrate_legacy_config_if_needed()
    try:
        accounts, _ = manager.load()
    except Exception as e:
        # 読めない設定を空リストで上書きすると、既存の Cookie を全部失う
        write_result(_ARGS.result, {
            "ok": False,
            "error": t("The settings file could not be read.\n{reason}\n\n"
                       "File: {path}", reason=e, path=manager.config_path),
        })
        return 1

    log_step(f"設定を読み込みました: アカウント {len(accounts)} 件 ({manager.config_path})")

    proxy_manager.configure(manager.settings)

    try:
        result = HANDLERS[_ARGS.mode](_ARGS, manager, accounts)
    except Exception as e:
        logger.exception("操作中に想定外のエラーが発生しました")
        QMessageBox.critical(
            None, t("Error"),
            t("The operation could not be completed.\n\n{reason}\n\n"
              "The details are in this log:\n{path}",
              reason=e, path=os.path.join(default_config_dir(), 'app.log')),
        )
        result = {"ok": False, "error": t("Unexpected error: {reason}", reason=e)}

    write_result(_ARGS.result, result)
    log_step(f"操作を終えました: ok={result.get('ok')} "
             f"cancelled={result.get('cancelled', False)}")
    # プロファイルを掴んだままだと、次に開くときに Cookie ファイルを
    # ロックしたままになることがある
    browser_profile.release_all()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
