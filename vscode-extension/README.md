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
| Claude.ai | Pro / Max quotas: 5-hour, weekly, weekly Opus, weekly Sonnet | Sign-in window, or paste a `sessionKey` cookie |
| ChatGPT | Plan quota windows (5-hour / daily / weekly) and add-on credits | Sign-in window, or paste a cookie |
| Gemini | App quotas: current usage and the weekly limit | Cookie pasted by hand — see [Limitations](#limitations) |
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

That is all the display side needs. If either package is missing, the extension
tells you which Python it tried and gives you a ready-to-run `pip install` line
for exactly that interpreter.

### To add, edit or sign in again

Additionally **PySide6**:

```sh
pip install PySide6 PySide6-Addons
```

These two are only used when a sign-in window is opened. Viewing, refreshing
and **deleting an account** never touch them.

If you would rather not put PySide6 into the same environment as `requests`,
point `aiUsageManager.guiPythonPath` at a second interpreter that has it.

### About the sign-in window

Signing in happens in **a separate window**, outside VS Code — a Chromium-based
browser window driven by PySide6. A webview inside VS Code cannot collect
another site's cookies, so the extension launches the same sign-in window the
desktop build uses instead of pretending to do it in-editor.

When you press a button that needs it, VS Code shows a progress notification
telling you to continue in the other window. **If the window never appears, the
× on that notification cancels.** Cancelling terminates the sign-in process on
the backend, so you can carry on without reloading the window.

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

Clicking the status bar item opens the same tab.

## Commands

All commands are under the `AI-UsageManager` category in the Command Palette.

| Command | What it does |
| --- | --- |
| `Open Usage` | Opens the usage tab (so does clicking the status bar) |
| `Refresh All` | Refreshes every account that can be refreshed |
| `Add Account` | Opens the sign-in window and registers a new account |
| `Edit Account` | Changes a registered account's details |
| `Sign In Again` | Opens the sign-in window and takes fresh cookies |
| `Delete Account` | Deletes the account and its saved sign-in state |
| `Open Settings` | Opens the Settings UI filtered to this extension |
| `Show Log` | Shows the backend log in an output channel |
| `Restart Backend` | Recreates the Python process |
| `Language` | Picks the display language |

Add, edit, delete and sign-in-again are also available as buttons in the view.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `aiUsageManager.pythonPath` | `""` | The Python that runs the backend. Empty means auto-detect |
| `aiUsageManager.guiPythonPath` | `""` | The Python that runs the sign-in window (needs PySide6). Empty means the same one as above |
| `aiUsageManager.language` | `auto` | Display language: `auto`, `en`, `ja`, `ko`, `zh-cn` |
| `aiUsageManager.autoRefreshMinutes` | `0` | Refresh interval in minutes (`0`, `1`, `5`, `10`, `30`, `60`). `0` disables it |
| `aiUsageManager.refreshOnOpen` | `true` | Refresh once when the view is opened |
| `aiUsageManager.showStatusBar` | `true` | Show the account closest to its limit in the status bar |

`aiUsageManager.language` covers the extension's own views and the backend's
messages. Command names in the Command Palette and the description text of the
settings themselves always follow VS Code's display language — VS Code resolves
those, and an extension cannot override them.

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

**Gemini needs its cookie pasted by hand.** Google refuses sign-in from embedded
browsers, so the built-in sign-in window cannot be used for it. The dialog walks
you through copying the cookie out of your normal browser's developer tools
instead.

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
| Claude.ai | Pro / Max の枠 (5時間・週間・週間 Opus・週間 Sonnet) | ログイン画面、または `sessionKey` の貼り付け |
| ChatGPT | プランの枠 (5時間 / 日次 / 週間) と追加クレジット | ログイン画面、または Cookie の貼り付け |
| Gemini | アプリの枠 (現在の使用量と週間上限) | Cookie の手貼り (後述) |
| Codex CLI | レート上限 (5時間 / 日次 / 週間 / 月次) | 不要。CLI 自身の記録ファイルを読むだけ |
| Antigravity (Gemini) | G1 クレジット残高 | gemini-cli のログイン状態を利用 |
| Anthropic API | 今月の課金額 (USD) をモデル別に | Admin API キー |
| Azure OpenAI | ゲートウェイの累計課金額 (JPY) をモデル別に | ゲートウェイの API キー (後述) |

課金額の2つには上限金額を設定でき、設定すると消費率として枠と並べて表示されます。
アカウントごとの有効 / 無効の切り替えもできます。

### 必要なもの

表示と更新だけなら **Python 3.9 以降**と `requests` / `urllib3`:

```sh
pip install requests urllib3
```

アカウントの追加・編集・再ログインを行う場合は、加えて **PySide6**:

```sh
pip install PySide6 PySide6-Addons
```

この2つはログイン画面を開いたときにだけ使われます。表示・更新と**アカウントの
削除**では使いません。`requests` の環境と分けたい場合は、
`aiUsageManager.guiPythonPath` に PySide6 入りの Python を指定してください。

### ログイン画面について

ログインは **VS Code とは別のウィンドウ** (PySide6 の Chromium ベースの画面) で
行います。Webview の中では他社サイトの Cookie を回収できないためです。

**別ウィンドウが出てこないときは、進行中の通知の × で中止できます。** 中止すると
バックエンドがログイン画面のプロセスを終了させるので、VS Code を再読み込みせずに
次の操作へ進めます。

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

### コマンド

コマンドパレットでは、すべて `AI-UsageManager` のカテゴリに入っています。

| コマンド | 内容 |
| --- | --- |
| `Open Usage` | 使用状況の画面を開きます |
| `Refresh All` | 取得できるアカウントをすべて更新します |
| `Add Account` | ログイン画面を開いて新しいアカウントを登録します |
| `Edit Account` | 登録内容を変更します |
| `Sign In Again` | ログイン画面を開いて Cookie を取り直します |
| `Delete Account` | アカウントと保存されたログイン状態を消します |
| `Open Settings` | この拡張の設定だけに絞って設定画面を開きます |
| `Show Log` | バックエンドのログを出力チャンネルに表示します |
| `Restart Backend` | Python プロセスを作り直します |
| `Language` | 表示言語を選びます |

追加・編集・削除・再ログインは、画面のボタンからも実行できます。

### 設定

| 設定 | 既定 | 内容 |
| --- | --- | --- |
| `aiUsageManager.pythonPath` | `""` | バックエンドを動かす Python。空欄で自動検出 |
| `aiUsageManager.guiPythonPath` | `""` | ログイン画面 (PySide6) を動かす Python。空欄なら上と同じ |
| `aiUsageManager.language` | `auto` | 表示言語 (`auto` / `en` / `ja` / `ko` / `zh-cn`) |
| `aiUsageManager.autoRefreshMinutes` | `0` | 自動更新の間隔 (分)。`0` / `1` / `5` / `10` / `30` / `60`。`0` で無効 |
| `aiUsageManager.refreshOnOpen` | `true` | 画面を開いたときに1回更新する |
| `aiUsageManager.showStatusBar` | `true` | ステータスバーに最逼迫アカウントを出す |

`aiUsageManager.language` が効くのは、この拡張の画面とバックエンドのメッセージ
です。**コマンドパレットのコマンド名と、設定項目の説明文そのものは、VS Code 自身の
表示言語に従います。** これらは VS Code が解決するもので、拡張からは差し替えられ
ません。

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

**Gemini は Cookie の手貼りが必要です。** Google が埋め込みブラウザからのサイン
インを拒否するため、内蔵のログイン画面は使えません。代わりに、普段のブラウザの
開発者ツールから Cookie を取り出す手順が画面に出ます。

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

### ライセンス

MIT。リポジトリ直下の `LICENSE` を参照してください。

### リポジトリ

<https://github.com/YutaHassy/AI-UsageManager>
