# Account And Cookie Management Design

## Goal

为 Books of Time 提供单账号二维码登录、Cookie 版本轮换和全请求最新凭据注入，同时保证无凭据、凭据过期或登录 CLI 未运行时，长期采集服务仍可匿名启动和工作。

## Boundaries

- 当前只选择一个配置账号，默认 ID 为 `default`。
- 不实现账号池、按请求切换账号、并发多账号调度、代理联动或规避平台风控。
- 存储与 provider API 从一开始携带 `account_id`。这是为未来真实多用户需求冻结兼容边界，避免重写加密文件格式、CLI 和 HTTP 注入接口；它不是提前实现多账号功能。
- 登录与凭据刷新响应可能包含秘密，不进入 raw evidence archive，也不写日志。
- 普通采集、媒体下载和 bilibili-api-python 请求继续经过统一 HTTP 客户端与限流器。

## Architecture

```text
bot login qr
  -> QrLoginFlow (managed cookies disabled)
  -> AccountManager.save_login()
  -> EncryptedFileCredentialStore (atomic replace)

service / worker request
  -> RawHttpClient
  -> CurrentCookieProvider.get_cookies(account_id="default")
  -> mtime-aware latest snapshot cache
  -> merge latest managed cookies into request

scheduled cookie refresh
  -> AccountManager.refresh_if_needed()
  -> bilibili Credential check_valid/check_refresh/refresh
  -> managed cookies disabled for refresh handshake
  -> new encrypted snapshot
  -> subsequent requests observe new file version
```

## Modules

```text
books_of_time/accounts/
  models.py       immutable snapshot/status values
  storage.py      encrypted local file, key lifecycle, atomic writes and lock
  provider.py     latest valid Cookie provider with external-change reload
  manager.py      login save, logout, status and refresh decisions
  qr_login.py     bilibili-api-python QR flow without secret output
  scheduled.py    persistent scheduled-job refresh handler
```

## Local Storage

Default paths:

```text
data/accounts/master.key
data/accounts/credentials.enc
data/accounts/credentials.lock
```

`credentials.enc` is a Fernet-encrypted JSON envelope. Each account contains a bounded history of Cookie snapshots, one active snapshot ID, health state and last validation time. A new QR login or successful refresh appends a snapshot, marks the previous one superseded, and atomically replaces the encrypted file.

The key and encrypted file are created with owner-only mode where the operating system supports POSIX permissions. Encryption protects accidental disclosure through backups, archive tools and casual file inspection; it does not protect against a process or administrator that can read both the key and ciphertext. Docker and systemd must mount the account directory persistently and restrict access to the service user.

Cross-process writes use a lock file and same-directory atomic replacement. History is bounded so automatic refresh does not grow the file indefinitely.

## Cookie Selection And Merge

`CurrentCookieProvider` returns only the active snapshot when health is not `invalid`. Missing key/file/account/snapshot or an explicitly invalid snapshot returns an empty mapping.

`RawHttpClient` asks the provider immediately before every request. The provider caches decrypted state but compares file metadata on every call, so a login CLI process can rotate credentials without restarting the service.

Merge order is:

```text
request cookies -> latest managed cookies
```

Thus bilibili-api-python's anonymous empty Cookie fields and stale explicit values cannot override the current managed snapshot. Login and refresh handshakes pass `use_managed_cookies=False`, because those operations intentionally exchange a specific old/new credential pair.

## Refresh And Failure Semantics

A persistent scheduled job periodically checks the selected snapshot:

1. No snapshot: succeed as `anonymous`, without network access.
2. `check_valid` false: mark invalid; all later requests become anonymous.
3. Valid and no refresh needed: update validation metadata only.
4. Refresh needed: call library refresh, save a new active snapshot, retain bounded history.
5. Transient request failure: keep the last snapshot unchanged and let scheduled-job backoff retry.

The service never requires a valid Cookie during construction or doctor checks. Authentication improves request context but is not a service liveness dependency.

## CLI

```text
bot login qr [--account default] [--timeout-seconds 180]
bot login status [--account default]
bot login logout [--account default]
```

QR output contains only the terminal QR and state transitions. Status shows account ID, health, source, snapshot creation time and last validation time. No command prints Cookie values.

## Acceptance

- Two saved snapshots resolve to the newest active version without restart.
- A second process atomically replacing the store is observed on the next request.
- Every managed HTTP request receives the latest non-empty Cookie values.
- Login/refresh requests can opt out of injection.
- Invalid or absent credentials yield anonymous requests and do not stop the service.
- QR completion persists a snapshot without printing secrets.
- Refresh creates a new version and scheduled-job retry preserves the old version on transient failure.
- Tests pass on Windows and Linux-compatible paths; Docker uses the same mounted local account directory.
