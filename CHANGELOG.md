# Changelog

## 2026-07-18 — Admin password reset

- Platform admin: `POST /admin/users/{id}/reset-password` sets a new password (min 6) without old password; audited as `user.password_reset_admin`.

## 2026-07-18 — User nameplates (badges)

- Added `users.badge` (migration `20260718_0023`) and admin-managed `badges` table (`20260718_0024`) seeded with 创始人/船员/搭子/内测官/特邀.
- CRUD: `GET/POST /admin/badges`, `PATCH/DELETE /admin/badges/{id}`; assign `PATCH /admin/users/{id}/badge`.
- `UserResponse` includes `badge`, `badge_label`, `badge_color`.

## 2026-07-17 — Ledger covers & Sign in with Apple

- Added `ledgers.cover_url` (migration `20260717_0022`) and owner-only `POST/DELETE /ledgers/{id}/cover` using the same COS upload path as avatars (`ledger-covers/` folder).
- Apple login no longer fails when the identity token omits email; uses a stable placeholder and never asks the client to re-collect email/name.
- Applies `full_name` from the first SIWA credential to `display_name` when provided.

## 2026-07-16 — Ledger confirmation toggle & projected settlements

- Added per-ledger `require_confirmation` (migration `20260716_0021`); default on for existing ledgers.
- `PATCH /ledgers/{id}` for owners to update name/currency/confirmation; turning confirmation off auto-confirms pending expenses.
- Creating/updating expenses respects the flag: when off, expenses are confirmed immediately.
- Settlement flow includes confirmed **and** pending (non-rejected) bills so projected transfers match the fully-confirmed end state.
- Settlement instructions expose `includes_unconfirmed` when either party still has pending bills.
- Password change returns Chinese errors, enforces min length, and writes an audit event (console/platform ops).

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
