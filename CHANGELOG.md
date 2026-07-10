# Changelog

## 2026-07-10 — Synchronized expense category icons

- Expenses can persist a category and a validated SF Symbol or Emoji icon.
- Added nullable API fields and a backwards-compatible database migration.

## 2026-07-10 — Live ledger member names

- Registered ledger members now always display their current user nickname.
- Temporary member names remain stored independently on the ledger membership.
- Added database constraints and eager user loading to prevent invalid rows and N+1 name lookups.

## 2026-07-10

### Added

- Added signed Tencent Cloud realtime-ASR WebSocket streaming for voice expenses.
- Added request-scoped ledger-member hotwords with Chinese and non-Chinese weighting.
- Added detailed Chinese diagnostics and phase timing for voice sessions.
- Added layered configuration from committed defaults, local overrides, and environment variables.

### Changed

- Replaced the previous FunASR adapter with the Tencent realtime-ASR client.
- Improved voice-draft member matching and deferred split calculation to final expense submission.
- Updated voice-session handling, final-result timeouts, and OpenAI-compatible draft parsing.

### Fixed

- Fixed Tencent signature plaintext construction and Unicode temporary-hotword signing.
- Filtered invalid temporary hotwords and applied super-hotword weight to non-Chinese names.
