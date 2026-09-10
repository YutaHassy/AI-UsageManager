# Changelog

All notable changes to the AI-UsageManager extension are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.2] - 2026-09-10

The same 403 as last time, from a different cause. 1.8.1 fixed a header that
could invite Cloudflare's bot check. This one is about what we were putting in
the `Cookie` header in the first place.

### Fixed

- **A cookie pasted as "Copy as cURL (cmd)" could be stored as the whole curl
  command, and the command was then sent as the `Cookie` header.** claude.ai
  sets an Intercom cookie whose value is JSON, so the cmd format escapes the
  quotes inside it as `^\^"`. `extract_cookie_header()` read the `-b` argument
  with a lazy `.*?`, which stopped at that first quote and returned a fragment
  — one that no longer contained `sessionKey`. `normalize_credential()` saw no
  session cookie in the fragment and fell back to storing the pasted text
  verbatim, so `curl --url ^"https://claude.ai/...` ended up in the cookie jar
  as a cookie name. Cloudflare answered the resulting header with
  `cf-mitigated: challenge` and a 403.

  **The cookie was never the problem, which is why re-pasting it did not
  help** — the same paste produced the same broken value every time. The cmd
  escaping is now removed before the arguments are read, and quoted arguments
  are read across escaped quotes rather than stopping at the first one.

- **A paste that could not be reduced to a cookie is now reported instead of
  saved silently.** Splitting a curl command on `;` turns up a stray
  ` sessionKey=...` fragment, so the check that looked for the session cookie
  first found one and raised nothing. The "no cookie could be taken out of what
  you pasted" warning is now decided before that check.

- **Fragments that cannot be cookie names are dropped on the way into the
  cookie jar.** One filter in `parse_cookie_header()` keeps a failed extraction
  anywhere in the codebase from reaching a provider as a `Cookie` header.

Gemini pastes go through the same extraction and get the same fix.

## [1.8.1] - 2026-09-09

A small one: Claude.ai accounts could be turned away by Cloudflare's bot
check, not by an expired cookie.

### Fixed

- **Claude.ai requests could be blocked as a bot (403, `cf-mitigated`) even
  with a fresh cookie.** The `Sec-Ch-Ua` header on Claude's requests still
  claimed Chromium 120 while the `User-Agent` sent alongside it said Chrome
  140 — a mismatch a real browser never produces, and one Cloudflare's
  mitigation can key on. `chatgpt.py` had already been kept in step with the
  shared `User-Agent` version; `claude.py` had not. The versions now match.

## [1.8.0] - 2026-08-30

The usage table decided two things on your behalf that it had no business
deciding: how big it was, and what order the accounts came in. Neither mattered
much with three accounts on a full-width tab. With a dozen accounts in a split,
both do — the bars are too small to read at a glance, and the account you
actually watch sits wherever you happened to register it.

**This release hands both back.** The tab zooms between 50% and 200%, and the
list can be dragged into whatever order you want, or sorted by one of four
criteria. Both choices are remembered and are there again the next time you
open it.

**The tab also opens beside your work now, rather than on top of it.** That is
a change to existing behaviour and the one thing here you will see without
having asked for it; `aiUsageManager.openLocation` puts the old placement back.

### Added

- **Zoom, from 50% to 200%.** `−`, `100%` and `＋` are in the toolbar,
  `Ctrl+Mouse wheel` works anywhere over the table, and `Ctrl+ +` / `Ctrl+ -`
  work while the tab is focused. Clicking the `100%` label returns to actual
  size.

  **The whole view scales, not only the text** — gauges, padding, the
  fixed-width metric columns and the rounded corners move together. This table
  is read by comparing bar lengths from row to row and from account to
  account, and a font that grows while its column does not is a table that no
  longer lines up. Scaling the layout itself is also all-or-nothing, which
  rules out the other failure available here: one hard-coded size missed among
  dozens, visible only once someone goes to 200%.

  The level is saved in `aiUsageManager.zoomLevel`, so a tab you shrank to fit
  a narrow split opens at that size next time. The row you were reading stays
  on screen while the level changes, for the same reason the view already
  refuses to jump back to the top when it refreshes.

- **Manual ordering, by dragging or from the keyboard.** Every row in the
  summary has a handle (`≡`) at its right end; drag it and the row lands where
  you dropped it. The list scrolls by itself when you reach its top or bottom
  edge, because a tab in a split at 200% shows fewer rows than there are
  accounts, and a handle you can pick up but cannot carry anywhere is worse
  than no handle. Without a mouse: focus a row and press `Alt+Up` or
  `Alt+Down`. The row keeps focus, so it can be pressed again immediately.
  Plain `Enter`, `Space` and the arrow keys behave exactly as before — only
  `Alt` is new.

  **The order lives with the accounts, not with the window.** It is written to
  `settings.account_order` in the same `config.json` the accounts are in, as a
  list of account ids. **The `accounts` array itself is never reordered:** its
  order *is* the record of what was registered when, and overwriting it would
  leave "in the order they were added" with nothing to sort by.

  While you are holding a row, the automatic refresh stops redrawing the list —
  it would otherwise reorder rows and change their heights under your pointer,
  and the drop would land somewhere you did not aim at. The results themselves
  are not discarded, only the redraw, and the view catches up the moment you
  let go.

- **Five ways to order the list.** `⇅` in the toolbar — or the
  `Sort Accounts` command — offers highest usage first, by name, by provider,
  in the order they were added, and "keep the order you arranged by hand". The
  choice is saved in `aiUsageManager.accountSort`, and `⇅` is highlighted
  whenever it is anything other than the manual order, so "why is this account
  not where I put it" is answerable without hovering over anything.

  **Choosing a criterion does not discard your manual order.** It stays in
  `config.json` and comes back intact when you switch back to it. Dragging a
  row while a criterion is active switches to the manual order and keeps the
  arrangement you were looking at, with the moved row in its new place — the
  alternatives are a handle that visibly does nothing, or a move that the next
  automatic refresh silently undoes.

  "Highest usage first" sorts on the number the row is showing, which includes
  the previous value while a refresh is in flight. Sorting on the live value
  would drop every account to "no number yet" the instant a refresh starts and
  reshuffle the entire list once a minute, which is the default refresh
  interval.

### Changed

- **The usage tab opens in a group beside the active editor, instead of in the
  same group.** It is a wide table, and opening it in the same group meant it
  covered the file you opened it to look at — after which the way to see both
  was to drag it into a split by hand, every time.

  **Set `aiUsageManager.openLocation` to `active` to get the previous
  behaviour back.** This decides where a *new* tab appears and nothing else: a
  tab you have already moved somewhere stays where you put it, and reopening
  an existing tab still never moves it. The direction of "beside" is VS Code's
  own `workbench.editor.openSideBySideDirection`, so it lands below rather
  than to the right for anyone who has set that to `down`.

- **`config.json` gains one key: `settings.account_order`.** It holds the
  account ids in the order you arranged them. It is matched against the
  accounts that actually exist every time the list is drawn, so it does not
  have to be complete or current — an id left behind by a deleted account is
  ignored, and a newly added account goes to the end. Nothing rewrites the key
  to tidy it up, which does mean that ids of deleted accounts stay visible if
  you open the file with `Open Settings File (config.json)`. Rewriting the
  settings file on every redraw would be the worse trade.

### Known limitations

- **An older desktop build can silently drop the order you arranged.**
  `config.json` is shared with `AI-UsageManager.exe`, and that app saves the
  settings it knows about; one built before this release does not know
  `settings.account_order` and writes the file back without it. **Only the
  order is lost** — the accounts, their credentials and every other setting
  are untouched, and rearranging the list restores it.

- **Zoom and drag-to-reorder were verified on VS Code 1.135.** The extension
  still declares `^1.85.0` and installs on anything from that version up,
  because raising the floor would lock people out of an extension that works
  for them otherwise. On a much older VS Code the scaled layout and the drop
  targets may not behave the way they do here; the numbers stay correct
  either way.

- **`Ctrl+ +` and `Ctrl+ -` are taken over from VS Code's own window zoom**
  while the usage tab is focused, by a keybinding this extension contributes.
  Move to another tab and they zoom the window again, as before. Keys are
  resolved by VS Code and the last rule wins, so a personal keybinding or
  another extension claiming the same combination can leave them zooming the
  window instead. The toolbar buttons and `Ctrl+Mouse wheel` are unaffected
  and always work.

- **Editing `aiUsageManager.zoomLevel` in the Settings UI takes effect the
  next time the tab is opened**, not immediately. The zoom level is owned by
  the view while the view is open — pushing the setting in would fight the
  wheel, which writes the setting a moment after you stop turning it.
  `zoomLevel` is also written globally, so a workspace-level value of it wins
  and the level will not appear to stick; this is the same behaviour
  `aiUsageManager.language` has always had.

## [1.7.0] - 2026-08-21

The last three releases each removed one more reason to install PySide6. This
one removes the reason itself. **The built-in sign-in browser is gone**, and
with it the last thing in the extension that needed a browser engine. Adding
and editing an account now happen on one screen inside the usage tab, and the
credential comes from the browser you already use.

Two things forced this. The window made anyone who only wanted to paste an API
key install QtWebEngine — which is what 1.5.0, 1.5.1 and 1.6.0 were each
chipping away at, one operation at a time. And some providers refuse to sign in
from an embedded browser at all: Gemini never could, because Google blocks it,
and that block exists precisely because whoever embeds the browser can watch you
type your password. The workaround built for Gemini — copy the cookie out of
your normal browser's developer tools — turned out to be the better path for
every provider. It is now the only one.

**Installing the vsix is now the whole installation.** A machine with Python and
`requests` can register its first account without installing anything else.

### Added

- **A form for adding and editing, inside the usage tab.** Provider, name,
  credential, spending cap and enabled/disabled are visible together and change
  as a unit. The previous release did this with one-question-at-a-time pickers,
  which needed no window but showed you only the question in front of you —
  and these fields depend on each other, since the provider decides what the
  credential even means.

  Choosing a provider rewrites the rest of the form: its labels, its example
  text, whether there is a spending cap at all, and the numbered steps for
  getting its credential. A button next to those steps opens the provider's
  page **in your default browser**, which is usually signed in already.

- **Claude can be registered from a normal browser.** It had a manual path in
  the code — `extract_cookie_header` has always understood "Copy as cURL"
  output, and the validator already said "filter on `organizations` and copy it
  again" — but nothing in the interface offered it. The steps existed; the
  doorway did not. Anthropic API gained a path to its Admin keys page for the
  same reason.

### Removed

- **The sign-in window, and every route to it.** `add_account`, `relogin` and
  `cancel_gui` are gone from the backend protocol, along with the helper
  process, the lock that serialised window operations, and the progress
  notification that asked you to continue in the other window. `gui_helper.py`
  is still in the repository but nothing can reach it: the extension has no
  code that starts it, it is not shipped in the vsix, and it cannot run from
  where it sits (`ui/` is no longer copied next to it). Its header says so.

- **`aiUsageManager.guiPythonPath`.** It existed to point at a second
  interpreter that had PySide6 in it. **If you had it set, the extension
  removes it from your settings file on the first launch after updating**, and
  says so once — a setting dropped from the manifest stays in `settings.json`
  and is flagged as unknown forever otherwise, and whoever removed it is the
  one who should clean up after it. Only settings named in an explicit list
  are touched, and a settings file that cannot be written is logged and left
  alone.

- **Silent renewal of Claude sessions.** This is a real loss and the only one.
  An expiring Claude cookie used to be refreshed in the background using the
  embedded browser's profile; that profile went with the window. An expired
  Claude session is now reported to you and you paste a fresh credential.
  ChatGPT and Gemini are unaffected — those are renewed by the provider.

### Fixed

Found by reviewing the new form before shipping it, not by using it:

- **Changing an account's provider carried its spending cap across.** The
  backend has always reset the cap when the provider changes, because the
  currency changes with it — but that reset only applied when the request
  omitted the cap, and the form always sent it. Editing an Azure OpenAI account
  (JPY) into an Anthropic API account (USD) would have kept the number and
  changed its meaning by two orders of magnitude. **Nothing validates a
  spending cap**, so nothing would have said a word. The form now swaps the
  cap, the extra field and the credential box to the new provider's defaults.

- **Changing the provider kept the old credential.** A Claude cookie is not an
  Azure API key. Keeping it produced an account that reads as "credential set"
  in the list and fails every fetch. Switching providers now requires a fresh
  credential, the same rule the extra field and the spending cap already
  followed.

- **A spending cap of `1,000` was silently saved as no cap at all.** An
  `<input type="number">` reports an empty value when what is typed is not a
  valid number, so thousands separators and full-width digits arrived as `0` —
  and `0` means "no cap", so the gauge quietly disappeared. The backend has a
  "the spending cap must be a number" message that could never be reached. The
  field is now plain text and the backend does the judging.

- **The save button did nothing, silently, if the provider could not be
  resolved.** An account whose provider is not in the list (a hand-edited
  `config.json`) produced a form that submitted nothing and reported nothing.
  A button that does nothing is the worst failure available. It now submits and
  lets the backend answer "unknown provider".

- **"Save anyway" could be pressed twice and create two accounts.** It was left
  out of the set of controls disabled while a save is in flight.

- **A malformed payload froze the whole view.** Iterating a `providers` that
  was not an array threw out of the message handler, after which no further
  state was ever rendered.

- **Opening the form dragged the usage tab to the leftmost editor group.**
  Revealing an existing panel passed it a column — "the active text editor's
  column, or 1" — and **a webview is not a text editor**, so while you were
  looking at this panel that expression was always 1. Pressing Add or Edit
  therefore moved the tab, every time, for anyone who keeps it in a split on
  the right. Revealing without a column leaves it where it is.

- **The credential stayed in memory after the form closed.** The form's DOM is
  kept between openings; the box is now cleared on the way out. Webviews are
  inspectable with DevTools.

- Accessibility of the new form: hints are tied to their fields with
  `aria-describedby`, the error area announces itself with `role="alert"`, and
  Esc cancels.

### Changed

- **"Sign In Again" opens the edit form.** The command and the button stay,
  because a Claude session that has expired is exactly when someone needs to be
  shown where to paste — but there is no window behind it any more.
- Every provider whose credential comes out of a browser now carries its own
  instructions for getting it, and a test asserts it. When the browser is the
  only path, a provider without instructions is a provider nobody can register.
  Azure OpenAI is the exception and stays one: the gateway is something each
  organization runs, so no URL would be right for everyone. It says who issues
  the key instead.
- The catalogue check now covers the new strings; 21 were added and 61 that
  described the removed window were dropped, in all three languages. Four of
  those additions were found by a review pass, not by the check: the tooltips
  on "＋ Add", "Edit" and "🔑 Sign in again", and the note shown when a
  credential expires, were all still promising a window that no longer opens.
  **They passed every check** — the strings were present, translated in all
  three languages, and their placeholders matched. Being present and being
  true are different things.

- **The build now checks the webview's two-way protocol.** The message types
  `media/main.js` sends and the ones `panel.js` handles were kept in step by
  hand. A missing handler makes a button that does nothing, silently; a
  handler nobody sends becomes unreachable code that reads as live. Removing
  the sign-in window left three of the latter behind, which is what prompted
  the check.

- `gui_helper.py` is no longer scanned for translatable strings, which is what
  had been keeping guidance for the removed `guiPythonPath` setting alive in
  all three catalogues.

## [1.6.0] - 2026-08-20

Editing had been asking for PySide6 for no reason, and the fix for that (1.5.0)
turned out to have a defect of its own (1.5.1). Both were instances of a kind,
so this release is the result of looking for the rest of the kind. Three
patterns came out of it: **an operation routed through a window it does not
need**, **advice that cannot be followed**, and **things that pile up where no
check is looking**. Each was found somewhere else in the codebase.

### Added

- **Adding an account no longer needs PySide6 either.** This was the third
  instance of the same mistake. The dialog behind "Add Account" is a plain form
  — the browser only appears when you press the sign-in button inside it — so
  anyone who was going to paste an API key was still made to install a browser
  engine. Two of the five providers (Azure OpenAI, Anthropic API) authenticate
  with a pasted key and never involve a browser at all.

  "Add Account" now asks which provider first, and for cookie providers offers
  the choice between signing in with the built-in browser and pasting the
  credential by hand. The first goes through the window as before; the second
  stays entirely inside VS Code, through a new `create_account` request that
  reuses `update_account` for its validation rather than repeating the rules.

  **A fresh install with no PySide6 can now create its first account.** Until
  now it could not, except by hand-editing `config.json`. Verified end to end
  against an interpreter without PySide6: create, edit and delete all succeed;
  only the sign-in window refuses, which is the point.

- **A command to open the settings file (`config.json`).** Several errors told
  you to add a domain to `aoai_allowed_hosts`, a setting that exists only in
  that file, with no way to reach it from anywhere in the UI. It is deliberately
  not lifted into VS Code settings — that file is the sole owner, and mirroring
  it the way the proxy settings are mirrored would let an empty default silently
  wipe out a restriction on where your API key may be sent.

- **The build now checks that every string in the source has a catalogue
  entry.** It used to check only that the three language files agreed with each
  other, on the grounds that multi-line concatenations could not be extracted
  reliably. They can: Python through `ast` (the parser folds implicit
  concatenation), JavaScript through a small scanner that folds `+`, and the
  `t(variable)` forms by importing the provider attributes they come from.

### Fixed

- **Thirteen strings had no translation and came out in English on a Japanese
  display.** All but one were in the Azure OpenAI provider, which is a live
  entry in the list — its description, the placeholder in its endpoint field,
  and the confirmation shown *every time* you save an endpoint. They were the
  residue of one rewrite: the provider moved from "fixed internal domain" to
  "you list the domains you allow", and the new wording was never added while
  the old wording was never removed. The catalogues agreed with each other
  perfectly — all three had been left behind together — so the old check saw
  nothing wrong. **Agreeing and being present are different things.**

- **Four places pointed at a fix that does not exist or does not work.**
  "Enter the proxy user name and password in Settings" — the password
  deliberately has no setting, because `settings.json` is plain text and syncs
  between machines; it has its own encrypted entry point, which the username
  setting now links to directly. "Add the domain to the `aoai_allowed_hosts`
  setting" — reachable now, see above. "Point `REQUESTS_CA_BUNDLE` at the CA
  certificate" — correct, but an environment variable set after the editor
  started never reaches the backend, not even across a backend restart, and the
  message now says so.

- **Changing the display language left already-fetched rows in the old
  language.** The quota labels, the status summaries and the error text are all
  translated by the backend, and the extension's cache was keeping the previous
  language's strings. It is now cleared and refetched.

- **Four messages interpolated an untranslated label**, producing "API key が
  未設定です" where "API キー" was meant. The other eleven call sites were
  already passing the label through `t()`.

- **On a machine without PySide6, a failed account spawned a helper process
  every minute**, each dying in 0.2 seconds — and holding the GUI lock while it
  did, which could make a legitimate "Add Account" fail with "another operation
  is in progress". The backend now recognises a missing dependency as permanent
  for the life of the process and stops trying.

- **The test suite's guard against importing the build-time copies covered
  `services` but not `models`.** Deliberately importing the copy of `models`
  first went through unnoticed. Since `tests/test_backend_rpc.py` imports
  `models.account` and runs first in collection order, this was not theoretical.

- `README.md` documented `autoRefreshMinutes` as defaulting to `0`; it has
  defaulted to `1` since 1.3.0. `extension.js` carried a second copy of that
  default that disagreed with the manifest.

### Changed

- `get_proxy` is no longer dead code: "Set Proxy Password" now tells you whether
  one is already saved, so confirming an empty box no longer clears a password
  you did not know was there.
- Five catalogue entries for wording that no longer exists were removed, and the
  ones the new wording needs were added, in all three languages.
- Comments and docs that still described editing (and then adding) as needing
  PySide6 were corrected — in the backend, in the `guiPythonPath` setting
  description in four languages, and in the README.

## [1.5.1] - 2026-08-20

A review of 1.5.0 found a defect in it, and a claim in it that was not true.

### Fixed

- **An account with no stored credential could not be edited at all.** The
  "enter a credential" check in `update_account` fired even when the request
  never touched the credential, so renaming such an account — or merely enabling
  it — failed with "Enter Cookie." The same enable/disable toggle worked from
  the list and failed from the edit menu, because the list goes through
  `set_enabled`. It now refuses only when the request tries to *empty* a
  credential, which is the one case that throws something away.

  This is not a hypothetical account. A `config.json` carried to another machine
  or another Windows user cannot be decrypted, and `secret_store` returns an
  empty string when it cannot — every account in that file lands in this state,
  and until now every one of them was uneditable except by pasting a credential
  first.

### Changed

- **The 1.5.0 note claimed the extension was "usable end to end without
  PySide6". It is not.** Adding an account still opens the sign-in window, and
  the dialog behind it is a plain form — the browser is only reached by pressing
  the sign-in button inside it. Two of the five providers (Azure OpenAI,
  Anthropic API) authenticate with a pasted API key and never involve a browser
  at all, yet registering one still requires PySide6. **It is the same mistake
  deleting and editing had, one door further along**, and it is not fixed here.

  What is true today: an account that already exists can be kept working without
  PySide6 — renamed, re-pointed, re-credentialed, disabled, deleted. A fresh
  install without PySide6 cannot create its first account except by hand-editing
  `config.json`.

  The overstatement has been corrected in the backend's own comments, in the
  `aiUsageManager.guiPythonPath` setting description (all four languages), and
  in the 1.5.0 entry below.

## [1.5.0] - 2026-08-20

"Edit" opened the same PySide6 dialog the desktop build uses, so renaming an
account — or pasting an API key — asked for a browser engine that has nothing to
do with either. On a Python without PySide6 the extension answered "PySide6 is
required to show the sign-in window", and there was no way to edit an account
from inside VS Code at all. Deleting was moved off that path in 1.4.0 for the
same reason; editing is the other half of it.

### Changed

- **Editing an account happens inside VS Code and no longer needs PySide6.**
  "Edit Account" opens a quick-pick menu — name, provider, the extra field, the
  spending cap, the credential, enable/disable — and each entry is edited in
  VS Code's own input box. The backend writes the change through a new
  `update_account` request that never launches a window, the way
  `delete_account` already worked.

  **A credential can be pasted here too**, so an account that already exists
  can be kept working without PySide6 (adding a new one still opens the
  window — see 1.5.1). Providers that offer a manual route
  (`manual_url` / `manual_steps`) show their steps first and can open the page
  in your normal browser. The box is masked, and leaving it empty keeps the
  saved value instead of clearing it — a request carries only the fields you
  actually changed.

  The checks are the ones `ui/account_dialog.py` runs: the same normalisation,
  the same "that does not look like `{marker}`" questions, asked before anything
  is saved. A paste is scrutinised the same way whichever door it comes in
  through. What differs is *when* they run — the desktop dialog confirms every
  field at once, while this menu checks only the field you just touched, because
  a warning about a field you did not open has nowhere to be fixed.

  Only "sign in with the built-in browser" still opens a window, and it stays on
  the menu for the providers that support it.

- `gui_helper.py` no longer has an `edit` mode and the `edit_account` request is
  gone from the backend protocol. Keeping them would mean two ways to edit an
  account, and two places to keep the validation honest.

### Added

- `list_providers` returns what each provider needs — labels, hints, whether it
  takes an extra field or a spending cap, and how to fetch a credential by hand.
  The edit menu asks it what to offer instead of hard-coding the answers.
  Providers retired from the list are returned when asked for by id, so an
  account still using one can be edited rather than becoming unreachable.

### Fixed

- **`aiUsageManager.guiPythonPath` took effect only after a window reload.** The
  value is handed to the backend process as an environment variable when it is
  spawned, and the configuration watcher did not list the setting — so the
  extension pointed at that very setting when PySide6 was missing, and then
  ignored it until the window was reloaded. It now restarts the backend the way
  `pythonPath` already did.

## [1.4.0] - 2026-08-19

ChatGPT accounts stopped working after a while and said "sign in again". That
was unhelpful in every case and wrong in one of them — pasting again could
never fix it. Two separate things were behind it, and the error message hid
both.

### Added

- **ChatGPT sessions are extended on every fetch, so they no longer expire on
  their own.** `/api/auth/session` is a rolling session: each call issues a new
  `sessionToken` and pushes the expiry about 90 days out. The newly issued
  value was thrown away every time and the one pasted at setup was kept
  forever, so once that one fell out of the refresh chain the account was dead
  and only a new paste brought it back. The provider now hands the new value
  back as `result["credential"]`, which the settings writer already knew how to
  save — Gemini has been doing this through `rotate_cookies` for a while.

  Renewal runs only after the usage call succeeds, because a session can only
  be extended while it is still alive. It is throttled to once an hour: the
  value is a JWE that is re-encrypted on every call, so it differs every time
  and "has it changed" cannot decide anything. The throttle is keyed on the
  account rather than on the stored value — a value key would change the moment
  it renewed and would never throttle a second time.

- **Whatever the server said is now part of the error.** `401` and `403` shared
  one branch that discarded the response body, so "Could not parse your
  authentication token" and "Workspace is not authorized in this region" — which
  need opposite fixes — both came out as "Cookie is invalid or expired".

- **Workspace accounts send the headers `backend-api` expects.**
  `ChatGPT-Account-Id` and `x-openai-internal-codex-residency` are read out of
  the access token the app already holds, and are added only when those claims
  are present, so personal accounts are unaffected.

### Fixed

- **A session ChatGPT itself cannot refresh is now named as such.**
  `/api/auth/session` can answer `200` with valid user data, an `error` of
  `RefreshAccessTokenError`, and an `accessToken` that expired days ago. The
  error was ignored and the expired token sent anyway, which produced a `401`
  and a "sign in again" that was misleading — the cookie was fine, so pasting
  it again gave exactly the same result. It is now caught when pasting *and*
  when fetching, and says that the browser session has to be signed out of and
  back into.

- **Bot protection is no longer reported as an expired credential.** A `403`
  carrying `Cf-Mitigated` is a challenge from the site's bot protection, not a
  credential problem. It now says so and quotes the `CF-RAY`, instead of asking
  for a sign-in that would not have helped.

- **Pasted credentials survive a dirty copy.** The extraction gave up on
  anything that did not start with `{`, which disabled the regular-expression
  fallback exactly where it was needed: a BOM, a JSON viewer's "Pretty-print"
  label, or line numbers in front of the JSON were enough. The whole paste was
  then saved as the credential *without a warning*, because the shape check
  accepted any value containing an `=` — and a signed-in session page always
  contains one, in the profile image URL. A trailing `",` left over from
  hand-copying a single line is stripped as well.

- **A credential containing newlines no longer escapes as "unexpected error".**
  Sending one raised `ValueError` from `http.client`, which is not a `requests`
  exception and so was never wrapped; the reason reached neither the screen nor
  the log.

- **The expiry of an exchanged access token is checked too**, not only that of
  a pasted one.

- **The signed-out session page is told apart from an expired cookie.**
  `/api/auth/session` now answers `200` with a `WARNING_BANNER` key rather than
  an empty object, and the key names received are included in the message so
  that a change of shape on ChatGPT's side is visible next time instead of
  being read as "the cookie expired".

## [1.3.0] - 2026-08-19

Four things the desktop build carried were lost when the repository was
reduced to the extension and its shared backend. Nothing had replaced them.

### Added

- **Signing in again no longer always needs a window.** When a cookie expired,
  the fetch ended with "sign in again" and there was nothing else to try. The
  account's browser profile usually still holds a live session, so the site is
  now loaded once in a hidden view first; only when that comes back at the
  sign-in page is the window shown. This is separate from Gemini's cookie
  renewal, which extends a cookie that is *still valid* and cannot help once
  one has expired.

  The hidden load runs in the sign-in helper process, where QtWebEngine can
  run, and is skipped while a sign-in window is already open — that window
  writes back the account list it loaded when it opened, and the two must not
  both save.

- **Proxy settings can be set from the extension.** The backend has read
  `proxy_mode`, `proxy_host`, `proxy_port`, `proxy_username` and
  `proxy_password` from its own config file all along, but nothing in the
  extension could write them, so the only way to set a proxy was to edit that
  file by hand — while the sign-in dialog was telling the reader to "enter the
  user name and password in the settings".

  `aiUsageManager.proxy.mode`, `.host`, `.port` and `.username` are now VS Code
  settings. **The password is not**, and deliberately so: `settings.json` is
  plain text and is copied to other machines by Settings Sync. It is set with
  `AI-UsageManager: Set Proxy Password` and stored encrypted (DPAPI on
  Windows), like the account credentials.

  `AI-UsageManager: Test Proxy Connection` and `AI-UsageManager: Import Proxy
  From Environment Variables` come back with them. The test sends no
  credentials — the API answering "not authenticated" is itself proof that the
  proxy was passed — and tells apart proxy authentication (407), a Cloudflare
  browser check, and a corporate block page, because "it does not connect" does
  not say which of them to go fix.

- **The log is written to `app.log` again** (1 MB, three generations, in the
  configuration folder). The output channel is cleared when VS Code closes, so
  nothing survived to be read afterwards — and the sign-in helper has been
  telling readers to check `app.log` the whole time. Only the backend opens the
  file; the helper's output reaches it through the backend, because two
  processes rotating one file cannot both win on Windows.

- **Tests.** `tests/` was the only place the parsing rules, the threshold
  table, the budget arithmetic and the scoping of proxy credentials to a single
  host were written down; it did not survive the move to this repository.
  200 of them run again (18 cover desktop widgets that no longer exist here and
  are skipped). `pytest` from the repository root.

  The Gemini cookie tests are rewritten against the 1.2.2 renewal logic and now
  cover what that release fixed: a cookie that was just pasted is renewed at
  once instead of waiting out the ten-minute interval, and the widened set of
  kept cookies is both sent and stored back.

- **`requirements.txt`**, with the versions the extension is built against.
  README told the reader which packages to install but nothing pinned them.
  PyInstaller is not included — this repository does not build an exe. PySide6
  is: adding, editing and signing in again open a real browser window, and the
  hidden refresh above needs the same engine.

## [1.2.3] - 2026-08-19

### Fixed

- **Deleting an account asked for PySide6.** Removing an account needs no
  browser window, but it was routed through the same helper process as adding,
  editing and signing in again. On a Python without PySide6 the extension
  answered "PySide6 is required to show the sign-in window", so an account
  could not be deleted from the extension at all. Deletion now happens inside
  the backend: the account is dropped from `config.json` and its saved sign-in
  state (the browser profile, cookies included) is removed on the spot.
  `requests` and `urllib3` stay the only requirements for everything except the
  sign-in window itself.

  Deleting is refused while a sign-in window is open, because that window
  writes back the account list it loaded when it opened and would bring the
  deleted account back.

## [1.2.2] - 2026-08-09

### Fixed

- **Gemini kept asking to be signed in again.** Two separate causes.

  The cookie renewal call was sending too little. `RotateCookies` was answering
  200 without a replacement `__Secure-1PSIDTS`, so the session was never
  extended and died within a day. Only `__Secure-1PSID`, `__Secure-1PSIDTS`
  and `__Secure-1PSIDCC` were being stored, and the renewal request was
  narrowed a second time on top of that. The rotation token
  `__Secure-1PSIDRTS` was present in the account's browser profile but was
  discarded before it could be sent. The stored set now also keeps
  `__Secure-1PSIDRTS`, `NID` and the `__Secure-3PSID` family, and the renewal
  request sends everything that was kept. Cookies that reach the whole Google
  account (`SID`, `SAPISID`, `HSID`, `SSID`, `APISID`) are still discarded.

  Renewal was also suppressed for up to ten minutes immediately after pasting
  a fresh cookie. The throttle was keyed on `__Secure-1PSID`, which does not
  change when you sign in again. It now also compares `__Secure-1PSIDTS`, so a
  newly pasted credential is renewed at once.

- Cookies returned by a successful renewal other than `__Secure-1PSIDTS` and
  `__Secure-1PSIDCC` were discarded, so the next renewal sent stale values.
  Every kept cookie the response replaces is now stored.

### Changed

- **`aiUsageManager.autoRefreshMinutes` now defaults to 1 minute instead of 0
  (off).** Gemini's cookie can only be extended while it is still valid, and
  extension happens after a successful fetch — with automatic refresh off there
  was almost no opportunity to extend it. Set it back to 0 to restore the
  previous behaviour. Cookie renewal itself stays rate-limited to once per 600
  seconds regardless of this setting.

- When renewal returns 200 without a new `__Secure-1PSIDTS`, the log now also
  records which cookie *names* were sent (never their values), so a future
  occurrence can be diagnosed instead of guessed at.

## [1.2.1] - 2026-08-08

### Fixed

- **Gemini stopped reporting usage.** `gemini.google.com/usage` now answers with
  a 302 to Google's abuse interstitial (`www.google.com/sorry/`) before
  redirecting back. Cookies were being sent as a request header only, and
  `requests` discards a manually set `Cookie` header when it follows a
  redirect — so the final page was fetched with no credentials and returned a
  signed-out document with HTTP 200. Because no 401 was ever involved, this
  surfaced as "no authentication token could be taken from the usage page",
  which is indistinguishable from an expired cookie.

  Cookies are now carried in the session cookie jar, scoped to the target host,
  so they survive redirects. Server-set cookies are kept for the duration of a
  single fetch (Gemini's quota call is rejected with HTTP 400 without them) and
  discarded when a different account is fetched, so accounts cannot bleed into
  one another.

### Changed

- Gemini cookie refresh no longer logs a warning when Google returns HTTP 200
  without a replacement `__Secure-1PSIDTS`. That is the normal response while
  the current token is still fresh, not a failure.

## [1.2.0] - 2026-08-08

### Added

- **Localization for English, Japanese, Korean and Simplified Chinese.**
  English is the source language; the display language follows VS Code and can
  be overridden with the `aiUsageManager.language` setting.
- `AI-UsageManager: Select Language` command.
- The build now validates the translation catalogues before packaging and fails
  when they disagree, so a half-translated release cannot ship silently.

## Earlier versions

Versions before 1.2.0 were built and installed directly from a `.vsix` and were
never published, so they have no release notes here.
