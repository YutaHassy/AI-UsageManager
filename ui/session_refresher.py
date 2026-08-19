"""アカウント専用プロファイルを使って、Cookie の再取得を画面なしで試みる。

Cookie が失効したとき、いきなりログイン画面を出す前にこれを試します。
プロファイルにログイン状態が残っていれば、対象サイトを1回読み込むだけで
新しいセッション Cookie が発行され、ユーザー操作なしで復帰できます。
残っていなければログイン画面へリダイレクトされるので、その場合だけ
ログイン画面を出す、という切り分けに使います。

**先回りの延命 (gemini.py の rotate_cookies) とは役割が違います。** あちらは
まだ生きている Cookie の期限を伸ばすもので、いったん切れてしまった後には
効きません。切れた後に手が無いと、利用者はそのつど手でログインし直す
ことになります。ここはその「切れた後」を受け持ちます。

開く URL・回収する Cookie のドメイン・成功と見なす Cookie 名は、
すべてプロバイダから受け取ります (ここにサイト固有の値を書かないこと)。

**QApplication のあるプロセスからしか使えません。** QWebEngineView を
作るためです。拡張のバックエンド (cli.py) には QApplication が無いので、
呼ぶのは gui_helper.py の silent_refresh モードだけです。
"""

import logging

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from services import browser_profile, proxy_manager
from services.providers import UsageProvider

logger = logging.getLogger(__name__)

# 状態
REFRESHED = "refreshed"              # 新しい Cookie を取得できた
LOGIN_REQUIRED = "login_required"    # 手動ログインが必要
FAILED = "failed"                    # 読み込み自体に失敗した


class SessionRefresher(QObject):
    """1アカウント分の再認証試行。使い捨てで、完了したら finished を出します。"""

    finished = Signal(str, str, str)  # account_id, cookie_header, status

    def __init__(self, account_id: str, provider: UsageProvider,
                 timeout_ms: int = 25000, parent=None):
        super().__init__(parent)
        self.account_id = account_id
        self.provider = provider
        self._cookies = {}
        self._session_key = None
        self._done = False

        self.profile = browser_profile.get_profile(account_id)
        self.cookie_store = self.profile.cookieStore()
        self.cookie_store.cookieAdded.connect(self._on_cookie_added)

        # 画面には出さない。表示しない QWebEngineView でも読み込みは行われる。
        self.view = QWebEngineView()
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.page.certificateError.connect(self._on_certificate_error)
        # プロキシ認証に応答しないと、社内プロキシ環境では必ず読み込みに失敗する
        proxy_manager.install_proxy_authentication(self.page, self._on_proxy_credentials_missing)
        self.page.loadFinished.connect(self._on_load_finished)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._complete(FAILED))
        self._timer.start(timeout_ms)

    def start(self):
        logger.info(
            "セッションの自動更新を試みます (account=%s, provider=%s)",
            self.account_id, self.provider.id,
        )
        self.view.setUrl(QUrl(self.provider.home_url))

    # ---------------- 内部処理 ----------------

    def _on_proxy_credentials_missing(self, proxy_host: str):
        # 認証情報が無い状態で待っても無駄なので、すぐ手動ログインへ倒す
        logger.error("プロキシ %s の認証情報が未設定のため、セッション更新を中止します。", proxy_host)
        self._complete(LOGIN_REQUIRED)

    def _on_certificate_error(self, error):
        # 自動処理の途中で証明書エラーを黙って受け入れることは絶対にしない
        logger.error("セッション更新中に証明書エラー: %s", error.description())
        error.rejectCertificate()

    def _on_cookie_added(self, cookie):
        # 対象ホストの Cookie だけを拾う
        if not self.provider.owns_cookie_domain(cookie.domain()):
            return
        try:
            name = cookie.name().data().decode("utf-8")
            value = cookie.value().data().decode("utf-8")
        except UnicodeDecodeError:
            return
        self._cookies[name] = value
        if name == self.provider.session_cookie_name:
            self._session_key = value

    def _on_load_finished(self, ok: bool):
        if self._done:
            return
        if not ok:
            self._complete(FAILED)
            return

        # 保存済み Cookie も拾えるだけ拾う
        self.cookie_store.loadAllCookies()

        # Cookie の書き込みが落ち着いてから判定する
        QTimer.singleShot(1200, self._evaluate)

    def _evaluate(self):
        if self._done:
            return

        final_url = self.page.url().toString()
        logger.debug("セッション更新後のURL: %s", final_url)

        if self.provider.is_login_page(final_url):
            self._complete(LOGIN_REQUIRED)
            return

        if self._session_key:
            self._complete(REFRESHED)
        else:
            # ログイン画面ではないが新しいセッション Cookie を取れなかった。
            # 手元の Cookie では API が通らなかったので、手動ログインに委ねる。
            self._complete(LOGIN_REQUIRED)

    def _complete(self, status: str):
        if self._done:
            return
        self._done = True
        self._timer.stop()

        try:
            self.cookie_store.cookieAdded.disconnect(self._on_cookie_added)
        except (RuntimeError, TypeError):
            pass

        cookie_header = ""
        if status == REFRESHED:
            cookie_header = "; ".join(f"{k}={v}" for k, v in self._cookies.items())

        logger.info("セッションの自動更新結果: %s (account=%s)", status, self.account_id)
        self.finished.emit(self.account_id, cookie_header, status)

        # ページを先に破棄しないとプロファイルが解放されない
        self.page.setParent(None)
        self.page.deleteLater()
        self.view.deleteLater()

    def abort(self):
        """終了時などに中断します。"""
        self._done = True
        self._timer.stop()
        try:
            self.page.stop()
        except RuntimeError:
            pass
