# Changelog

All notable changes to the AI-UsageManager extension are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
