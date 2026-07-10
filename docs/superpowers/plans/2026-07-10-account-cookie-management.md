# Account And Cookie Management Implementation Plan

**Goal:** Add independent QR login, encrypted versioned Cookie storage, per-request latest-Cookie injection, and automatic refresh with anonymous fallback.

**Method:** Execute directly in the main workspace with TDD. Commit each task separately; no design approval checkpoint is required.

## Task 1: Encrypted Credential Store

- [x] Add direct cryptography and file-lock dependencies.
- [x] Write tests for key creation, encrypted round-trip, bounded versions, atomic replacement, status and logout.
- [x] Implement account snapshot models and `EncryptedFileCredentialStore`.
- [x] Verify tests, secret redaction and file modes.
- [x] Commit `feat: store versioned account credentials`.

## Task 2: Latest Cookie Provider And HTTP Injection

- [ ] Write tests for missing/invalid anonymous fallback, latest snapshot selection, external file reload and merge precedence.
- [ ] Implement `CurrentCookieProvider` and optional provider injection in `RawHttpClient`.
- [ ] Add managed-cookie opt-out to bilibili request capture context.
- [ ] Wire the provider through application builders, including direct media downloads.
- [ ] Commit `feat: inject latest managed cookies`.

## Task 3: Independent QR Login CLI

- [ ] Write QR state-machine and CLI parser/dispatch tests.
- [ ] Implement `QrLoginFlow`, `bot login qr/status/logout`, timeout and safe output.
- [ ] Ensure login uses unified rate limiting with managed Cookie injection disabled and no raw secret archive.
- [ ] Update the existing QR example without printing credentials.
- [ ] Commit `feat: add QR account login CLI`.

## Task 4: Automatic Cookie Refresh

- [ ] Write tests for anonymous no-op, valid unchanged, invalid fallback, successful rotation and transient failure preservation.
- [ ] Implement `AccountManager.refresh_if_needed()` around bilibili-api-python Credential APIs.
- [ ] Add a persistent scheduled-job kind/handler and configuration interval.
- [ ] Verify service construction and execution without account files.
- [ ] Commit `feat: refresh managed cookies automatically`.

## Task 5: Operations And Documentation

- [ ] Add account paths/interval/environment overrides to example and deployment configs.
- [ ] Write concise `docs/LOGIN.md` for Windows, Linux and Docker usage and permissions.
- [ ] Mark P1 TODO acceptance items complete only after full test, Ruff and service smoke verification.
- [ ] Run full SQLite tests and isolated PostgreSQL migration/schema checks.
- [ ] Commit `docs: add account login operations guide`.
