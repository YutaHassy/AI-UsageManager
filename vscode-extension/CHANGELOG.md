# Changelog

All notable changes to the AI-UsageManager extension are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
