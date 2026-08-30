# AI-UsageManager

See how much of your AI subscription quota you have used, and how much your API
accounts have cost, without leaving VS Code. Claude, ChatGPT, Gemini, Codex CLI,
Antigravity, the Anthropic API and an Azure OpenAI gateway are all shown in one
table, with the account closest to its limit pinned to the status bar.

The extension talks to each provider as *you* — using your own signed-in session
or your own API key — so it shows your real numbers, not an estimate.

The UI is available in English, 日本語, 한국어 and 简体中文.

## What it reads

| Provider | What you see | How you authenticate |
| --- | --- | --- |
| Claude.ai | Pro / Max quotas: 5-hour, weekly, weekly Opus, weekly Sonnet | A `sessionKey` cookie from your browser |
| ChatGPT | Plan quota windows (5-hour / daily / weekly) and add-on credits | A cookie or accessToken from your browser |
| Gemini | App quotas: current usage and the weekly limit | A cookie from your browser |
| Codex CLI | Rate limits: 5-hour / daily / weekly / monthly | Nothing. It reads the CLI's own local session records |
| Antigravity (Gemini) | G1 credit balance | Reuses the gemini-cli sign-in state |
| Anthropic API | This month's cost in USD, broken down by model | Admin API key |
| Azure OpenAI | Cumulative gateway cost in JPY, broken down by model | Gateway API key — see [Limitations](#limitations) |

For the two cost providers you can set a monthly budget, and the spend is then
shown as a percentage of it, next to the quota bars.

Accounts can be enabled and disabled individually, so an account you do not want
refreshed stays registered but is skipped.

## Requirements

### To view and refresh

**Python 3.9 or later**, with `requests` and `urllib3`:

```sh
pip install requests urllib3
```

**That is the whole list.** Nothing else is needed — not for viewing, not for
refreshing, and not for adding or editing an account. If either package is
missing, the extension tells you which Python it tried and gives you a
ready-to-run `pip install` line for exactly that interpreter.

### How an account gets its credential

Adding and editing both happen **on one screen inside the usage tab**. Provider,
name, credential and spending cap are all visible at once, and nothing opens a
second window.

The credential itself comes from **your own browser**:

1. Press `Add Account`. Choose the provider first — the rest of the form
   changes to match it.
2. The form shows how to get that provider's credential, and a button that
   opens the provider's page **in your default browser**. That browser is
   usually signed in already.
3. Follow the steps, copy what they tell you to copy, and paste it into the
   box on the form.
4. Save.

**Paste the whole thing.** For the cookie providers the steps end in "Copy as
cURL", which puts a long command on your clipboard; the extension picks out the
part it needs and discards the rest. You do not have to find the cookie
yourself, and pasting the wrong row is recoverable — save it and you are told
what is missing.

Earlier versions signed you in through a Chromium window bundled with the
extension, which is why `PySide6` used to be in this list. That window is gone.
Two reasons: it made anyone who only wanted to paste an API key install a
browser engine, and some providers refuse to sign in from an embedded browser
at all — a protection that exists because whoever embeds the browser can watch
you type your password. Using the browser you already trust is the honest way
round, and it is usually signed in already.

**One thing was lost with it.** Claude sessions used to be renewed silently in
the background using that window's profile. Now an expired Claude session is
reported to you and you paste a fresh credential. ChatGPT and Gemini are
unaffected — those are renewed by the provider.

### How Python is found

Leave `aiUsageManager.pythonPath` empty and the extension looks, in this order:

1. The `aiUsageManager.pythonPath` setting.
2. The interpreter the Python extension (`ms-python.python`) has selected.
3. `.venv` or `venv` at the root of the workspace — **trusted folders only.**
   A folder you have not trusted may have brought its own `python.exe` with it,
   and running that silently is exactly what workspace trust exists to prevent.
4. `py -3` on Windows, `python3` elsewhere, from `PATH`.

If the first guess turns out to lack `requests`, the extension asks the Python
extension for its answer and retries once, so conda and pyenv environments are
picked up without you configuring anything. If it still guesses wrong, set
`aiUsageManager.pythonPath` to the interpreter's path.

## Opening the view

Click the gauge icon in the activity bar and the usage table opens **as an
editor tab**, not in the sidebar. It is a wide table; squeezed into a narrow
panel the quota names and bars become unreadable.

It opens **in a group beside the active editor**, so it does not cover the file
you opened it to look at. Set `aiUsageManager.openLocation` to `active` and it
opens in the same group as the active editor instead, the way it did before
1.8.0. Either way, a tab you have moved somewhere yourself stays where you put
it — this only decides where a new one appears.

Clicking the status bar item opens the same tab.

## Commands

All commands are under the `AI-UsageManager` category in the Command Palette.

| Command | What it does |
| --- | --- |
| `Open Usage` | Opens the usage tab (so does clicking the status bar) |
| `Refresh All` | Refreshes every account that can be refreshed |
| `Add Account` | Opens the form for a new account |
| `Edit Account` | Opens the form for a registered account |
| `Sign In Again` | Same form, for pasting a fresh credential when one expires |
| `Delete Account` | Deletes the account and its saved sign-in state |
| `Open Settings` | Opens the Settings UI filtered to this extension |
| `Open Settings File (config.json)` | Opens the backend's own config file, for the settings that live only there (`aoai_allowed_hosts`) |
| `Show Log` | Shows the backend log in an output channel |
| `Restart Backend` | Recreates the Python process |
| `Language` | Picks the display language |
| `Sort Accounts` | Picks how the account list is ordered |
| `Zoom In` | Makes the usage tab one step larger |
| `Zoom Out` | Makes the usage tab one step smaller |
| `Set Proxy Password` | Sets or clears the proxy password (stored encrypted, never in settings.json) |
| `Test Proxy Connection` | Tries to reach the network with the current proxy settings |
| `Import Proxy From Environment Variables` | Fills the proxy host/port settings from `HTTP_PROXY` / `HTTPS_PROXY` |

Add, edit, delete and sign-in-again are also available as buttons in the view.
Add, edit and sign-in-again all open the same form; the last is just the name
the button takes when a credential has expired.

## Sizing and ordering the table

The table is meant to be read in a split, beside whatever you are working on,
so it can be resized and its rows can be put in the order you care about.
Both choices are remembered and are there again the next time you open it.

| What | How |
| --- | --- |
| Zoom in / out | The `−` and `＋` buttons in the toolbar, `Ctrl+Mouse wheel` over the table, or `Ctrl+ +` / `Ctrl+ -` while the tab is focused |
| Back to actual size | Click the `100%` label between those two buttons |
| Move one row | Drag the `≡` handle at the right of the row, or focus the row and press `Alt+Up` / `Alt+Down` |
| Sort by a criterion | `⇅` in the toolbar: highest usage first, by name, by provider, in the order they were added, or the order you arranged by hand |

**Everything scales together** — text, gauges, padding and the fixed-width
columns. The table is read by comparing bar lengths across rows, so a font that
grows while its column does not is a table that no longer lines up.

**Choosing a criterion does not throw away the order you arranged by hand.** It
is kept in `config.json` and comes back when you switch back to it. Dragging a
row while a criterion is active switches to the manual order and keeps the
arrangement you were looking at, with the moved row where you dropped it.

`Ctrl+ +` and `Ctrl+ -` are taken over from VS Code's own window zoom while the
usage tab is focused, and released as soon as you move to another tab. Zoom and
drag-to-reorder were verified on VS Code 1.135; the extension installs on 1.85
and later, and on a much older version the scaled layout may not look the way
it does here.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `aiUsageManager.pythonPath` | `""` | The Python that runs the backend. Empty means auto-detect |
| `aiUsageManager.language` | `auto` | Display language: `auto`, `en`, `ja`, `ko`, `zh-cn` |
| `aiUsageManager.openLocation` | `beside` | Where a new usage tab opens: `beside` (a group next to the active editor) or `active` (the same group) |
| `aiUsageManager.zoomLevel` | `100` | Zoom level of the usage tab, in percent (`50`–`200`). The toolbar buttons, `Ctrl+Mouse wheel` and `Ctrl+ +/-` write here |
| `aiUsageManager.accountSort` | `manual` | How the account list is ordered: `manual`, `usage`, `name`, `provider`, `added` |
| `aiUsageManager.autoRefreshMinutes` | `1` | Refresh interval in minutes (`0`, `1`, `5`, `10`, `30`, `60`). `0` disables it |
| `aiUsageManager.refreshOnOpen` | `true` | Refresh once when the view is opened |
| `aiUsageManager.showStatusBar` | `true` | Show the account closest to its limit in the status bar |
| `aiUsageManager.proxy.mode` | `system` | How to reach the network: `system`, `manual`, `none` |
| `aiUsageManager.proxy.host` | `""` | Proxy host, used when `proxy.mode` is `manual` |
| `aiUsageManager.proxy.port` | `8080` | Proxy port, used when `proxy.mode` is `manual` |
| `aiUsageManager.proxy.username` | `""` | Proxy user name. The password has no setting key — use `Set Proxy Password` instead |

`aiUsageManager.language` covers the extension's own views and the backend's
messages. Command names in the Command Palette and the description text of the
settings themselves always follow VS Code's display language — VS Code resolves
those, and an extension cannot override them.

**The proxy password is never stored in `settings.json`.** That file is plain
text and can be synced across machines through Settings Sync. The password goes
through the `Set Proxy Password` command instead, and the backend encrypts it
with Windows DPAPI before writing it to its own config file.

## Privacy and stored credentials

Reading your usage requires your credentials, so the extension keeps them. Here
is exactly what that means.

**Where they live.** Accounts, cookies and API keys are stored in a single file:

- Windows: `%LOCALAPPDATA%\AI-UsageManager\config.json`
- Elsewhere: `$XDG_CONFIG_HOME/AI-UsageManager/config.json` (falling back to
  `~/.config/AI-UsageManager/config.json`)

It is deliberately outside the project directory, so that syncing, zipping or
sharing a source folder cannot carry a live session cookie out with it.

**How they are protected.** On Windows, cookies and API keys are encrypted with
DPAPI, which binds them to the same Windows user on the same machine — a copied
`config.json` is useless elsewhere. **On platforms where DPAPI is not available,
or when the DPAPI call fails, they are written in plain text.** The extension
does not hide this: the view shows `⚠ Credentials are stored unencrypted` at the
bottom whenever that is the case.

**Where they go.** Credentials are sent to the provider that the account belongs
to, and nowhere else. There is no server belonging to this project, no
telemetry, and no analytics.

**They are never handed to the webview.** The usage table receives only a
boolean saying whether a credential is set. A webview is a DevTools-inspectable
environment; there is no reason to put a session cookie inside one.

**Deleting an account deletes its sign-in state too**, including the browser
profile created for it.

**Workspace trust.** In a folder you have not trusted, the extension will not run
a `python.exe` found in that folder's `.venv` / `venv`. It uses the interpreter
from your settings or from `PATH` instead.

## Limitations

**The endpoints this reads are undocumented.** None of the consumer providers
publish an API for "how much of my plan have I used"; these routes were found by
watching what the web apps themselves do. Providers can change or remove them
without notice, and when that happens the affected provider stops reporting
until the extension is updated. When a response no longer matches the shape it
expects, the extension reports an error rather than claiming 0% — telling you
that you have plenty of headroom left when it no longer knows is the one failure
mode worth ruling out.

**Google refusing to sign in from an embedded browser is why there is no
embedded browser.** Gemini was never able to use one, and the workaround for it
— copy the cookie out of your normal browser's developer tools — turned out to
be the better path for every provider. It is now the only one.

**Azure OpenAI means an organization-internal gateway**, not Azure Cost
Management. It reads a `/bill/billing` endpoint, and the endpoint host must sit
within that organization's domain — the API key is not sent anywhere else. If
you do not have such a gateway, this provider is not usable for you.

**Antigravity reports the G1 credit balance only.** The quota endpoint answers
403 for the OAuth client available here, so quota percentages are not shown.

**Anthropic API cost needs an Admin API key** (`sk-ant-admin01-…`), which
requires an organization; personal accounts cannot issue one. Figures are daily
buckets and exclude Priority Tier costs, so they read slightly below the invoice.

**Codex CLI reads local files only.** It parses the CLI's own session records,
so numbers appear only after that CLI has actually made model calls.

**An older desktop build can drop the order you arranged by hand.** That order
is stored as `settings.account_order` in the `config.json` this extension
shares with `AI-UsageManager.exe`, and a desktop build older than this release
does not know the key, so it writes the file back without it. Only the order is
lost — accounts, credentials and every other setting are untouched, and
rearranging the list restores it.

## License

MIT. See the `LICENSE` file at the root of the repository.

## Repository

<https://github.com/YutaHassy/AI-UsageManager>

Issues and pull requests are welcome there.

---

## 日本語

Claude / ChatGPT / Gemini / Azure OpenAI などの**利用枠の消費率と課金額**を、
VS Code の中で確認します。取得は利用者自身のログイン状態や API キーで行うため、
推定値ではなく実際の数字が出ます。表示言語は英語・日本語・韓国語・簡体字中国語に
対応しています。

### 取得できるもの

| 取得先 | 見えるもの | 認証 |
| --- | --- | --- |
| Claude.ai | Pro / Max の枠 (5時間・週間・週間 Opus・週間 Sonnet) | ブラウザから取った `sessionKey` |
| ChatGPT | プランの枠 (5時間 / 日次 / 週間) と追加クレジット | ブラウザから取った Cookie または accessToken |
| Gemini | アプリの枠 (現在の使用量と週間上限) | ブラウザから取った Cookie |
| Codex CLI | レート上限 (5時間 / 日次 / 週間 / 月次) | 不要。CLI 自身の記録ファイルを読むだけ |
| Antigravity (Gemini) | G1 クレジット残高 | gemini-cli のログイン状態を利用 |
| Anthropic API | 今月の課金額 (USD) をモデル別に | Admin API キー |
| Azure OpenAI | ゲートウェイの累計課金額 (JPY) をモデル別に | ゲートウェイの API キー (後述) |

課金額の2つには上限金額を設定でき、設定すると消費率として枠と並べて表示されます。
アカウントごとの有効 / 無効の切り替えもできます。

### 必要なもの

**Python 3.9 以降**と `requests` / `urllib3`:

```sh
pip install requests urllib3
```

**これで全部です。** 表示・更新はもちろん、アカウントの追加も編集も、他に要る
ものはありません。どちらかが入っていなければ、どの Python を試したかと、
**その Python 用の** `pip install` の1行が出ます。

### 資格情報の取り方

追加も編集も、**使用状況のタブの中の1画面**で行います。取得先・名前・
資格情報・上限金額が一度に見え、別のウィンドウは出ません。

資格情報そのものは、**普段お使いのブラウザ**から取ってきます。

1. 「アカウントを追加」を押します。最初に取得先を選ぶと、以降の欄がその
   取得先に合わせて入れ替わります。
2. その取得先の取り方の手順と、**既定のブラウザでそのページを開く**ボタンが
   出ます。普段のブラウザなら、たいていは既にログイン済みです。
3. 手順どおりにコピーして、フォームの入力欄に貼り付けます。
4. 保存します。

**丸ごと貼ってください。** Cookie を使う取得先では、手順の最後が
「Copy as cURL」になっています。クリップボードには長いコマンドが入りますが、
必要な部分だけを拡張が取り出し、残りは捨てます。**どこが Cookie かを自分で
探す必要はありません。** 違う行を選んでしまっても、保存すれば何が足りないかを
教えます。

以前の版は、拡張に同梱した Chromium の画面でログインさせていました
(`PySide6` がここに並んでいたのはそのためです)。**その画面は無くなりました。**
理由は2つあります。API キーを貼るだけの人にまでブラウザエンジンの導入を
強いていたこと。そして、埋め込みブラウザからのログインを拒む取得先がある
こと — それは埋め込んだ側がパスワード入力を覗けるという理由で存在する保護
なので、迂回しようとするのは筋が悪い。**普段お使いのブラウザで取ってくる**
ほうが正しく、そちらは大抵ログイン済みでもあります。

**1つだけ失われたものがあります。** Claude のセッションは、以前はその画面の
プロファイルを使って裏で黙って更新していました。今後は期限切れがそのまま
通知され、新しい資格情報を貼り直すことになります。ChatGPT と Gemini は
取得先の側で延長されるため、影響はありません。

### Python の探し方

設定が空欄なら、次の順に自動で探します。

1. 設定 `aiUsageManager.pythonPath`
2. Python 拡張 (`ms-python.python`) が選んでいるインタープリタ
3. ワークスペース直下の `.venv` / `venv` (**信頼しているフォルダのみ**。信頼して
   いないフォルダが持ち込んだ実行ファイルを黙って動かさないため)
4. PATH 上の `py -3` (Windows) / `python3`

1本目に `requests` が無ければ、Python 拡張の答えを待って一度だけ引き直します
(conda や pyenv の環境はこれで拾えます)。それでも外れる場合は
`aiUsageManager.pythonPath` に実行ファイルのパスを入れてください。

### 開き方

アクティビティバーのゲージのアイコンを押すと、**エディタタブで**開きます。横に
広い表なので、細いサイドバーに押し込むと枠の名前とバーが潰れて読めなくなるため
です。ステータスバーの表示をクリックしても同じ画面が開きます。

開く場所は**アクティブなエディタの隣のグループ**です。見るために開いたファイルを
覆わないようにするためで、1.8.0 より前と同じく同じグループに開きたいときは
`aiUsageManager.openLocation` を `active` にしてください。どちらの設定でも、
**自分で動かしたタブはそのまま**です — 決まるのは新しく開くときの場所だけです。

### コマンド

コマンドパレットでは、すべて `AI-UsageManager` のカテゴリに入っています。

| コマンド | 内容 |
| --- | --- |
| `Open Usage` | 使用状況の画面を開きます |
| `Refresh All` | 取得できるアカウントをすべて更新します |
| `Add Account` | 新しいアカウントのフォームを開きます |
| `Edit Account` | 登録済みアカウントのフォームを開きます |
| `Sign In Again` | 同じフォーム。期限切れの資格情報を貼り直すときの名前です |
| `Delete Account` | アカウントと保存されたログイン状態を消します |
| `Open Settings` | この拡張の設定だけに絞って設定画面を開きます |
| `Open Settings File (config.json)` | バックエンドの設定ファイルをエディタで開きます。そこにしか無い項目 (`aoai_allowed_hosts`) を直すためのものです |
| `Show Log` | バックエンドのログを出力チャンネルに表示します |
| `Restart Backend` | Python プロセスを作り直します |
| `Language` | 表示言語を選びます |
| `Sort Accounts` | アカウント一覧の並び順を選びます |
| `Zoom In` | 使用状況の画面を1段だけ拡大します |
| `Zoom Out` | 使用状況の画面を1段だけ縮小します |
| `Set Proxy Password` | プロキシのパスワードを設定・消去します (暗号化して保存され、settings.json には書かれません) |
| `Test Proxy Connection` | いまのプロキシ設定で通信できるか試します |
| `Import Proxy From Environment Variables` | `HTTP_PROXY` / `HTTPS_PROXY` からホストとポートの設定を埋めます |

追加・編集・削除・再ログインは、画面のボタンからも実行できます。

### 大きさと並び順

この表は、作業中のタブの隣に開いて読むものなので、**大きさも並び順も
変えられます。** どちらも保存され、次に開いたときもそのままです。

| やりたいこと | 操作 |
| --- | --- |
| 拡大 / 縮小 | ツールバーの `−` / `＋`、表の上での `Ctrl+ホイール`、タブが前面のときの `Ctrl+ +` / `Ctrl+ -` |
| 等倍に戻す | 2つのボタンのあいだの `100%` を押します |
| 行を1つ動かす | 行の右端の `≡` をドラッグします。マウスを使わないなら、行に焦点を当てて `Alt+↑` / `Alt+↓` |
| 基準で並べる | ツールバーの `⇅`: 使用率が高い順 / 名前順 / 取得先順 / 追加した順 / 手で並べた順のまま |

**文字だけでなく、バーも余白も固定幅の列も一緒に拡大縮小します。** この表は
行をまたいでバーの長さを見比べて読むものなので、文字だけが大きくなって列が
付いてこないと、そもそも読み方が成り立たなくなります。

**基準を選んでも、手で並べた順は捨てられません。** `config.json` に残っている
ので、いつでも戻せます。基準で並べている最中にドラッグすると「手で並べた順の
まま」へ切り替わり、**そのとき画面に出ていた並びに、動かした1件を落とした位置
のまま**残します。

`Ctrl+ +` / `Ctrl+ -` は、使用状況のタブが前面のあいだだけ VS Code 本体の
ウィンドウ拡大縮小から借りています (他のタブへ移れば元どおりです)。拡大縮小と
並べ替えの動作確認は VS Code 1.135 で行いました。導入できるのは 1.85 以降です
が、大きく古い版では見た目が想定と違うことがあります。

### 設定

| 設定 | 既定 | 内容 |
| --- | --- | --- |
| `aiUsageManager.pythonPath` | `""` | バックエンドを動かす Python。空欄で自動検出 |
| `aiUsageManager.language` | `auto` | 表示言語 (`auto` / `en` / `ja` / `ko` / `zh-cn`) |
| `aiUsageManager.openLocation` | `beside` | 使用状況のタブを新しく開く場所。`beside` (アクティブなエディタの隣のグループ) / `active` (同じグループ) |
| `aiUsageManager.zoomLevel` | `100` | 使用状況の画面の拡大率 (%)。`50`〜`200`。ツールバーのボタン・`Ctrl+ホイール`・`Ctrl+ +/-` がここへ書きます |
| `aiUsageManager.accountSort` | `manual` | アカウント一覧の並び順 (`manual` / `usage` / `name` / `provider` / `added`) |
| `aiUsageManager.autoRefreshMinutes` | `1` | 自動更新の間隔 (分)。`0` / `1` / `5` / `10` / `30` / `60`。`0` で無効 |
| `aiUsageManager.refreshOnOpen` | `true` | 画面を開いたときに1回更新する |
| `aiUsageManager.showStatusBar` | `true` | ステータスバーに最逼迫アカウントを出す |
| `aiUsageManager.proxy.mode` | `system` | ネットワークへの接続方法 (`system` / `manual` / `none`) |
| `aiUsageManager.proxy.host` | `""` | プロキシのホスト名。`proxy.mode` が `manual` のとき使用 |
| `aiUsageManager.proxy.port` | `8080` | プロキシのポート。`proxy.mode` が `manual` のとき使用 |
| `aiUsageManager.proxy.username` | `""` | プロキシのユーザーID。パスワードには設定キーが無く、`Set Proxy Password` コマンドから設定します |

`aiUsageManager.language` が効くのは、この拡張の画面とバックエンドのメッセージ
です。**コマンドパレットのコマンド名と、設定項目の説明文そのものは、VS Code 自身の
表示言語に従います。** これらは VS Code が解決するもので、拡張からは差し替えられ
ません。

**プロキシのパスワードは `settings.json` には書かれません。** このファイルは
平文で、Settings Sync により他端末にも複製され得ます。パスワードは
`Set Proxy Password` コマンド経由で渡され、バックエンドが Windows DPAPI で
暗号化してから自分の設定ファイルに保存します。

### 資格情報の扱い

利用状況を読むには資格情報が要るので、この拡張はそれを保存します。何が起きるかを
そのまま書きます。

**保存場所** — アカウント・Cookie・API キーは1つのファイルに入ります。

- Windows: `%LOCALAPPDATA%\AI-UsageManager\config.json`
- それ以外: `$XDG_CONFIG_HOME/AI-UsageManager/config.json`
  (無ければ `~/.config/AI-UsageManager/config.json`)

プロジェクトのフォルダの外に置いているのは、フォルダごと同期・圧縮・共有された
ときに、生きたセッション Cookie が一緒に出ていかないようにするためです。

**保護のしかた** — Windows では Cookie と API キーを DPAPI で暗号化します。DPAPI
で保護したデータは「同じ Windows ユーザー・同じマシン」でしか復号できないため、
`config.json` をコピーされてもそのままでは使えません。**DPAPI が使えない環境や、
暗号化に失敗した場合は平文で保存されます。** その場合は画面の下部に
`⚠ Credentials are stored unencrypted` と表示されます。

**送信先** — 資格情報は、そのアカウントの取得先へだけ送られます。このプロジェクト
が持つサーバーはありません。テレメトリも解析も送っていません。

**Webview には渡していません。** 画面に渡すのは「設定済みかどうか」だけです。
Webview は DevTools で中身を覗ける実行環境なので、そこへ Cookie を置く理由が
ありません。

**アカウントを削除すると、保存されたログイン状態も一緒に消えます** (そのアカウント
用に作られたブラウザプロファイルを含みます)。

**信頼していないフォルダでは、そのフォルダの `.venv` / `venv` の Python を使いま
せん。** 設定または PATH の Python を使います。

### 制約

**読んでいるのは公開されていないエンドポイントです。** 「自分のプランをどれだけ
使ったか」を返す API を公開している提供元は無く、ここでの経路は Web アプリ自身の
通信を確認して判明したものです。提供元は予告なくこれを変更できます。変わった
場合、その取得先は拡張が更新されるまで数字を出せなくなります。**形式を認識でき
なくなったときは、0% と偽らずエラーとして出します** — 余裕があると誤解させるのが
最も避けたい壊れ方だからです。

**Google が埋め込みブラウザからのサインインを拒否することが、埋め込み
ブラウザを持たない理由です。** Gemini では元から使えず、その回避策 —
普段のブラウザの開発者ツールから Cookie を取り出す — が、結果としてどの
取得先にとってもよい道でした。いまはそれが唯一の道です。

**Azure OpenAI は社内ゲートウェイ向けです** (Azure Cost Management ではありません)。
`/bill/billing` を読む作りで、エンドポイントのホストはその組織のドメイン内である
必要があります (API キーがそれ以外へ出ないようにするためです)。該当する
ゲートウェイが無い場合、この取得先は使えません。

**Antigravity は G1 クレジット残高だけです。** クォータのエンドポイントは、ここで
使える OAuth クライアントでは 403 を返すため、使用率は出せません。

**Anthropic API の課金額には Admin API キー** (`sk-ant-admin01-…`) が要ります。
組織が必要で、個人アカウントでは発行できません。値は日次バケットで、Priority Tier
の分は含まれないため、実際の請求よりわずかに少なく出ます。

**Codex CLI はローカルのファイルを読むだけです。** CLI 自身のセッション記録を
解析するので、その CLI がモデルを呼んだ後でないと数字は出ません。

**手で並べた順は、古いデスクトップ版に消されることがあります。** 並び順は
`config.json` の `settings.account_order` に入りますが、このファイルは
`AI-UsageManager.exe` と共有しています。このキーを知らない古いデスクトップ版が
設定を保存すると、そのキーごと落ちます。**失われるのは並び順だけで**、
アカウントも資格情報も他の設定も無事です。並べ直せば元に戻ります。

### ライセンス

MIT。リポジトリ直下の `LICENSE` を参照してください。

### リポジトリ

<https://github.com/YutaHassy/AI-UsageManager>
