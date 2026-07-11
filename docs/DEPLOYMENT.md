# Books of Time Deployment

Books of Time 是长期运行的应用服务。PostgreSQL 由宿主机或局域网现有实例提供，应用不会启动数据库容器，也不会在正常启动时自动迁移 schema。media asset 与加密账号凭据始终写入本地持久目录；raw payload 默认写本地文件系统，也可单独切换到已有 MinIO。登录操作见 [LOGIN](LOGIN.md)，对应仓库路径为 `docs/LOGIN.md`。

## Deployment Contract

所有部署共享以下顺序：

1. 备份 PostgreSQL、raw、media 和 accounts。
2. 更新代码并用 `uv sync --frozen --no-dev` 安装锁定依赖。
3. 显式执行 `uv run alembic upgrade head`。
4. 执行 `uv run python main.py service doctor`。
5. 启动或重启 `service run`。
6. 使用 `service health` 和 `service status` 验收。

`service run` 不会调用 `create_all` 或 Alembic。schema 不匹配时，doctor 和正式启动都会失败。

## PostgreSQL

推荐为 Books of Time 使用独立数据库和最小权限用户。数据库 URL 使用 asyncpg：

```text
postgresql+asyncpg://USER:PASSWORD@HOST:5432/books_of_time
```

数据库必须允许应用所在主机访问。Docker 连接宿主机 PostgreSQL 时，需要让 PostgreSQL 监听 Docker bridge 可达地址，并在 `pg_hba.conf` 中仅允许实际 bridge 网段和目标用户/数据库。不要把数据库无条件开放到公网。

服务连接 PostgreSQL 时，请求限流状态保存在 `request_budget_states`。同一数据库上的 scheduler、worker 和 worker 副本会在一个事务中同时保留 `global`、host 与 request type 三层令牌，因此共享同一请求预算；任一层额度不足都不会部分消耗其他层。各实例的 `rate_limit` 配置必须一致，配置漂移会令请求明确失败，避免静默扩大平台请求量。

SQLite 仅用于 Windows 开发和单进程测试，继续使用进程内 token bucket，不提供跨进程预算保证。二维码登录是独立的一次性管理命令，也不依赖数据库预算表。

### New Database

对空数据库运行下列任一入口，两者都会执行 Alembic `upgrade head`：

```bash
uv run python main.py init-db
# 或
uv run alembic upgrade head
uv run python main.py service doctor
```

### Existing Unversioned Development Database

旧版本的 `init-db` 通过 `create_all` 创建表，可能没有 `alembic_version`。不要直接对这些已有表执行初始 upgrade，也不要手工强制 stamp。先完整备份，再执行：

```bash
uv run python main.py init-db --adopt-legacy
uv run python main.py service doctor
```

`--adopt-legacy` 会先用 Alembic metadata 严格比对旧库，只接受已知的基线漂移：事件/flag 新表、`frontier_states.extra` 和已知 enum 值。它会在补齐后登记 `0001_initial`，再升级到 head。任何额外缺表、缺列、索引或约束差异都会直接拒绝接管；此时应人工审计并编写针对性迁移。

## Docker Compose

Compose 只运行 Books of Time。先准备环境变量：

```bash
cp deploy/docker.env.example deploy/docker.env
# 编辑 BOT_DATABASE_URL 和 BOT_DATA_DIR
docker compose --env-file deploy/docker.env config
```

Linux Docker 使用 `host.docker.internal:host-gateway` 访问宿主机。Mac 和 Windows Docker Desktop 也可使用 `host.docker.internal`。如果 PostgreSQL 在局域网其他主机，直接把 URL 中 host 改为该主机名或地址。

先迁移，再启动：

```bash
docker compose --env-file deploy/docker.env build
docker compose --env-file deploy/docker.env run --rm \
  books-of-time /app/.venv/bin/alembic upgrade head
docker compose --env-file deploy/docker.env run --rm \
  books-of-time /app/.venv/bin/python main.py service doctor
docker compose --env-file deploy/docker.env up -d
docker compose ps
```

默认绑定 `${BOT_DATA_DIR}/raw`、`${BOT_DATA_DIR}/media` 和 `${BOT_DATA_DIR}/accounts`。Linux 上应提前创建目录并让容器用户可写；不要把本地 `config/config.yaml` 或账号密钥放进镜像。

### Optional MinIO For Raw Payloads

MinIO 只用于 raw payload，项目不会启动 MinIO 容器，图片仍写入本地 `BOT_MEDIA_DIR`。连接已有 MinIO 时设置：

```text
BOT_RAW_STORAGE_BACKEND=minio
BOT_MINIO_ENDPOINT=minio.example.internal:9000
BOT_MINIO_ACCESS_KEY=...
BOT_MINIO_SECRET_KEY=...
BOT_MINIO_BUCKET=books-of-time-raw
BOT_MINIO_PREFIX=raw
BOT_MINIO_SECURE=true
BOT_MINIO_CREATE_BUCKET=false
```

推荐预先创建 bucket 并授予应用账号最小的 bucket read/write 权限。只有明确允许应用创建 bucket 时才将 `BOT_MINIO_CREATE_BUCKET` 设为 `true`。`service doctor` 会探测实际 raw 后端；切换后，历史 `file://` 记录不会自动迁移，迁移必须连同 PostgreSQL 中的 `storage_uri` 一起规划。

查看状态：

```bash
docker compose exec books-of-time \
  /app/.venv/bin/python main.py service health
docker compose exec books-of-time \
  /app/.venv/bin/python main.py service status
docker compose logs -f books-of-time
```

## Linux systemd

示例假定代码位于 `/opt/books-of-time`，状态目录位于 `/var/lib/books-of-time`：

```bash
sudo useradd --system --home /opt/books-of-time --shell /usr/sbin/nologin books-of-time
sudo mkdir -p /opt/books-of-time /etc/books-of-time /var/lib/books-of-time/{raw,media,accounts}
sudo chown -R books-of-time:books-of-time /opt/books-of-time /var/lib/books-of-time

cd /opt/books-of-time
sudo -u books-of-time uv sync --frozen --no-dev
sudo cp deploy/books-of-time.env.example /etc/books-of-time/books-of-time.env
sudo chmod 600 /etc/books-of-time/books-of-time.env
sudo cp deploy/systemd/books-of-time.service /etc/systemd/system/
```

编辑环境文件后，在 shell 中加载同一配置并迁移：

```bash
set -a
. /etc/books-of-time/books-of-time.env
set +a
cd /opt/books-of-time
sudo -u books-of-time --preserve-env uv run alembic upgrade head
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now books-of-time
systemctl status books-of-time
journalctl -u books-of-time -f
```

systemd 在启动进程前运行 `service doctor`，失败时不会进入采集循环。迁移保持为显式部署步骤。

## Windows Development

Windows 不需要模拟 systemd。连接本机 PostgreSQL，并直接运行同一内核：

```powershell
uv sync --group dev
uv run alembic upgrade head
uv run python main.py service doctor
uv run python main.py service run
```

Ctrl+C 会触发协作式停止。单独的 `worker loop` 和 `discovery loop` 仅保留用于诊断。

## Upgrade And Rollback

升级前先停止服务或确认任务 lease 可以安全过期，然后备份：

```bash
pg_dump --format=custom --file=books_of_time.dump books_of_time
tar -czf books-of-time-files.tar.gz data/raw data/media data/accounts
```

更新代码后执行 migration、doctor，再重启。若应用回滚到旧 commit，必须确认旧代码能读取当前 schema。不要在没有数据库备份的情况下运行 `alembic downgrade`；优先恢复数据库备份和匹配版本的 raw/media 索引。

## Operations

```bash
uv run python main.py service health
uv run python main.py service status --limit 20
uv run python main.py task list --status failed
uv run python main.py task retry-failed
uv run python main.py database maintain --output maintenance-plan.jsonl
```

`health` 检查数据库、Alembic revision、实际 raw 后端、本地 media 目录和服务/worker 心跳。`status` 展示实例、队列积压、最老待处理任务、活动请求退避，以及最近 `service.request_failure_window_seconds` 秒内的请求页数、请求错误数、失败率和解析错误数。失败率用于观测，不直接触发 health 失败。

`database maintain` 默认只输出并记录计划，不执行 SQL。人工审查后使用 `--execute` 执行 ANALYZE、BRIN summarization 和已验证分区父表的未来月份 DDL；只有明确需要时才额外传 `--vacuum`。VACUUM 可能长时间占用 I/O，应放在低峰窗口运行。普通 `comment_observations` 表不会执行分区 DDL，完整切换前置条件见 [PARTITIONING](PARTITIONING.md)。

## Backup Checklist

备份必须把 PostgreSQL 与本地文件视为一个证据集：

- PostgreSQL 使用 `pg_dump` 或现有数据库备份方案。
- raw/media/accounts 使用文件级快照或增量备份；accounts 中的 key 与密文必须成对恢复。
- 记录应用 commit、Alembic revision 和备份时间。
- 定期执行恢复演练，确认数据库中的 `storage_uri` 能定位恢复后的文件。
