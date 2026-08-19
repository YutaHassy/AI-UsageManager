# Changelog

All notable changes to the AI-UsageManager extension are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
