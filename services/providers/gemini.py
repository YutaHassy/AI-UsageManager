"""Gemini (ブラウザ版) の利用枠。

DevTools で実際の通信を確認して判明した経路です (2026-08 実測):

  1. GET https://gemini.google.com/usage   (Cookie 認証)
     応答の HTML に WIZ_global_data が埋め込まれており、そこから
     以降の呼び出しに必要な値を取り出します:

       SNlM0e → at    POST ボディに必須の XSRF トークン
       cfb2h  → bl    ビルドラベル (URL の bl=)
       FdrFJe → f.sid セッション ID (URL の f.sid=)

     **ChatGPT と違い Bearer トークンは使いません。** Cookie と at の組です。

  2. POST https://gemini.google.com/_/BardChatUi/data/batchexecute
          ?rpcids=jSf9Qc&source-path=%2Fusage&bl=<bl>&f.sid=<sid>&hl=ja&_reqid=<n>&rt=c
     ボディ (フォーム)  f.req=[[["jSf9Qc","[]",null,"generic"]]]  と  at=<at>

     引数は空配列 "[]" です (ユーザーの指定は Cookie だけで決まる)。

  応答は JSON ではなく、)]}' の後に「長さの行 + JSON の行」が交互に続く形式:

    )]}'

    251
    [["wrb.fr","jSf9Qc","[2,[[999999,0,5,...]]]",null,null,null,"generic"],["di",176],...]
    25
    [["e",4,null,null,287]]

  wrb.fr の3番目が、さらに JSON 文字列として入れ子になっています。実測値:

    [2,[
      [999999, 0,          5, null, null, [[1786420628,997914000],2]],
      [  2382, 0.01,       1, [[1785985028,997821000]]],
      [ 48329, 0.00112903, 2, [[1786420628,997914000]]]
     ], false]

  各要素の意味 (画面表示と突き合わせて確認済み):

    [0] 上限値。**単位が不明**で画面にも出ないため使いません。
        999999 は「画面に出さない枠」を表すセンチネルでした。
    [1] **使用済み**の割合 (0.0-1.0)。0.01 → 画面の「1% 使用中」と一致。
        Antigravity の remainingFraction は逆 (残り割合) なので混同しないこと。
    [2] 枠の種別。1 = 現在の使用量 / 2 = 1週間の上限 / 5 = 画面に出ない枠。
    [3] リセット時刻 [[秒, ナノ秒]]。1785985028 → 8/6 11:57、
        1786420628 → 8/11 12:57 で、画面の表示と一致しました。
        ただし種別5 だけは [3][4] が null で、[5] に [[秒,ナノ秒],2] の形で入ります。

  **種別の対応は Pro プラン1件の実測からの推定です。** 別プランで違う値が
  来る可能性があるため、知らない種別は捨てずに汎用ラベルで出します
  (黙って消すと、枠が増えたことにも減ったことにも気づけません)。

**Cookie は cookiejar で送らなければなりません** (2026-08 に壊れた原因):

  /usage は Google の bot 判定画面へ 302 で飛ばすことがあります:

    gemini.google.com/usage
      → www.google.com/sorry/index?continue=...   (302)
      → gemini.google.com/usage?google_abuse=...  (302)
      → gemini.google.com/usage                   (200)

  requests は**リダイレクトを追うとき、手で設定した Cookie ヘッダを捨てます**
  (Session.resolve_redirects)。そのため最後の /usage には
  GOOGLE_ABUSE_EXEMPTION しか付かず、**サインアウト状態の HTML が 200 で
  返っていました。** 401 ではないので認証エラーにもならず、表に出る症状は
  「SNlM0e が取り出せない」だけ — Cookie の失効と見分けがつきません
  (実際には RotateCookies が 200 を返す＝生きている状態でした)。

  対処は base.HttpClient に入れてあります (_stash_cookies)。ここでは
  ヘッダに Cookie を載せるだけでよく、jar への移し替えは共通側がやります。

  なお **1回の取得の中では jar を持ち越す必要があります。** ページ取得で
  受け取る GOOGLE_ABUSE_EXEMPTION / NID / __Secure-3PSID などを捨てると、
  続く batchexecute だけが 400 で弾かれます (実測)。

認証の弱点と、その埋め合わせ:
  Google のログインは埋め込みブラウザから拒否されるため、Cookie は
  普段お使いのブラウザから手で持ってきてもらいます (manual_steps 参照)。

  貼ってもらった Cookie のうち __Secure-1PSIDTS は短命で、Google 側が
  定期的に差し替えます。放っておくと「Cookie を貼り直してください」に
  なってしまうため、RotateCookies で取り直します (画面側も同じことをして
  います)。実測 (2026-08):

    POST https://accounts.google.com/RotateCookies
    ヘッダ  Content-Type: application/json
            Origin: https://accounts.google.com
    Cookie  __Secure-1PSID と __Secure-1PSIDTS
    ボディ  [000,"-0000000000000000000"]

    → 200。__Secure-1PSID (長命な方) はそのまま使い続けられる。

    **200 でも新しい __Secure-1PSIDTS が返らないことがあります。** 2026-08 の
    実測では、数時間の間隔をあけて2回叩いても返るのは __Secure-1PSIDCC /
    __Secure-3PSIDCC だけでした。そして**その日のうちにセッションは失効し、
    貼り直しが必要になりました。**

    つまり「まだ新しいので差し替え不要」なのか「延命に失敗している」のかは、
    まだ切り分けられていません。**200 が返ることは延命できた証拠になりません。**
    返らなかった場合は更新せずに済ませますが、記録は残します
    (静かに済ませると、失効したときに何も手がかりが残りません)。

    **有力な原因: 送る Cookie を絞りすぎていました** (2026-08-09)。参照実装
    (HanaokaYuzu/Gemini-API) と突き合わせたところ、URL もボディも更新間隔も
    こちらと同じで、違いは送る Cookie だけでした。あちらはセッションの jar を
    まるごと送るのに対し、こちらは 1PSID 系の3つしか持っていませんでした。
    **更新用のトークン __Secure-1PSIDRTS は、ブラウザのプロファイルには
    入っているのに設定に残していなかったため、更新の要求にも付いていません。**
    KEPT_COOKIES を広げて様子を見ます (どれが必須かは Google 側の応答から
    分からないため、延命できなかったときは送った Cookie の名前を残します)。

    **なお、これは失効までの時間を延ばすだけです。** 更新は取得に成功した
    直後にしか走らないので、アプリを閉じている間は何も延命しません。
    自動更新 (aiUsageManager.autoRefreshMinutes) を切っていると、更新の
    機会そのものがほぼ無くなります。

  **これは「延命」であって「復旧」ではありません。** 実測で切り分けた結果:

    1PSID + 有効な 1PSIDTS  → 200 (新しい 1PSIDTS が返る)
    1PSID + 期限切れ/壊れた 1PSIDTS → 401
    1PSID のみ (1PSIDTS を送らない)  → 401

  つまり **有効なうちにしか取り直せません。** 失効を検知してから呼んでも
  必ず 401 になるので、取得に成功した (= まだ有効と分かっている) 直後に
  呼んで期限を延ばします。いったん切れてしまったら貼り直してもらうしか
  ありません (アプリを長時間閉じていた場合がこれに当たります)。

  **ボディの形が違うと 400 で弾かれます。** 中身に意味は無さそうですが、
  [000,"-----"] のような短い値では通りませんでした (401 ではなく 400 が返る
  ので、失効したのか形が違うのか区別できず、ここで一度はまりました)。
  また続けて叩くと 429 になるため、更新の間隔を空けます。
"""

import hashlib
import json
import logging
import random
import re
import time
from typing import Any, Dict, List

from services.i18n import t
from services.providers.base import (
    AUTH_COOKIE, HttpClient, UsageError, UsageProvider,
    build_result, extract_cookie_header, parse_cookie_header, percent_metric,
    unix_to_iso,
)

logger = logging.getLogger(__name__)

HOME_URL = "https://gemini.google.com/"
USAGE_PAGE_URL = "https://gemini.google.com/usage"
BATCHEXECUTE_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
USAGE_RPCID = "jSf9Qc"

# Cookie の更新先。詳細はモジュール冒頭の説明を参照。
ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
# 決め打ちのボディ。中身に意味は無いが、形が違うと 400 で弾かれる。
ROTATE_BODY = '[000,"-0000000000000000000"]'
# 更新の間隔 (秒)。取得のたびに叩く必要はなく、続けて叩くと 429 が返る
# (実測では2回目が即 429 だった)。一方で空けすぎると期限切れに間に合わない。
ROTATE_INTERVAL_SEC = 600

# 更新を最後に行った時刻と、そのとき持っていた __Secure-1PSIDTS の指紋。
# アカウント (1PSID のハッシュ) ごとに {キー: (monotonic 秒, TS の指紋)} で持ちます。
#
# **プロバイダはシングルトンです** (services/providers/__init__.py が import 時に
# 1個だけ作り、全アカウントで使い回します)。したがってインスタンス属性でも
# 寿命は同じですが、「どのアカウントの話か」をキーで分ける必要があるため、
# 置き場所ではなく辞書であることに意味があります。
_last_rotation: Dict[str, tuple] = {}

# WIZ_global_data の中のキー名。難読化された短い名前ですが、
# Gemini Web を扱う非公式クライアント各種がそろってこの名前を使っています。
_WIZ_AT = "SNlM0e"
_WIZ_BL = "cfb2h"
_WIZ_SID = "FdrFJe"

# 保存する Cookie の許可リスト。
#
# **Google の Cookie ヘッダには SID / SAPISID / HSID / SSID など、Gemini に
# 限らず Google アカウント全体を操作できるものが混ざります。** 使わないものを
# 抱え込むと、この設定ファイルが漏れたときの被害がそのまま広がるため、
# ここに挙げたものだけを残して他は捨てます。
#
# **ただし絞りすぎると延命できません** (2026-08 に判明)。ここが 1PSID 系の
# 3つだけだった間、RotateCookies は 200 を返しながら新しい __Secure-1PSIDTS を
# 返さず、その日のうちに貼り直しになっていました。参照実装
# (HanaokaYuzu/Gemini-API) と突き合わせると、URL もボディも更新間隔 (600秒) も
# こちらと同じで、**違いは送る Cookie だけ**でした。あちらは絞らず、セッションが
# 持っている jar をまるごと送ります。
#
# そこで、更新に関わるものだけを足してあります:
#
#   __Secure-1PSIDRTS / __Secure-3PSIDRTS … 名前のとおり更新用のトークン。
#       **ブラウザのプロファイルには入っているのに、ここで捨てていました。**
#   NID                                   … アカウントの識別に使われる。
#   __Secure-3PSID / -3PSIDTS / -3PSIDCC  … 1PSID 系の三者間文脈版。
#       RotateCookies の応答は __Secure-3PSIDCC も返してきます。
#
# **SID / SAPISID / HSID / SSID / APISID は足していません。** これらは
# SAPISIDHASH 認証に使えてしまい、Gemini どころか Google アカウント全体に
# 手が届きます。「漏れたときの被害を広げない」という上の判断はそのままです。
#
# **どれが必須かは実測で確かめられていません** (Google 側の応答からは
# 「足りない」と分かる手がかりが返りません)。そのため rotate_cookies は、
# 延命できなかったときに送った Cookie の**名前**を記録に残します。
KEPT_COOKIES = (
    "__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC",
    "__Secure-1PSIDRTS",
    "__Secure-3PSID", "__Secure-3PSIDTS", "__Secure-3PSIDCC",
    "__Secure-3PSIDRTS",
    "NID",
)

# 枠の種別 → 画面に出す名前。Gemini 自身の表記に合わせています
# (「5時間枠」のような、向こうが名乗っていない名前を作らない)。
# 値は訳す前の原文 (英語)。訳すのは _window_metric です。
_WINDOW_LABELS = {1: "Current usage", 2: "Weekly limit"}

# 上限値に入るセンチネル。この値の枠は Gemini の画面にも出ません。
_UNLIMITED = 999999


def extract_request_url(text: str) -> str:
    """貼り付けられた内容から、その通信の宛先 URL を取り出します (診断用)。

    Network の一覧には gstatic.com など別ドメイン宛ての通信も混ざります。
    それを選ぶと Gemini の Cookie は付いていないので、
    「行を選び間違えた」と具体的に言えるようにするために使います。
    """
    match = re.search(r"https?://[^\s\"'\\]+", text or "")
    return match.group(0) if match else None


def _rotation_key(jar: Dict[str, str]) -> str:
    """更新間隔を記録するための、アカウントを見分けるキー。

    Cookie の値をそのまま辞書のキーにすると、例外表示やデバッガに
    生の資格情報が出てしまうため、ハッシュにしておきます。
    """
    return hashlib.sha256(jar.get("__Secure-1PSID", "").encode("utf-8")).hexdigest()[:16]


def _extract_wiz(html: str, key: str) -> str:
    """HTML に埋め込まれた WIZ_global_data から値を1つ取り出します。"""
    match = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(key), html or "")
    return match.group(1) if match else None


def rpc_payload(text: str, rpcid: str):
    """batchexecute の応答から、指定した rpcid の中身を取り出します。

    応答は「長さの行 + JSON の行」の繰り返しですが、**長さを信じて
    切り出すことはしません。** 1文字でもずれた瞬間に全部読めなくなる上、
    ずれても静かに失敗するためです。行ごとに JSON として読めるものを
    拾う方が、余計な行が増えても壊れません。
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            envelopes = json.loads(line)
        except ValueError:
            continue
        if not isinstance(envelopes, list):
            continue
        for envelope in envelopes:
            if (isinstance(envelope, list) and len(envelope) >= 3
                    and envelope[0] == "wrb.fr" and envelope[1] == rpcid):
                try:
                    return json.loads(envelope[2])
                except (TypeError, ValueError):
                    logger.error("%s の中身を JSON として読めませんでした。", rpcid)
                    return None
    return None


class GeminiProvider(UsageProvider):
    id = "gemini"
    label = "Gemini"
    description = ("Shows the Gemini app quotas (current usage and the weekly limit).")

    auth_kind = AUTH_COOKIE
    credential_label = "Cookie"
    credential_hint = (
        "Paste what you copied from your browser.\n"
        "Only the cookies Gemini needs are taken out of it."
    )

    login_url = HOME_URL
    home_url = HOME_URL
    cookie_domain = "google.com"
    session_cookie_name = "__Secure-1PSID"

    # 普段のブラウザで取ってくる道。**これが唯一の道です。**
    # (Google は埋め込みブラウザからのサインインを拒否します。)
    # 詳細は base.UsageProvider.manual_url を参照。
    manual_url = USAGE_PAGE_URL
    manual_steps = (
        "1. Use the button below to open Gemini in your usual browser.\n"
        "   (do not copy the page itself — just open it)\n"
        "\n"
        "2. Press F12 on that page. The developer tools open\n"
        "   beside or below the page.\n"
        "\n"
        '3. Choose the "Network" tab at the top of the developer tools.\n'
        "\n"
        '4. Type  usage  into the "Filter" box below that tab.\n'
        "   * This step matters. Without filtering you will pick a request\n"
        "     that has nothing to do with Gemini, and the cookie you need\n"
        "     will not be attached to it.\n"
        "\n"
        "5. Press F5 to reload the page.\n"
        "   Only the filtered requests are listed.\n"
        "\n"
        "6. Any of the remaining rows will do. Right-click it and choose\n"
        '   "Copy" then "Copy as cURL".\n'
        "\n"
        "7. Paste it into the box below and save.\n"
        "\n"
        "* Picking the wrong row is fine. Save and you will be told\n"
        "  exactly what is missing.\n"
        "* Only the cookies Gemini needs are taken out of what you paste;\n"
        "  the rest (which covers your whole Google account) is discarded.\n"
        '* If you cannot find "Copy as cURL" in step 6, click the row,\n'
        '  go to "Headers" then "Request Headers", right-click the cookie\n'
        '  and use "Copy value" instead.'
    )

    implemented = True

    def __init__(self, timeout: int = 20):
        self.http = HttpClient(credential_label=self.credential_label, timeout=timeout)

    def is_login_page(self, url: str) -> bool:
        url = url or ""
        return "accounts.google.com" in url or "/ServiceLogin" in url

    # ---------------- 資格情報 ----------------

    def normalize_credential(self, value: str) -> str:
        """Cookie ヘッダ全体を貼ってもらい、必要なものだけを残します。

        利用者に Cookie を1つずつ選ばせると、名前の取り違えや
        コピー漏れが必ず起きます。まるごと貼ってもらった上で、
        **こちらが減らす**のが安全側です。
        """
        value = (value or "").strip()
        jar = parse_cookie_header(extract_cookie_header(value))

        kept = [(name, jar[name]) for name in KEPT_COOKIES if name in jar]
        if not kept:
            # 目当てのものが1つも無い = 貼り間違い。
            # ここで空にすると何を貼ったか分からなくなるので、そのまま返して
            # validate_credential に説明させる。
            return value

        dropped = len(jar) - len(kept)
        if dropped:
            # 値は絶対に出さない (ログに Google の資格情報を残さない)
            logger.info("Gemini に不要な Cookie を %d 件破棄しました。", dropped)
        return "; ".join(f"{name}={val}" for name, val in kept)

    def cookies_for_profile(self, pasted: str) -> str:
        """プロファイルには、絞らずに貼られた Cookie をそのまま預けます。

        normalize_credential が3つに絞るのは設定ファイルの話です。
        セッションの更新には SID / SAPISID などが要るので、ここでは
        貼り付けの飾り (curl のオプション等) だけを外して全部渡します。
        """
        return extract_cookie_header(pasted)

    def validate_credential(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if self.session_cookie_name in parse_cookie_header(extract_cookie_header(value)):
            return ""

        # ここから下は失敗の説明。「見つかりません」だけだと、
        # 何を直せばよいのか分からず同じ操作を繰り返すことになる。

        # 取り違えその1: ページの本文をコピーした。
        # Cookie は HttpOnly なので、ページを見ても本文には絶対に出ない。
        #
        # **ここの日本語は訳しません。** これは画面に出す文言ではなく、
        # Gemini のページに書かれている文字そのもの (探す目印) です。
        # t() を通すと、この拡張の表示言語で探すことになり、利用者の
        # Gemini が別の言語で表示されている場合に見つけられなくなります。
        # 逆に言うと、いまの目印は日本語表示の Gemini でしか当たりません
        # (当たらなくても下の一般的な説明に落ちるだけで、実害はありません)。
        if "使用量" in value or "Gemini を使用できる時間" in value:
            return t(
                "This looks like the text of the page.\n"
                "Cookies are never shown on the page, so they cannot be taken "
                "from its text.\n"
                "Take it from the F12 developer tools instead (see the steps below)."
            )

        # 取り違えその2: Network の行を選び間違えた。
        # 一覧には別ドメイン宛ての通信も混ざり、並び順も毎回変わるため、
        # これが最も起きやすい。宛先を名指しで返す。
        url = extract_request_url(value)
        if url and self.cookie_domain not in url.split("/")[2]:
            return t(
                "The request you picked goes to {host}.\n"
                "That is not Gemini ({domain}), so the cookie you need\n"
                "is not attached to it.\n"
                "Type usage into the Network Filter box and pick one of\n"
                "the remaining rows instead.",
                host=url.split('/')[2], domain=self.cookie_domain,
            )

        return t(
            "{cookie} was not found.\n"
            "Are you working in a browser that is signed in to Gemini?\n"
            "This cookie does not exist when signed out or in a private window.",
            cookie=self.session_cookie_name,
        )

    # ---------------- 取得 ----------------

    def _headers(self, cookie: str, extra: Dict[str, str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": "*/*",
            "Referer": USAGE_PAGE_URL,
            "Origin": "https://gemini.google.com",
            "Cookie": cookie,
        }
        headers.update(extra or {})
        return headers

    def _fetch_page_tokens(self, cookie: str) -> Dict[str, str]:
        """使用量ページの HTML から at / bl / f.sid を取り出します。

        bl (ビルドラベル) を決め打ちしないのは、Google 側の更新で
        すぐ古くなるためです。毎回ページから読み直します。
        """
        html = self.http.get_text(USAGE_PAGE_URL, self._headers(cookie),
                                  t("the usage page"))

        at_token = _extract_wiz(html, _WIZ_AT)
        if not at_token:
            # ログイン画面の HTML が返っているとここに落ちる
            raise UsageError(
                t("No authentication token could be taken from the usage page.\n"
                  "The cookie may have expired. Paste a fresh one."),
                auth_error=True,
            )
        return {
            "at": at_token,
            "bl": _extract_wiz(html, _WIZ_BL),
            "sid": _extract_wiz(html, _WIZ_SID),
        }

    def rotate_cookies(self, cookie: str) -> str:
        """短命な __Secure-1PSIDTS を取り直し、更新後の Cookie ヘッダを返します。

        **まだ有効な Cookie を渡してください。** 期限切れのものを渡しても
        401 になるだけです (理由はモジュール冒頭の説明を参照)。

        取り直せなかった場合は None を返します。更新できなくても使用状況の
        取得自体は成功しているので、失敗を例外にはしません。
        """
        jar = parse_cookie_header(cookie)
        key = _rotation_key(jar)

        # いま持っている __Secure-1PSIDTS の指紋。値そのものを持ち歩くと
        # 例外表示やデバッガに出てしまうため、ハッシュにしておきます
        # (_rotation_key と同じ考え方)。
        current_ts = hashlib.sha256(
            jar.get("__Secure-1PSIDTS", "").encode("utf-8")).hexdigest()[:16]

        last = _last_rotation.get(key)
        if last is not None:
            last_at, last_ts = last
            # **貼り直された直後は間隔を待ちません。** キーは __Secure-1PSID の
            # ハッシュですが、あれは長命な方なので**貼り直しても変わりません**。
            # 時刻だけで見ていたため、利用者が新しい Cookie を貼った直後 —
            # いちばん延命を急ぎたいところ — で最大10分待たされていました。
            # TS が入れ替わっていれば別の資格情報なので、すぐ延命に入ります。
            if last_ts == current_ts and time.monotonic() - last_at < ROTATE_INTERVAL_SEC:
                logger.debug("前回の Cookie 更新から間もないため、今回は見送ります。")
                return None

        # **記録は投げる前に付けます。** 失敗しても間隔を空けたいためです
        # (401 なら何度叩いても 401 ですし、続けて叩くと 429 が返ります)。
        # 瞬断で1回落ちると次まで待つことになりますが、その間も取得自体は
        # 動いており、待つ側に倒す方が安全です。
        _last_rotation[key] = (time.monotonic(), current_ts)

        # **保存してあるものは全部送ります。** 以前はここで 1PSID 系だけに
        # 絞っていましたが、保存の時点で既に KEPT_COOKIES に絞られているため、
        # ここで二重に削ると更新用のトークン (RTS) や NID まで落ちていました。
        # 「アカウント全体を操作できる Cookie を渡さない」という当初の意図は
        # KEPT_COOKIES 側で担保してあります (あちらの説明を参照)。
        sending = "; ".join(f"{name}={value}" for name, value in jar.items())

        try:
            response = self.http.post_response(
                ROTATE_COOKIES_URL,
                {
                    "User-Agent": self.http.user_agent,
                    "Content-Type": "application/json",
                    "Origin": "https://accounts.google.com",
                    "Cookie": sending,
                },
                t("the cookie refresh"),
                data=ROTATE_BODY,
            )
        except UsageError as e:
            # 401 (1PSID ごと失効) も 429 (叩きすぎ) もここに来る。
            # どちらも「今は更新できない」だけなので、貼り直しの案内に委ねる。
            logger.info("Cookie を更新できませんでした: %s", e)
            return None

        new_ts = response.cookies.get("__Secure-1PSIDTS")
        if not new_ts:
            # 200 は返っているが延命はできていない。**これが「まだ不要」なのか
            # 「延命に失敗している」のかは分かっていません** (モジュール冒頭の
            # 説明を参照)。失効したときに手がかりが残るよう、記録は残します。
            #
            # **送った Cookie の名前も残します** (値は絶対に出しません)。
            # 絞りすぎが原因かどうかは、これが無いと後から確かめられません。
            logger.warning(
                "Cookie の更新は 200 でしたが __Secure-1PSIDTS が返らず、"
                "期限を延ばせていません (送った Cookie: %s / 返った Cookie: %s)。"
                "このまま失効した場合は貼り直しが要ります。",
                sorted(jar), sorted(response.cookies.keys()),
            )
            return None

        # **返ってきたものは全部差し替えます。** 以前は 1PSIDTS と 1PSIDCC しか
        # 見ておらず、一緒に返る 3PSIDCC や更新用のトークンを古いまま残して
        # いました。次の更新はその古い値を送ることになります。
        # 許可リストに載っているものだけを取り込むので、ここから設定ファイルに
        # 新しい種類の Cookie が増えることはありません。
        updated = dict(jar)
        for name in KEPT_COOKIES:
            value = response.cookies.get(name)
            if value:
                updated[name] = value
        updated["__Secure-1PSIDTS"] = new_ts

        # **更新後の TS で記録を上書きします。** ここを古いままにすると、
        # 次の取得で「TS が入れ替わっている = 貼り直された」と誤って読み、
        # 間隔を無視して毎回叩きに行きます (429 のもと)。
        _last_rotation[key] = (
            time.monotonic(),
            hashlib.sha256(new_ts.encode("utf-8")).hexdigest()[:16],
        )

        logger.info("__Secure-1PSIDTS を更新しました (差し替えた Cookie: %s)。",
                    sorted(n for n in updated if updated[n] != jar.get(n)))
        return "; ".join(f"{name}={value}" for name, value in updated.items())

    def _call_usage_rpc(self, cookie: str, tokens: Dict[str, str]) -> str:
        params = {
            "rpcids": USAGE_RPCID,
            "source-path": "/usage",
            "hl": "ja",
            # 実物は単調増加の値だが、1回きりの呼び出しなので何でも通る
            "_reqid": str(random.randint(100000, 999999)),
            "rt": "c",
        }
        # 取れなかったものは付けない (空文字を送ると弾かれることがある)
        if tokens.get("bl"):
            params["bl"] = tokens["bl"]
        if tokens.get("sid"):
            params["f.sid"] = tokens["sid"]

        body = {
            "f.req": json.dumps([[[USAGE_RPCID, "[]", None, "generic"]]],
                                separators=(",", ":")),
            "at": tokens["at"],
        }
        headers = self._headers(cookie, {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        })
        return self.http.post_text(BATCHEXECUTE_URL, headers, t("the quota"),
                                   data=body, params=params)

    def fetch_usage(self, credential: str, organization_id: str = "") -> Dict[str, Any]:
        cookie = self.normalize_credential(credential)
        if not cookie:
            raise UsageError(
                t('{credential} is not set. Set it from "Sign In Again".',
                  credential=t(self.credential_label)),
                auth_error=True,
            )

        tokens = self._fetch_page_tokens(cookie)
        text = self._call_usage_rpc(cookie, tokens)

        payload = rpc_payload(text, USAGE_RPCID)
        if payload is None:
            raise UsageError(t(
                "The result for {rpcid} could not be taken out of the response "
                "(Gemini may have changed).",
                rpcid=USAGE_RPCID,
            ))

        result = self.parse_usage_response(payload)

        # ここまで来た = Cookie はまだ有効。**有効なうちにしか取り直せない**ので、
        # このタイミングで期限を延ばしておく (失効を検知してからでは間に合わない)。
        refreshed = self.rotate_cookies(cookie)
        if refreshed:
            result["credential"] = refreshed
        return result

    # ---------------- レスポンス解釈 ----------------

    @staticmethod
    def _reset_seconds(bucket: List[Any]):
        """リセット時刻の「秒」を取り出します。

        実測では [[秒, ナノ秒]] が要素3に入りますが、画面に出ない枠だけは
        要素3・4が null で、要素5に [[秒,ナノ秒], 2] の形で入っていました。
        位置を決め打ちすると片方で必ず外すため、後続の要素から
        [[数値, ...]] の形をしたものを探します。
        """
        for item in bucket[3:]:
            stamp = item
            # [[秒,ナノ秒], 2] のように一段包まれていることがある
            while isinstance(stamp, list) and stamp and isinstance(stamp[0], list):
                stamp = stamp[0]
            if (isinstance(stamp, list) and stamp
                    and isinstance(stamp[0], (int, float))
                    and not isinstance(stamp[0], bool)):
                return stamp[0]
        return None

    @classmethod
    def _bucket_metric(cls, bucket: Any):
        if not isinstance(bucket, list) or len(bucket) < 3:
            return None

        limit, fraction, kind = bucket[0], bucket[1], bucket[2]

        # 画面に出ない枠。0% として出すと「まだ使っていない枠がある」と
        # 誤解されるので、指標にしない。
        if limit == _UNLIMITED:
            logger.debug("上限がセンチネル値の枠を飛ばしました (種別 %r)", kind)
            return None

        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            logger.warning("使用率が数値ではありません (種別 %r): %r", kind, fraction)
            return None

        label = _WINDOW_LABELS.get(kind)
        if label is None:
            # 知らない種別を黙って捨てると、枠が増えたことに気づけない。
            logger.warning("未知の枠種別です: %r", kind)
            label = t("Quota (type {kind})", kind=kind)
        else:
            label = t(label)

        return percent_metric(
            f"window{kind}", label, float(fraction) * 100.0,
            resets_at=unix_to_iso(cls._reset_seconds(bucket)),
        )

    @classmethod
    def parse_usage_response(cls, payload: Any) -> Dict[str, Any]:
        buckets = payload[1] if (isinstance(payload, list) and len(payload) >= 2) else None
        if not isinstance(buckets, list):
            # ここで 0% を返すと「まだ使っていない」と誤解される。
            logger.error("利用枠の形を認識できませんでした: %r", type(payload).__name__)
            raise UsageError(t(
                "The quota format was not recognised (Gemini may have changed)."
            ))

        metrics = [m for m in (cls._bucket_metric(b) for b in buckets) if m]
        if not metrics:
            raise UsageError(t(
                "Not a single quota could be read (buckets: {count}). "
                "Gemini may have changed.",
                count=len(buckets),
            ))

        return build_result(metrics, raw=payload)
