# Account Login And Cookie Management

Books of Time 默认使用单个 `default` 账号。登录是独立管理命令，不依赖 PostgreSQL；未登录、已登出或 Cookie 确认失效时，服务仍会匿名运行。

## Quick Start

Windows、Linux 原生运行：

```bash
uv run python main.py login qr
uv run python main.py login status
```

终端会显示二维码。使用 Bilibili 手机客户端扫码并确认后，新 Cookie 会写入本地加密快照；命令不会打印 `SESSDATA`、CSRF 或 refresh token。

指定当前预留的账号 ID：

```bash
uv run python main.py login qr --account default --timeout-seconds 180
uv run python main.py login status --account default
uv run python main.py login logout --account default
```

当前版本只运行一个配置账号。`account_id` 是为未来多用户保留的持久化和接口兼容点，不提供账号池、按请求切换账号或扩大请求预算的能力。

## Linux Service

使用与 systemd 服务相同的用户和环境执行登录，避免生成服务不可读的文件：

```bash
set -a
. /etc/books-of-time/books-of-time.env
set +a
cd /opt/books-of-time
sudo -u books-of-time --preserve-env uv run python main.py login qr
sudo -u books-of-time --preserve-env uv run python main.py login status
sudo systemctl restart books-of-time
```

服务不要求重启才能看到新 Cookie；每次请求都会检查凭据文件版本。重启命令只用于需要立即重新执行部署检查的场景。

## Docker Compose

运行中的容器可直接登录：

```bash
docker compose --env-file deploy/docker.env exec books-of-time \
  /app/.venv/bin/python main.py login qr
```

服务尚未启动时，可使用一次性容器；`accounts` volume 会保存结果：

```bash
docker compose --env-file deploy/docker.env run --rm books-of-time \
  /app/.venv/bin/python main.py login qr
```

默认宿主机目录是 `${BOT_DATA_DIR}/accounts`，容器内目录是 `/var/lib/books-of-time/accounts`。

## Automatic Refresh

服务 scheduler 默认每 21600 秒检查一次当前 Cookie：

- Cookie 有效且不需刷新：仅更新检查时间。
- Cookie 需要刷新：保存新快照并自动切换，后续请求立即使用最新版本。
- Cookie 明确失效：标记为 `invalid`，后续请求退回匿名。
- 网络或平台临时失败：保留旧快照，scheduled job 按现有退避策略重试。

可在 YAML 中配置：

```yaml
accounts:
  enabled: true
  active_account_id: default
  credentials_path: ./data/accounts/credentials.enc
  key_path: ./data/accounts/master.key
  history_limit: 5
  auto_refresh: true
  refresh_check_seconds: 21600
```

部署环境可使用 `BOT_ACCOUNT_ENABLED`、`BOT_ACCOUNT_ID`、`BOT_ACCOUNT_CREDENTIALS_PATH`、`BOT_ACCOUNT_KEY_PATH`、`BOT_ACCOUNT_AUTO_REFRESH` 和 `BOT_ACCOUNT_REFRESH_SECONDS` 覆盖。

## Local Files And Security

默认文件：

```text
data/accounts/master.key
data/accounts/credentials.enc
data/accounts/credentials.enc.lock
```

key 和密文在支持 POSIX 权限的系统上创建为 owner-only。加密用于降低备份、归档或误操作造成的意外泄露；能够同时读取 key 与密文的本机管理员或进程仍可解密。

- 不要提交 `data/accounts`、key、密文或 Cookie 到 Git。
- 不要把 Cookie 放进 YAML、命令行参数或日志。
- 备份和恢复时必须同时处理 `master.key` 与 `credentials.enc`。
- Linux 上让目录仅对 `books-of-time` 服务用户可读写。
- 登出会移除该账号的全部本地快照；如需重新认证，再执行 `login qr`。
