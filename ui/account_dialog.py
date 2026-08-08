import logging
import uuid

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget
)

from models.account import Account
from services import browser_profile, providers, proxy_manager
from services.i18n import t
from services.providers import (
    AUTH_COOKIE, AUTH_OAUTH, ENV_INSECURE_SSL, UsageProvider, insecure_ssl_enabled
)

logger = logging.getLogger(__name__)


class ManualCredentialDialog(QDialog):
    """アプリ内ブラウザでログインできないときに、資格情報を手で設定する画面。

    認証側が埋め込みブラウザからのサインインを拒否することがあります
    (Google の「このブラウザまたはアプリは安全でない可能性があります」)。
    これは**埋め込み側アプリがパスワード入力を盗み見できる**という理由で
    存在する保護です。ブラウザを偽装して通そうとはせず、普段お使いの
    ブラウザで取ってきた値を貼ってもらう経路を用意します。

    開く URL も手順もプロバイダから受け取ります。
    このクラスにサービス固有の値を書かないでください。
    """

    def __init__(self, provider: UsageProvider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.credential = ""
        # 絞り込む前に貼られた内容。設定ファイルには保存しませんが、
        # セッション維持に必要な Cookie がここにしか無いため、
        # ブラウザプロファイルへ預けるのに使います (browser_profile.inject_cookies)。
        self.pasted = ""
        self.setWindowTitle(
            t("{provider} — set it another way", provider=provider.label))
        self.setMinimumWidth(560)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(12)

        heading = QLabel(t("If you cannot sign in from the built-in browser"))
        heading.setStyleSheet("font-weight: bold; font-size: 14px; color: #f9e2af;")
        layout.addWidget(heading)

        reason = QLabel(t(
            "Some sign-in providers, Google accounts among them, refuse to sign "
            "you in from an embedded browser.\n"
            "That is a protection against the surrounding app seeing your "
            "password, so take the value from your usual browser instead."
        ))
        reason.setWordWrap(True)
        reason.setStyleSheet("color: #a6adc8; font-size: 11px;")
        layout.addWidget(reason)

        steps = QLabel(t(self.provider.manual_steps))
        steps.setWordWrap(True)
        steps.setTextInteractionFlags(Qt.TextSelectableByMouse)
        steps.setAlignment(Qt.AlignTop)
        steps.setStyleSheet("color: #cdd6f4; font-size: 12px; padding: 4px;")

        # 手順は取得先によって長さがまちまち (Gemini は開発者ツールの操作を
        # 案内するため長い)。そのまま並べるとノート PC の縦解像度で
        # 「設定」ボタンが画面外に出て押せなくなるため、ここだけ畳む。
        steps_area = QScrollArea()
        steps_area.setWidget(steps)
        steps_area.setWidgetResizable(True)
        steps_area.setMaximumHeight(300)
        steps_area.setStyleSheet(
            "QScrollArea { background-color: #313244; border: none; border-radius: 6px; }"
            "QScrollArea > QWidget > QWidget { background-color: #313244; }"
        )
        layout.addWidget(steps_area)

        self.open_btn = QPushButton(t("🌐 Open in the default browser"))
        self.open_btn.setStyleSheet(
            "QPushButton { background-color: #89b4fa; color: #11111b; font-weight: bold;"
            " border-radius: 6px; padding: 8px; }"
            "QPushButton:hover { background-color: #b4befe; }"
        )
        self.open_btn.clicked.connect(self.open_in_browser)
        layout.addWidget(self.open_btn)

        # ブラウザが開かない環境 (リモートセッション等) でも詰まないように
        # URL そのものを選択可能な形で出しておく。
        url_label = QLabel(self.provider.manual_url)
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_label.setWordWrap(True)
        url_label.setStyleSheet("color: #7f849c; font-size: 10px;")
        layout.addWidget(url_label)

        self.value_input = QTextEdit()
        self.value_input.setAcceptRichText(False)
        self.value_input.setPlaceholderText(t("Paste what you copied here, as-is"))
        self.value_input.setMaximumHeight(110)
        layout.addWidget(self.value_input)

        privacy = QLabel(t(
            "* Only the part of what you paste that is needed to fetch usage "
            "is saved (email addresses and the like are not).\n"
            "  What is saved is encrypted with Windows DPAPI."
        ))
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #a6adc8; font-size: 10px;")
        layout.addWidget(privacy)

        warning = QLabel(t(
            "⚠ What you paste here is as good as a password. "
            "Do not show it to anyone or paste it into chat or email."
        ))
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #f9e2af; font-size: 10px;")
        layout.addWidget(warning)

        btn_layout = QHBoxLayout()
        cancel_btn = QPushButton(t("Cancel"))
        cancel_btn.clicked.connect(self.reject)
        self.set_btn = QPushButton(t("Set"))
        self.set_btn.setObjectName("primaryButton")
        self.set_btn.setDefault(True)
        self.set_btn.clicked.connect(self.accept)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.set_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def open_in_browser(self):
        if QDesktopServices.openUrl(QUrl(self.provider.manual_url)):
            return
        # 既定ブラウザが解決できないことがある。URL は画面に出してあるので、
        # 手でコピーしてもらえるようにそれを案内する。
        QMessageBox.warning(
            self, t("Could not open a browser"),
            t("The default browser could not be opened.\n"
              "Copy the URL shown on this screen and open it yourself."),
        )

    def accept(self):
        pasted = self.value_input.toPlainText().strip()
        if not pasted:
            QMessageBox.warning(
                self, t("Input error"),
                t("Paste {credential}.",
                  credential=t(self.provider.credential_label)),
            )
            self.value_input.setFocus()
            return

        # 画面に出たものを丸ごと貼ってもらい、必要な部分だけを取り出す。
        # 保存するのはここで絞った結果だけ (貼り付け元には認証に不要な
        # メールアドレスや別の資格情報が混ざっている)。
        value = self.provider.normalize_credential(pasted)

        # 形が違うものは知らせるが、通すかどうかは利用者に委ねる。
        # 有効性を最終的に決めるのは取得先のサーバーであってこのアプリではない。
        warning = self.provider.validate_credential(value)
        if warning:
            reply = QMessageBox.question(
                self, t("Confirm"),
                t("{warning}\n\nSet it anyway?", warning=warning),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self.value_input.setFocus()
                return

        self.credential = value
        self.pasted = pasted
        super().accept()


class LoginDialog(QDialog):
    """アカウント専用のブラウザプロファイルで対象サービスにログインする画面。

    プロファイルをアカウントごとに分けているため、
      - 普段使いのブラウザでログアウトしてもアプリ側のセッションは切れない
      - 複数アカウントを同時にログイン状態で保持できる
      - 前回のログイン状態が残っていれば、開いた直後に自動で完了する
    という挙動になります。

    開く URL や成功判定はプロバイダから受け取るので、
    このクラスにサービス固有の値を書かないでください。
    """

    def __init__(self, account_id: str, provider: UsageProvider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.setWindowTitle(
            t("{provider} sign-in (cookies collected automatically)",
              provider=provider.label))
        # ログイン画面は普通のブラウザに近い横長比率で開きます。
        # 以前は 760x900 の縦長で、ログインフォームが左右に余りつつ
        # 縦に間延びして見えていました。ログインページは応答型なので、
        # 幅が狭いとモバイル寄りの窮屈なレイアウトに切り替わります。
        self.setMinimumSize(640, 560)
        self._resize_to_screen(1080, 800)
        self.account_id = account_id
        self.cookies = {}
        self.session_key = None
        self.cookie_header = None
        # 手貼りのとき、絞り込む前に貼られた内容 (セッション維持用)。
        # アプリ内ブラウザでログインできた場合は、Cookie がすでに
        # プロファイルに入っているので不要 (None のまま)。
        self.pasted = None
        self.used_manual_fallback = False
        self._proxy_auth_failed = False

        self.init_ui()

    def _resize_to_screen(self, preferred_width: int, preferred_height: int):
        """画面からはみ出さない範囲で、比率を保ったまま開きます。

        ノート PC の縦解像度では、固定サイズだとタスクバーの下に隠れて
        ボタンが押せなくなるため、作業領域に合わせて縮めます。

        **縦横を別々に切り詰めないこと。** 幅と高さを独立に min() すると、
        縦の狭い画面では高さだけが縮んで横長に、横の狭い画面では
        幅だけが縮んで縦長になり、画面ごとに歪んだ形で開きます。
        同じ比率で縮めれば、どの画面でも同じ見た目になります。
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(preferred_width, preferred_height)
            return

        available = screen.availableGeometry()
        shrink = min(
            1.0,
            available.width() * 0.9 / preferred_width,
            available.height() * 0.9 / preferred_height,
        )
        minimum = self.minimumSize()
        self.resize(
            max(int(preferred_width * shrink), minimum.width()),
            max(int(preferred_height * shrink), minimum.height()),
        )

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        guidance = t("  * Sign in to {provider}. This window closes by itself "
                     "once you are signed in.", provider=self.provider.label)
        if self.provider.has_manual_fallback:
            guidance += t('\n    If you are refused, you can set it up from '
                          '"Cannot sign in?" below.')
        self.info_bar = QLabel(guidance)
        self.info_bar.setStyleSheet(
            "background-color: #313244; color: #a6e3a1; padding: 8px; font-weight: bold; font-size: 13px;"
        )
        self.info_bar.setWordWrap(True)
        layout.addWidget(self.info_bar)

        # アカウント専用の永続プロファイルを使う。
        # 以前は共有プロファイル + deleteAllCookies() だったため、
        # 毎回まっさらな状態からログインし直す必要があった。
        self.profile = browser_profile.get_profile(self.account_id)

        self.web_view = QWebEngineView()
        self.page = QWebEnginePage(self.profile, self.web_view)
        self.web_view.setPage(self.page)

        self.page.certificateError.connect(self.handle_certificate_error)

        # 社内プロキシが 407 を返したときに認証情報を送る。
        # これを繋がないと、プロキシ環境ではページが真っ白のまま開けない。
        proxy_manager.install_proxy_authentication(self.page, self.on_proxy_credentials_missing)

        # 読み込み失敗を黙って握りつぶさない
        self.page.loadFinished.connect(self.on_load_finished)

        # Cookie ストアの監視
        self.cookie_store = self.profile.cookieStore()
        self.cookie_store.cookieAdded.connect(self.on_cookie_added)

        self.web_view.setUrl(QUrl(self.provider.login_url))
        self.web_view.urlChanged.connect(self.on_url_changed)

        layout.addWidget(self.web_view)

        # 救済導線。ここで詰まった利用者はダイアログを閉じるしか手が無く、
        # 「Cookie 欄に何を貼ればよいか」を知る術がないため、画面内から辿れるようにする。
        if self.provider.has_manual_fallback:
            layout.addWidget(self._build_manual_bar())

        self.setLayout(layout)

    def _build_manual_bar(self) -> QWidget:
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 6, 8, 6)

        note = QLabel(t("Trouble signing in?"))
        note.setStyleSheet("color: #a6adc8; font-size: 11px;")
        bar_layout.addWidget(note)
        bar_layout.addStretch()

        self.manual_btn = QPushButton(t("Cannot sign in?"))
        self.manual_btn.setStyleSheet(
            "QPushButton { background-color: #45475a; color: #f9e2af;"
            " border-radius: 6px; padding: 6px 12px; }"
            "QPushButton:hover { background-color: #585b70; }"
        )
        self.manual_btn.clicked.connect(self.open_manual_fallback)
        bar_layout.addWidget(self.manual_btn)
        return bar

    def open_manual_fallback(self):
        dialog = ManualCredentialDialog(self.provider, self)
        if dialog.exec() != QDialog.Accepted:
            return
        # 手で貼られた値をそのまま資格情報として扱う。
        # Cookie 経由かトークン直貼りかの解釈はプロバイダの仕事。
        self.cookie_header = dialog.credential
        self.pasted = dialog.pasted
        self.used_manual_fallback = True
        self.accept()

    def handle_certificate_error(self, error):
        """証明書エラーの扱い。

        この画面ではユーザーが対象サービスの実際のパスワードを入力します。
        以前は無条件に acceptCertificate() していたため、中間者攻撃で
        偽の証明書を提示されてもそのまま接続し、パスワードごと窃取される
        経路になっていました。既定では拒否します。
        """
        description = error.description()
        url = error.url().toString()
        logger.error("証明書エラー: %s (%s)", description, url)

        if insecure_ssl_enabled():
            logger.warning(
                "%s が設定されているため証明書エラーを無視して接続します。", ENV_INSECURE_SSL
            )
            error.acceptCertificate()
            return

        error.rejectCertificate()
        self.info_bar.setText(t(
            "  ⚠ The connection was stopped because the certificate could not "
            "be verified: {reason}\n"
            "  Set the environment variable {env}=1 only if you mean to allow "
            "this, for instance behind a corporate proxy.",
            reason=description, env=ENV_INSECURE_SSL,
        ))
        self.info_bar.setStyleSheet(
            "background-color: #f38ba8; color: #11111b; padding: 8px; font-weight: bold; font-size: 13px;"
        )

    def _set_error_bar(self, text: str):
        self.info_bar.setText(text)
        self.info_bar.setStyleSheet(
            "background-color: #f38ba8; color: #11111b; padding: 8px; font-weight: bold; font-size: 13px;"
        )

    def on_proxy_credentials_missing(self, proxy_host: str):
        self._proxy_auth_failed = True
        self._set_error_bar(t(
            "  ⚠ The proxy {host} is asking for authentication.\n"
            "  Enter the proxy user name and password in Settings.",
            host=proxy_host,
        ))

    def on_load_finished(self, ok: bool):
        if ok or self._proxy_auth_failed:
            return
        # 読み込み失敗の原因が分からないと打つ手が無いので、候補を出す
        self._set_error_bar(t(
            "  ⚠ The page could not be loaded.\n"
            "  Proxy: {proxy}\n"
            "  Behind a corporate proxy, set the proxy credentials in Settings.",
            proxy=proxy_manager.redacted_proxy_url(),
        ))

    def on_cookie_added(self, cookie):
        if not self.provider.owns_cookie_domain(cookie.domain()):
            return
        try:
            name = cookie.name().data().decode("utf-8")
            value = cookie.value().data().decode("utf-8")
        except UnicodeDecodeError:
            return

        self.cookies[name] = value
        if name == self.provider.session_cookie_name:
            self.session_key = value

    def on_url_changed(self, url):
        # ログイン成功時は対象サイトのトップ等へ遷移し、セッション Cookie が発行される。
        # 「ログイン画面ではない」かつ「セッション Cookie を取れた」で成功とみなす。
        url_str = url.toString()
        if not self.session_key:
            return
        if self.provider.cookie_domain not in url_str:
            return
        if self.provider.is_login_page(url_str):
            return

        self.cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        self.accept()


class AccountDialog(QDialog):
    def __init__(self, parent=None, account: Account = None):
        super().__init__(parent)
        self.account = account
        # ログイン用のブラウザプロファイルはアカウント ID ごとに分けるため、
        # 新規追加のときも先に ID を決めておく (保存時にこの ID をそのまま使う)
        self.account_id = account.id if account else str(uuid.uuid4())
        # [A-02] 対策: 「ブラウザログインを実際に使ったか」を覚えておくフラグ。
        # LoginDialog はコンストラクタの時点で browser_profile.get_profile() を呼び、
        # ForcePersistentCookies でプロファイルをディスクへ永続化してしまう
        # (ログインの成否は問わない)。新規追加モードでこれを使った後に
        # 追加そのものをキャンセルすると、config.json から参照されない
        # プロファイル (生きたセッション Cookie を含む) が残り続けてしまうため、
        # reject() 側でこのフラグを見て後始末する。
        self.used_browser_login = False
        self.init_ui()

    def current_provider(self) -> UsageProvider:
        return providers.get(self.provider_combo.currentData())

    def init_ui(self):
        self.setWindowTitle(t("Add Account") if not self.account
                            else t("Edit Account"))
        self.resize(560, 560)
        self.setMinimumSize(460, 420)

        # 取得先を切り替えたことを検知するために、直前の選択を覚えておく
        self._last_provider_id = None

        layout = QVBoxLayout()
        layout.setSpacing(15)

        self.form_layout = QFormLayout()
        self.form_layout.setSpacing(10)
        self.form_layout.setLabelAlignment(Qt.AlignRight)

        # 取得先 (プロバイダ)
        self.provider_combo = QComboBox()
        for provider in providers.all_providers():
            label = (provider.label if provider.implemented
                     else t("{provider} (not ready)", provider=provider.label))
            self.provider_combo.addItem(label, provider.id)
        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        self.form_layout.addRow(t("Provider:"), self.provider_combo)

        self.provider_desc = QLabel("")
        self.provider_desc.setStyleSheet("color: #a6adc8; font-size: 11px;")
        self.provider_desc.setWordWrap(True)
        self.form_layout.addRow("", self.provider_desc)

        # アカウント名
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(t("e.g. Main account"))
        self.form_layout.addRow(t("Account name:"), self.name_input)

        # 取得先ごとの追加フィールド (Organization ID / エンドポイント URL など)。
        # 使わない取得先では行ごと隠す。
        self.org_input = QLineEdit()
        self.org_label = QLabel("Organization ID:")
        self.form_layout.addRow(self.org_label, self.org_input)

        # 上限金額 (課金系の取得先のみ)。
        # QLineEdit ではなく QDoubleSpinBox を使うのは、負数や文字列を
        # Qt 側が構造的に弾いてくれるためです (検証コードを書かずに済む)。
        #
        # **setSpecialValueText は使いません。** 0 のときに長い文言
        # (「未設定」等) を出すと、入力欄ではなく警告文に見える上、
        # 直接打ち込むのに全選択して消す手間がかかります。
        # 数字を打つ欄として素直に見せ、0 の意味はツールチップで補います。
        self.budget_input = QDoubleSpinBox()
        self.budget_input.setDecimals(0)
        self.budget_input.setRange(0.0, 100_000_000.0)
        self.budget_input.setGroupSeparatorShown(True)
        self.budget_label = QLabel(t("Spending cap:"))
        self.form_layout.addRow(self.budget_label, self.budget_input)

        # 資格情報と自動取得ボタン
        cookie_widget = QWidget()
        cookie_layout = QVBoxLayout(cookie_widget)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.setSpacing(8)

        self.get_cookie_btn = QPushButton(
            t("🔑 Sign in with a browser and get cookies automatically"))
        self.get_cookie_btn.setStyleSheet(
            "QPushButton { background-color: #a6e3a1; color: #11111b; font-weight: bold; border-radius: 6px; padding: 8px; }"
            "QPushButton:hover { background-color: #b4befe; }"
        )
        self.get_cookie_btn.clicked.connect(self.login_and_get_cookie)
        cookie_layout.addWidget(self.get_cookie_btn)

        # [A-01] 対策: 編集モードで開いた直後は、既存の Cookie をこの欄に
        # 入れない (init_ui の末尾を参照)。画面共有や覗き見で有効なセッション
        # Cookie がそのまま漏れるのを防ぐため、既定では空 + プレースホルダ表示に
        # しておき、確認したいときだけ「表示」で明示的に呼び出してもらう。
        cookie_input_row = QHBoxLayout()
        cookie_input_row.setContentsMargins(0, 0, 0, 0)

        self.cookie_input = QTextEdit()
        self.cookie_input.setAcceptRichText(False)
        cookie_input_row.addWidget(self.cookie_input)

        self.show_cookie_checkbox = QCheckBox(t("Show"))
        self.show_cookie_checkbox.toggled.connect(self.on_show_cookie_toggled)
        # 新規追加モードでは隠すべき既存値がそもそも無いので、編集モードでのみ出す
        # (on_provider_changed 実行前の暫定状態。最終的な出し分けは init_ui 末尾で行う)
        self.show_cookie_checkbox.setVisible(False)
        cookie_input_row.addWidget(self.show_cookie_checkbox, 0, Qt.AlignTop)

        cookie_layout.addLayout(cookie_input_row)

        self.credential_label = QLabel("Cookie / sessionKey:")
        self.form_layout.addRow(self.credential_label, cookie_widget)
        self._cookie_widget = cookie_widget

        # 有効/無効
        self.enabled_checkbox = QCheckBox(t("Enable this account"))
        self.enabled_checkbox.setChecked(True)
        self.form_layout.addRow("", self.enabled_checkbox)

        layout.addLayout(self.form_layout)

        info_label = QLabel(t(
            "* Credentials are encrypted with Windows DPAPI, saved under your\n"
            "  user profile, and sent nowhere but to the service itself.\n"
            "  The sign-in state is kept in a browser profile of this account's\n"
            "  own, so signing out of your usual browser does not expire\n"
            "  this app's cookies."
        ))
        info_label.setStyleSheet("color: #89b4fa; font-size: 11px;")
        layout.addWidget(info_label)

        # ボタン
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton(t("Save"))
        self.save_btn.setObjectName("primaryButton")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.accept)

        self.cancel_btn = QPushButton(t("Cancel"))
        self.cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.save_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        # 編集モードの場合、既存のデータをセット
        if self.account:
            index = self.provider_combo.findData(self.account.provider)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
            self.name_input.setText(self.account.name)
            self.org_input.setText(self.account.organization_id)
            self.budget_input.setValue(self.account.budget)
            # [A-01] 対策: 編集モードで開いた直後は Cookie 欄を空のままにする。
            # 名前を変えるだけの編集でも有効なセッション Cookie が全文平文で
            # 画面に出てしまっていたため、既定では入れない。確認したいときだけ
            # 「表示」で明示的に呼び出してもらう (on_show_cookie_toggled 参照)。
            # 保存時に欄が空なら self.account.cookie をそのまま使う
            # (get_account_data / validate_inputs 参照) ので、既存の資格情報は
            # 何も操作しなければ消えない。
            self.show_cookie_checkbox.setVisible(True)
            self.enabled_checkbox.setChecked(self.account.enabled)

        # 選択中の取得先に合わせて入力欄を整える (編集モードの反映後に呼ぶ)
        self.on_provider_changed()

    def on_provider_changed(self):
        """取得先の切り替えに合わせて入力欄の見せ方を変えます。"""
        provider = self.current_provider()

        # プロバイダのクラス属性は訳す前の原文 (英語) です。
        # **訳すのは、こうして画面へ入れる瞬間です。**
        self.provider_desc.setText(t(provider.description))
        self.credential_label.setText(
            t("{label}:", label=t(provider.credential_label)))
        if self.account:
            # 編集モードでは欄を空のまま開くため (A-01 対策)、
            # 取得先ごとの入力ヒントより「何もしなければ既存値を維持する」
            # ことを優先して案内する。
            self.cookie_input.setPlaceholderText(t(
                "A credential is already saved "
                "(fill this in only if you want to change it)"
            ))
        else:
            self.cookie_input.setPlaceholderText(t(provider.credential_hint))

        # ブラウザログインは Cookie 認証の取得先だけの機能
        is_cookie_auth = provider.auth_kind == AUTH_COOKIE
        self.get_cookie_btn.setVisible(is_cookie_auth)

        self.org_label.setText(
            t("{label}:", label=t(provider.extra_field_label)))
        self.org_input.setPlaceholderText(t(provider.extra_field_hint))
        self.form_layout.setRowVisible(self.org_input, provider.uses_extra_field)

        # 追加フィールドは取得先ごとに意味が違う (Organization ID / エンドポイント URL)。
        # 取り替えないと、URL を入れたまま Claude に切り替えて保存したときに
        # URL が Organization ID として保存されてしまう。
        first_time = self._last_provider_id is None
        switched = self._last_provider_id is not None and self._last_provider_id != provider.id
        self._last_provider_id = provider.id
        if switched:
            self.org_input.setText(provider.extra_field_default)
        elif self.account is None and not self.org_input.text().strip():
            self.org_input.setText(provider.extra_field_default)

        # 上限金額は課金系だけの機能。通貨記号は取得先が持っている。
        self.form_layout.setRowVisible(self.budget_input, provider.supports_budget)
        if provider.supports_budget:
            symbol = providers.currency_symbol(provider.currency)
            self.budget_label.setText(
                t("Spending cap ({currency}):", currency=provider.currency))
            self.budget_input.setPrefix(f"{symbol} " if symbol else "")
            self.budget_input.setToolTip(t(
                "The share of this amount you have spent is shown as a gauge.\n"
                "Set it to 0 to show the amount only, with no gauge."
            ))

            # 既定値を入れるのは「新規追加の初回」と「取得先の切り替え」だけ。
            # 編集中のアカウントに保存済みの値 (0 を含む) は上書きしない。
            # 切り替え時に持ち越さないのは通貨が変わるためで、JPY の 10000 を
            # USD の上限として引き継ぐと桁が2つ違ってしまう。
            if switched or (first_time and self.account is None):
                self.budget_input.setValue(provider.default_budget)

        # OAuth 系は別ツールのログイン状態を借りるので、ここに貼るものが無い
        needs_input = provider.auth_kind != AUTH_OAUTH
        self.form_layout.setRowVisible(self._cookie_widget, needs_input)

    def on_show_cookie_toggled(self, checked: bool):
        """[A-01] 「表示」チェックボックスの切り替え。

        編集モードでは Cookie 欄を空のまま開いているため (init_ui 参照)、
        ここで初めて実際の値を差し込みます。もう一度外すと元の非表示状態
        (空 + プレースホルダ) に戻します。

        表示している間に「🔑 ブラウザでログインしてCookieを自動取得」で
        新しい値を取得した場合など、欄の中身が保存済みの値とは別物に
        変わっていることがあります。その状態でチェックを外して空に戻すと
        せっかく取得した新しい値を消してしまうため、欄の中身が表示した値と
        まだ一致しているとき (＝誰も書き換えていないとき) だけ空に戻します。
        """
        if not self.account:
            return
        if checked:
            self.cookie_input.setPlainText(self.account.cookie)
        elif self.cookie_input.toPlainText() == self.account.cookie:
            self.cookie_input.clear()

    def login_and_get_cookie(self):
        provider = self.current_provider()
        # [A-02] 対策: LoginDialog はコンストラクタの時点で
        # browser_profile.get_profile() を呼び、ForcePersistentCookies で
        # プロファイルをディスクへ永続化してしまう (ログインの成否は問わない)。
        # ダイアログを開いた時点で「ブラウザログインを使った」とみなし、
        # reject() 側の後始末 (forget_profile) の対象にする。
        self.used_browser_login = True
        dialog = LoginDialog(self.account_id, provider, self)
        if dialog.exec() == QDialog.Accepted and dialog.cookie_header:
            # 「表示」中 (＝欄に保存済みの古い値を出していた) だったら、
            # これから入れる新しい値と取り違えて上書きされないよう
            # 先にチェックを外しておく (on_show_cookie_toggled 参照)。
            if self.show_cookie_checkbox.isChecked():
                self.show_cookie_checkbox.setChecked(False)
            self.cookie_input.setPlainText(dialog.cookie_header)
            # 欄には今取得したばかりの実値が入っている。「表示」は保存済みの
            # 旧い値を呼び戻すためのものなので、ここで出したままだと
            # 押し間違いで取得直後の値を古い値に上書きしてしまう。隠す。
            self.show_cookie_checkbox.setVisible(False)
            if not self.name_input.text().strip():
                self.name_input.setText(t("(enter a name)"))
                self.name_input.selectAll()
            if dialog.used_manual_fallback:
                # ログインはしていない。「成功しました」と出すと、
                # 次に切れたとき何をすればよいか分からなくなる。
                QMessageBox.information(
                    self, t("Set"),
                    t("The {credential} you pasted has been set.\n"
                      "When it expires, get a new one the same way.",
                      credential=t(provider.credential_label)),
                )
            else:
                QMessageBox.information(
                    self, t("Success"),
                    t("Signed in, and the cookies were collected automatically."))

    def _effective_cookie(self) -> str:
        """欄の入力値から、実際に保存する資格情報を決めます。

        [A-01] 対策で編集モードは Cookie 欄を空のまま開くため、ここで空を
        そのまま素通しすると、既存の資格情報が空文字で上書きされて消えて
        しまいます。編集モードで欄が空のまま (＝ユーザーが何も入力しなかった)
        なら、保存済みの値 (self.account.cookie) をそのまま維持します。
        新規追加モード (self.account is None) では元々ここに戻す既存値が
        無いので、空は空のまま返します (未入力チェックは validate_inputs 側)。
        """
        cookie = self.cookie_input.toPlainText().strip()
        if not cookie and self.account is not None:
            return self.account.cookie
        return cookie

    def get_account_data(self) -> Account:
        """入力されたデータを元にAccountインスタンスを作成して返します。"""
        provider = self.current_provider()
        name = self.name_input.text().strip()
        org_id = self.org_input.text().strip() if provider.uses_extra_field else ""
        cookie = self._effective_cookie()
        enabled = self.enabled_checkbox.isChecked()

        budget = self.budget_input.value() if provider.supports_budget else 0.0

        # ブラウザプロファイルと結びつけるため、ダイアログで決めた ID を使う
        return Account(name=name, organization_id=org_id, cookie=cookie,
                       enabled=enabled, id=self.account_id, provider=provider.id,
                       budget=budget)

    def validate_inputs(self) -> bool:
        """入力値のバリデーションを行います。"""
        provider = self.current_provider()

        if not self.name_input.text().strip():
            QMessageBox.warning(self, t("Input error"),
                                t("Enter an account name."))
            self.name_input.setFocus()
            return False

        # 欄へ直接貼られた場合も、救済ダイアログと同じ取り出しを通す。
        # 貼り方は入口によって変わらないので、扱いも変えない。
        pasted = self.cookie_input.toPlainText().strip()
        if pasted:
            normalized = provider.normalize_credential(pasted)
            if normalized != pasted:
                self.cookie_input.setPlainText(normalized)

        # OAuth 系は貼り付けるものが無いので、資格情報のチェックは省く
        if provider.auth_kind != AUTH_OAUTH:
            # [A-01] 対策で編集モードは欄を空のまま開くため、「何も入力しなかった」
            # (＝保存済みの値をそのまま使う) のか「新しく入力した」のかで
            # 扱いを分ける。typed が空なら _effective_cookie() が
            # self.account.cookie を返す (新規追加モードでは空のまま)。
            typed = self.cookie_input.toPlainText().strip()
            cookie = self._effective_cookie()
            if not cookie:
                QMessageBox.warning(
                    self, t("Input error"),
                    t("Enter {credential}.",
                      credential=t(provider.credential_label)),
                )
                self.cookie_input.setFocus()
                return False

            # 保存済みの値をそのまま使う場合、それは過去に一度この検証を
            # 通っている値なので、ここでの書式チェック・確認ダイアログは
            # 新しく入力されたときだけ行う。毎回行うと、名前を変えるだけの
            # 編集でも無関係な確認ダイアログが出てしまう。
            if typed:
                if provider.credential_marker and provider.credential_marker not in cookie:
                    reply = QMessageBox.question(
                        self, t("Confirm"),
                        t("Nothing that looks like {marker}... was found in "
                          "what you entered.\nSave it anyway?",
                          marker=provider.credential_marker),
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                    )
                    if reply != QMessageBox.Yes:
                        self.cookie_input.setFocus()
                        return False

                # 欄へ直接貼られた場合も、救済ダイアログと同じ確認を通す
                # (取り違えは入口を問わず同じように起きる)。
                warning = provider.validate_credential(cookie)
                if warning:
                    reply = QMessageBox.question(
                        self, t("Confirm"),
                        t("{warning}\n\nSave it anyway?", warning=warning),
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                    )
                    if reply != QMessageBox.Yes:
                        self.cookie_input.setFocus()
                        return False

        if provider.uses_extra_field:
            # 書式の判定はプロバイダに任せる (UUID なのか URL なのかは取得先の事情)
            warning = provider.validate_extra(self.org_input.text().strip())
            if warning:
                reply = QMessageBox.question(
                    self, t("Confirm"), warning,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    self.org_input.setFocus()
                    return False

        return True

    def accept(self):
        if self.validate_inputs():
            super().accept()

    def reject(self):
        """[A-02] 対策: 追加をキャンセルしたときに孤児化するブラウザプロファイルを片付けます。

        LoginDialog はコンストラクタの時点で browser_profile.get_profile() を呼び、
        ForcePersistentCookies でプロファイルをディスクへ永続化してしまいます
        (ログインの成否は問いません)。そのため「ログイン成功 → 追加をキャンセル」
        のように保存に至らなかった場合、config.json のどのアカウントからも
        参照されないプロファイル (生きたセッション Cookie を含む) がディスクに
        残り続け、削除する手段が無くなってしまいます。

        ここを削除対象にできるのは、**新規追加モード (self.account is None) で、
        かつ実際にブラウザログインを使った場合だけ** です。編集モードの
        account_id は既存の保存済みアカウントのものなので、キャンセルしただけで
        forget_profile() を呼ぶと、そのアカウントのログイン状態そのものを
        破壊してしまいます (実害の大きい取り違えなので、ここは絶対に混同しない)。
        """
        if self.account is None and self.used_browser_login:
            browser_profile.forget_profile(self.account_id)
            logger.info(
                "追加をキャンセルしたため、孤児化するブラウザプロファイルを削除しました: %s",
                self.account_id,
            )
        super().reject()
