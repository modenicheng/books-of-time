# Books of Time Deployment

Books of Time 是长期运行的应用服务。PostgreSQL 由宿主机或局域网现有实例提供，应用不会启动数据库容器，也不会在正常启动时自动迁移 schema。raw payload、media asset 与加密账号凭据始终写入本地持久目录。登录操作见 [LOGIN](LOGIN.md)，对应仓库路径为 `docs/LOGIN.md`。

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

### New Database

对空数据库直接运行：

```bash
uv run alembic upgrade head
uv run python main.py service doctor
```

### Existing Unversioned Development Database

旧版本使用 `init-db` 创建表但没有 `alembic_version`。不要直接对这些已有表执行初始 upgrade。先完整备份，再执行：

```bash
# 仅用于把旧开发库补齐到当前 ORM 的缺失表；不会修改已有列。
uv run python main.py init-db

# 必须显示 No new upgrade operations detected。
uv run alembic check

# 只有 check 无差异时才登记基线。
uv run alembic stamp 0001_initial
uv run python main.py service doctor
```

如果 `alembic check` 报告差异，应先审查并编写针对该旧库的迁移，不能强行 stamp。

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
```

`health` 检查数据库、Alembic revision、本地目录和服务心跳。`status` 展示实例、队列积压、最老待处理任务和活动请求退避。

## Backup Checklist

备份必须把 PostgreSQL 与本地文件视为一个证据集：

- PostgreSQL 使用 `pg_dump` 或现有数据库备份方案。
- raw/media/accounts 使用文件级快照或增量备份；accounts 中的 key 与密文必须成对恢复。
- 记录应用 commit、Alembic revision 和备份时间。
- 定期执行恢复演练，确认数据库中的 `storage_uri` 能定位恢复后的文件。
