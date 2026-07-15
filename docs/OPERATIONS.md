# Operations Runbook

Books of Time 的正式形态是长期运行服务。本文面向 operator，覆盖启动、停机、监控、任务恢复、备份、维护、容量、升级和扩缩容。部署文件和首次安装见 [DEPLOYMENT](DEPLOYMENT.md)。

## 1. Operating Model

一个服务实例可承担：

- `scheduler`：领取持久化 scheduled job，生成 collection task，刷新 Cookie，评估告警。
- `worker`：领取 collection task，执行请求、归档、解析和写库。

单进程默认 roles：

```yaml
service:
  roles: [worker, scheduler]
```

正式入口：

```bash
uv run python main.py service run
```

split 部署推荐一个 scheduler、一个或多个 worker。每个 worker 进程内部 concurrency 当前固定为 1；增加吞吐量使用 worker 副本，而不是在单进程内并发请求。

PostgreSQL 是多进程模式的前提：task/job lease、shared token bucket 和 service heartbeat 都在数据库协调。SQLite 只用于测试和单进程开发。

## 2. Startup Sequence

每次新部署或升级按同一顺序：

```text
backup -> install locked dependencies -> migrate -> doctor -> start -> health -> status
```

Linux/Windows 原生：

```bash
uv sync --frozen --no-dev
uv run alembic upgrade head
uv run python main.py service doctor
uv run python main.py service run
```

开发环境需要测试依赖时使用 `uv sync --group dev`。

`service run` 启动时会再次 doctor，但不会自动运行 Alembic。schema revision 不匹配时服务拒绝启动。

### Startup Acceptance

另开终端：

```bash
uv run python main.py service health
uv run python main.py service status --limit 20
uv run python main.py task list --status failed --limit 100
```

健康验收要求：

- 数据库和 service schema 可访问。
- `alembic_version` 等于代码预期 head。
- 实际 raw backend 可探测。
- media directory 可创建并写入探针。
- 存在 heartbeat 新鲜的 running service。
- 存在 heartbeat 新鲜且带 worker role 的实例。

仅运行 scheduler 而没有任何 worker 时，`service health` 失败是正确行为。

## 3. Graceful Shutdown

Windows Ctrl+C、Linux SIGTERM、Docker stop 都会设置 stop event：

1. worker/scheduler 循环停止继续领取任务。
2. 当前 worker 最多等待 `shutdown_grace_seconds`。
3. heartbeat 状态依次写入 stopping/stopped。
4. 超过宽限期的 worker task 被取消；数据库 lease 到期后由其他 worker 恢复。

命令：

```bash
sudo systemctl stop books-of-time
docker compose --env-file deploy/docker.env stop
```

不要使用强制 kill 作为日常停止方式。被强杀的实例会保留旧 `running` heartbeat，health 在 timeout 后才视为不新鲜，task 则等待 lease 到期。

## 4. Doctor, Health And Status

| 命令 | 检查内容 | 需要服务运行 | 失败退出 |
| --- | --- | --- | --- |
| `service doctor` | DB、schema revision、raw、media | 否 | 是 |
| `service health` | doctor + service/worker heartbeat | 是 | 是 |
| `service status` | queue、backoff、失败窗口、实例、active alerts | 否，只需 DB | 否 |

`service status` 的请求失败率：

```text
sum(request_errors) / sum(pages_requested)
```

当窗口中没有 requested page 时为 NULL。active operational alert 是需要处理的状态，但 status 命令本身仍正常退出，便于人工和监控系统读取。

## 5. Scheduled Jobs

scheduler 启动时 bootstrap 以下稳定 job：

| Job | 默认周期 | 作用 |
| --- | --- | --- |
| `uid-discovery` | `scheduler.discovery_scan_seconds`，默认 60 秒 | 仅在 10:00（含）到 22:00（不含）为静态/event UID 生成 discovery task；每个重点时点生成 T+0/T+30 两次高优先级检查 |
| `video-snapshot-sweep` | 60 秒 | 全天为到期 known video 生成指标 task |
| `daily-terminal-snapshot` | 60 秒检查 | 22:00 后生成额外的当日日终快照，不停止常规 sweep |
| `snapshot-cohort-planning` | `snapshot_cohorts.planning_seconds`，默认 30 秒 | 仅在 `snapshot_cohorts.enabled=true` 时注册；C5 写入热门范围和 latest current-head shadow 证据，不创建 scan/task |
| `operational-alert-evaluation` | 默认 60 秒 | 评估持久告警，可关闭 |
| `account-cookie-refresh:<id>` | 默认 21600 秒 | 校验/刷新 Cookie，可关闭 |

job 使用 PostgreSQL lease，失败后默认 300 秒重试。成功时按照原 schedule slot 推进；服务停机跨过多个 slot 后会跳到下一个未来 slot，不为每个漏过周期逐次补跑。

启用 cohort shadow planner：

```yaml
snapshot_cohorts:
  enabled: true
  policy_version: cohort-default-v2
  rollout_mode: shadow
  planning_seconds: 30
```

重启 scheduler 后检查 `snapshot-cohort-planning` 连续成功，并确认 `snapshot_cohorts.status='shadow_planned'` 持续增加、`comment_scan_runs` 没有新增行、`collection_tasks.snapshot_cohort_id` 没有新增非 NULL 行。C5 明确拒绝 `rollout_mode: live`；不要修改代码绕过检查，因为旧 sweep/外部评论调度 owner 尚未迁移，会产生重复请求。

planner 使用 UTC 30 秒 bucket 和持久化 `video_collection_states`，重启不会重置 pubdate anchor、tier、checkpoint 或下一到期时间。停机期间漏过的普通 routine 被压缩为 `collection_schedule_gaps`，逾期 checkpoint 保留 missed/not-applicable 证据并生成一个当前 recovery，而不是瞬间补发整段历史请求。有限候选批次先接管尚无 state 的视频，再按最早 `next_due_at` 处理已接管视频，避免长期服务中较老视频永久占满批次。

PostgreSQL 允许多个 scheduler 连接同一数据库：scheduled-job lease 控制 handler 所有权，单视频 state 行锁和唯一键/savepoint 处理 planner 与任务入队竞争。所有实例必须使用同一 policy 内容和 `policy_version`。SQLite 的 `FOR UPDATE` 语义不足以验证该并发模型，只用于 Windows/Linux 单进程开发和确定性单元测试。

UID discovery 按持久化的 `next_run_at` 判断窗口和重点分钟，而不是按 handler
实际开始时间判断。因此 scheduler 短暂繁忙导致的分钟级延迟不会丢失重点标记；
服务停机期间错过的历史 slot 不会在恢复后逐个补请求。视频 sweep、worker task
lease 和 Cookie/告警 job 不使用 discovery 窗口。

重点槽用本地整分钟作为稳定锚点，T+0 和 T+30 的 task 分别记录
`focus_offset_seconds=0/30`。handler 若晚到，会把主任务的可执行时间顺延到
当前时间，并把补任务顺延到其后 30 秒；这避免两次检查在恢复瞬间同时被领取。

配置周期改变后，scheduler 下次 bootstrap 会更新仍包含在 definitions 中的已有 job：kind、周期、priority、payload 和 enabled，不删除成功/失败历史字段。

当前 coordinator 不会自动停用“新配置已不再声明”的旧 job。这影响以下切换：

- `operations.alerts.enabled` 从 true 改为 false 后，已有 `operational-alert-evaluation` 行可能仍 enabled，但新进程没有对应 handler。
- `accounts.enabled/auto_refresh` 关闭或 `active_account_id` 改名后，旧 `account-cookie-refresh:*` 行可能仍 enabled；更换账号时旧/新行还会共享当前 account handler。
- `snapshot_cohorts.enabled` 从 true 改为 false 后，已有 `snapshot-cohort-planning` 行可能仍 enabled，但新进程没有对应 handler。

操作时先停止 scheduler，修改配置，再定向停用旧行：

```sql
BEGIN;
UPDATE scheduled_jobs
SET enabled = FALSE,
    lease_owner = NULL,
    lease_until = NULL,
    updated_at = now()
WHERE job_key = 'operational-alert-evaluation';

UPDATE scheduled_jobs
SET enabled = FALSE,
    lease_owner = NULL,
    lease_until = NULL,
    updated_at = now()
WHERE job_key LIKE 'account-cookie-refresh:%';

UPDATE scheduled_jobs
SET enabled = FALSE,
    lease_owner = NULL,
    lease_until = NULL,
    updated_at = now()
WHERE job_key = 'snapshot-cohort-planning';
COMMIT;
```

只执行与本次配置变更相关的 UPDATE。重新启用后，bootstrap 会把当前定义对应行设回 enabled；账号改名时新 key 会被创建，旧 key 保持手工 disabled。

当前没有 scheduled-job CLI。只读检查可使用：

```sql
SELECT job_key, job_kind, enabled, schedule_seconds, next_run_at,
       lease_owner, lease_until, consecutive_failures,
       last_succeeded_at, last_failed_at, last_error_type
FROM scheduled_jobs
ORDER BY priority DESC, next_run_at;
```

不要直接修改 `next_run_at` 或清零 failures 来隐藏问题；先修复 handler/config，再观察下一轮成功自动重置。

shadow planner 只读核对：

```sql
SELECT status, reason, COUNT(*)
FROM snapshot_cohorts
GROUP BY status, reason
ORDER BY status, reason;

SELECT COUNT(*) AS executable_cohort_tasks
FROM collection_tasks
WHERE snapshot_cohort_id IS NOT NULL;

SELECT COUNT(*) AS executable_comment_scans
FROM comment_scan_runs;

SELECT bvid, next_due_at, last_planned_at, last_checkpoint_hours,
       desired_tier, effective_tier, life_stage, policy_version
FROM video_collection_states
ORDER BY last_planned_at DESC NULLS LAST
LIMIT 50;
```

在纯 C5 shadow 运行中，第二、三个计数都应为 0；若不为 0，先区分测试/手工底层 live 数据，再确认没有绕过 service 配置边界。不要删除 shadow 历史来“清零”。component 的 `extra` 应能看到 `hot_core` / `hot_deep` 固定页范围，以及 latest 的 `max_scan_seconds`、`current_head_required=true`：routine S/A/B/C 热门为 3/2/1/1 页，checkpoint 和首次 active 采纳总量为 20/10/3/1 页，deep 只保存扣除 core 后的余量。

## 6. Comment Collection Scheduling

内置 scheduler 当前不自动周期入队 hot/latest comments。cohort shadow planner 只记录本应采集的组件，不执行它们。长期评论归档仍需要外部 timer 调用 legacy 入队 CLI：

```bash
uv run python main.py video comments <BVID> --mode hot --tier c
uv run python main.py collect-latest-comments <BVID> --max-scan-seconds 55
```

这些命令只执行数据库 enqueue，适合 systemd timer、cron、Windows Task Scheduler 或现有编排平台；不要从 timer 启动第二个 `worker loop`。常驻 worker 全天领取这些任务，评论调度器本身不应增加任何 10:00 到 22:00 的窗口判断。

活动 idempotency key 会吸收重叠调用：上次同目标任务仍 pending/running 时，不会再插入第二条活动任务。任务结束后，下一次 timer 可创建新快照。

C5 的 scan-backed hot/latest task 目前只在底层 live 验收中启用，service 仍保持 shadow。C7 迁移所有权后：

- `hot_core` / `hot_deep` 按最多 10 页、最多 55 秒拆成编号 slice，成功页推进 `next_page_number`。
- latest routine 按有效 interval 得到 10-55 秒 slice，checkpoint/recovery 为 55 秒；成功页通过 CAS 推进 `frontier_states.cursor/version`。
- 一个 BVID 只有一个 active latest scan，其他 cohort component 以 `joined_active_task` 共享，不会创建重叠 baseline/incremental。
- baseline tail 到末尾时自动创建 child head sweep；空 tail 直接建立空 frontier。
- `scan_slice_key` 在所有 task 状态唯一。不要手工复制、删除、改号或直接改 cursor 来“恢复”续片。

届时可用以下只读查询核对逻辑扫描，而不是只看最后一个 task：

```sql
SELECT id, bvid, mode, status, outcome,
       parent_scan_run_id, start_frontier_rpid, result_frontier_rpid,
       target_pages, next_page_number, result_cursor,
       pages_requested, pages_succeeded,
       items_observed, raw_payloads_saved, slice_count,
       last_error_type, updated_at
FROM comment_scan_runs
ORDER BY updated_at DESC
LIMIT 100;

SELECT comment_scan_run_id, scan_slice_no, scan_slice_key,
       status, retry_count, not_before, lease_owner, lease_until
FROM collection_tasks
WHERE comment_scan_run_id IS NOT NULL
ORDER BY comment_scan_run_id, scan_slice_no;

SELECT target_id AS bvid, active_scan_run_id, version, cursor,
       frontier_rpid, frontier_anchor_set,
       last_scan_status, last_scan_pages, last_scan_truncated, extra
FROM frontier_states
WHERE frontier_type = 'latest_comments'
ORDER BY updated_at DESC
LIMIT 100;

SELECT c.id AS component_id, c.cohort_id, c.component_kind,
       c.status, c.scheduled_for, c.deadline, c.finished_at,
       c.comment_scan_run_id, c.failure_reason,
       c.requested_pages, c.succeeded_pages
FROM snapshot_cohort_components c
WHERE c.component_kind IN ('latest_current_head', 'latest_reconciliation')
ORDER BY c.scheduled_for DESC, c.id DESC
LIMIT 200;
```

`paused/time_slice_yield` 是正常非终态。latest 的正常终态包括
`complete/start_anchor_reached`、`complete/frontier_reached` 和
`partial/frontier_missing`；后者表示旧锚点未出现，不等于请求失败。`failed`、
`corrupted`、`start_anchor_missing`、`cursor_loop` 和 `retry_exhausted` 必须结合 task、
coverage、HTTP attempt、raw page 和 scan evidence 检查。

current-head component 只在 `head_captured_at` 落在 `[scheduled_for, deadline)` 时 complete。
到期仍在历史 tail 为 `partial/baseline_tail_in_progress`；其他无有效头页为
`partial/current_head_not_captured`。同一 scan 的不同 cohort 可能一个 complete、一个仍
joined，这是正常的时间窗差异。

若 planned/paused latest scan 缺少对应 numbered task，30 秒 planner 会按稳定 slice key
修复并保留原 priority/budget/retry 参数。若 scan 为 running 且 task/lease 异常，不要
直接改数据库进度：先停止相关 worker、保存 task/scan/frontier/attempt/raw 现场，等待
lease 过期恢复或修复根因。worker 终态失败只在 task 携带的 frontier version 仍匹配时
CAS 清 owner；版本更高说明另一个 worker 已推进，陈旧 worker 不应人工强制覆盖。

PostgreSQL 才是多 worker 验收目标：partial unique index、行锁、savepoint 和 CAS 共同
协调 active latest。SQLite 的 `FOR UPDATE` 语义不足，只允许单进程开发和确定性测试。
可使用 `BOT_TEST_POSTGRESQL_URL` 运行隔离 schema 并发测试；测试会创建并删除随机
schema，绝不能对配置业务 schema 执行 downgrade。

建议按研究优先级设频率：

- 热点事件核心视频：更频繁的热门第 1 页和 latest 增量。
- 普通 active 视频：较低频率。
- legacy baseline 未 complete：重复手工 latest 入队；scan-backed baseline 在 C7 live 后自动 tail -> child head。
- closed/archived 事件：停止外部评论 timer，但保留历史数据。

每次改变频率后同时检查平台请求预算、pending backlog 和 event coverage。完整状态机见 [COLLECTION](COLLECTION.md)。

C6 将补 full/segmented reconciliation、visibility watchlist 和两次独立删除确认；C7 才
启用 live owner 迁移、page-level 短事务、长任务 lease renewal、容量/公平性和 raw
storage gate。C5 完成不等于可以取消现有外部 timer 或打开 `rollout_mode: live`。

## 7. Task Queue Operations

### Inspect

```bash
uv run python main.py task list --limit 50
uv run python main.py task list --status pending --limit 200
uv run python main.py task list --status running --limit 200
uv run python main.py task list --status failed --limit 200
```

关注：

- pending 的 `not_before` 是否在未来，可能是正常退避。
- running 的 `lease_until` 是否长期过期，下一次 worker 会回收。
- retry count 是否接近 max retries。
- 同 target/kind 是否持续失败。

### Retry Failed

先确认根因已经解除，再按最小范围重试：

```bash
uv run python main.py task retry-failed --target-id <BVID> --kind fetch_hot_comments --limit 20
```

不带筛选会重试最多 100 条失败 task：

```bash
uv run python main.py task retry-failed
```

重试不会删除 failed task 的旧 coverage/raw；同一 task 行变回 pending 并清零 retry count。

### Queue Does Not Mean Coverage

`succeeded` 说明 collector 正常返回，不一定说明研究集合完整。例如 latest `frontier_missing` task 自身会完成，但 coverage 是 partial；baseline tail complete 也不是完整 baseline。验收必须同时查 coverage。

## 8. Operational Alerts

默认每 60 秒评估：

| Alert key/type | 默认触发 | Severity |
| --- | --- | --- |
| `worker_heartbeat` | 90 秒无新鲜 worker | critical |
| `task_backlog` | pending >= 1000 或最老 pending >= 900 秒 | warning |
| `request_failure_rate` | 3600 秒窗口至少 20 页且失败率 >= 0.25 | critical |
| `scheduled_job_failure:<key>` | enabled job 连续失败 >= 3 | critical |

状态保存在 `operational_alert_states`。第一次触发、超过 repeat interval 和恢复时通知；默认 notifier 只写日志。服务重启不会丢失 active/resolved 状态。

查看：

```bash
uv run python main.py service status --limit 200
```

处置顺序：

1. 记录 alert key、首次/最近触发时间和 details。
2. 检查对应 service/task/job/raw 配置。
3. 修复根因，不手工改 alert status。
4. 等下一次 evaluation 自动写 resolved 并发恢复通知。

新数据库设置 `operations.alerts.enabled: false` 不会 bootstrap 告警 job，也不会删除历史 alert 行。已有数据库还必须按上一节停用旧 scheduled job，否则旧行不会自动停止。

## 9. Logs

应用当前将 Rich 格式日志写 stdout/stderr，没有内置文件轮转。

Linux systemd：

```bash
journalctl -u books-of-time -f
journalctl -u books-of-time --since "1 hour ago"
```

Docker：

```bash
docker compose --env-file deploy/docker.env logs -f books-of-time
docker compose --env-file deploy/docker.env -f compose.split.yaml logs -f scheduler worker
```

Windows 原生：前台终端直接查看；需要后台托管时，应由 Windows 服务包装器或任务系统负责 stdout/stderr 持久化和轮转。

日志可能包含 BVID、MID、RPID、source URL、错误 response 摘要和本地路径，但登录/status 代码不会打印 Cookie、CSRF 或 refresh token。仍应限制日志访问权限。

## 10. Backup Strategy

### Backup Unit

必须作为一个逻辑单元备份：

1. PostgreSQL。
2. raw filesystem 或 MinIO bucket。
3. local media directory。
4. account `master.key` + encrypted credentials。
5. application commit、Alembic revision 和有效配置。

最简单的一致备份是在维护窗口优雅停止 scheduler/worker，确认没有 running task，再备份数据库和文件。

### Pre-Backup Checks

```bash
uv run python main.py service status --limit 20
uv run python main.py task list --status running --limit 200
uv run alembic current
```

### PostgreSQL

```bash
pg_dump --format=custom --file=books_of_time.dump books_of_time
```

连接参数可使用现有 `PGHOST`、`PGPORT`、`PGUSER`、`PGDATABASE` 和 `.pgpass`。不要把密码直接写进可被 shell history 记录的命令。

### Filesystem

Linux：

```bash
tar -czf books-of-time-files.tar.gz data/raw data/media data/accounts
```

Windows PowerShell 可使用系统 tar：

```powershell
tar -a -c -f books-of-time-files.zip data/raw data/media data/accounts
```

生产部署使用 `/var/lib/books-of-time` 或 `BOT_DATA_DIR` 的实际目录，不要机械复制示例路径。

### MinIO Raw

使用现有对象存储的 versioning/replication/backup 方案备份配置 bucket 和 prefix。数据库 dump 与对象备份必须记录同一维护窗口。media 不在 MinIO，仍需单独备份本地目录。

### Backup Manifest

建议伴随备份记录：

```text
created_at
git_commit
alembic_revision
database_dump_sha256
file_archive_sha256 or MinIO version marker
raw_payload_count
media_asset_count
latest_raw_payload_id
```

## 11. Restore Drill

恢复流程：

1. 停止应用。
2. 恢复匹配版本 PostgreSQL。
3. 恢复 raw/media/accounts 到原路径或与 URI 兼容的挂载路径。
4. checkout 记录的 commit，安装锁定依赖。
5. 检查 `alembic current`；仅在计划升级时执行 upgrade。
6. 运行 doctor。
7. 抽样 raw 和 media hash。
8. 启动服务，运行 health/status。

命令示例：

```bash
pg_restore --clean --if-exists --dbname=books_of_time books_of_time.dump
uv run alembic current
uv run python main.py service doctor
uv run python main.py raw inspect <KNOWN_RAW_ID> --preview-bytes 200
uv run python main.py service run
```

`file://` URI 可能包含相对或绝对路径。恢复到不同工作目录/挂载点时，必须确保 URI 仍能被当前进程解析；项目没有通用的批量 media URI 重写 CLI。

accounts key 和密文必须成对恢复。缺 key、错误 key 或损坏密文会让 credential store 拒绝解密；不要通过删除 key 让服务“重新生成”并覆盖恢复证据。

## 12. Database Maintenance

默认 dry-run：

```bash
uv run python main.py database maintain --output data/maintenance-plan.jsonl
```

计划包含：

- 8 张时间表的 `ANALYZE`。
- `--vacuum` 时改为 `VACUUM (ANALYZE)`。
- 8 个 BRIN index 的 `brin_summarize_new_values`。
- 只有 catalog 确认 parent 已按 captured_at RANGE 分区时才生成月份 partition DDL。

审查后执行：

```bash
uv run python main.py database maintain --execute --output data/maintenance-result.jsonl
```

`--months-ahead` 范围 0–24，默认 3。任一 action 失败后，后续 action 标记 skipped，命令失败。VACUUM 可能占用大量 I/O，应放在低峰窗口并结合 PostgreSQL 自身 autovacuum 状态判断。

当前 `comment_observations` 是普通表，partition action 正常显示 skipped。不要为消除 skipped 而手工伪造 parent；见 [PARTITIONING](PARTITIONING.md)。

## 13. Raw Filesystem To MinIO Migration

迁移只处理 raw，不处理 media。流程必须是：停止服务、备份、dry-run、分批 execute、逐条 hash 回读验证、抽样 inspect、切换 backend。

```bash
uv run python main.py raw migrate-minio --limit 100 --after-id 0
uv run python main.py raw migrate-minio --execute --limit 100 --after-id 0
```

每条记录在数据库 URI 更新前会：

1. 读取并解压本地源，核对 payload SHA-256 和尺寸。
2. 上传确定性 MinIO object。
3. 下载目标并再次核对。
4. 只在目标验证成功后提交 `s3://` URI。

命令不删除本地源。失败行不阻止本批其他行，但 execute 最终非 0 退出。详细切换与回滚见 [DEPLOYMENT](DEPLOYMENT.md#migrate-existing-raw-payloads-to-minio)。

## 14. Capacity Monitoring

项目当前没有自动 raw/media retention 或删除任务。磁盘接近满时不要直接按文件时间删除，因为数据库仍可能引用这些文件。

数据库大小：

```sql
SELECT pg_size_pretty(pg_database_size(current_database()));

SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
```

raw/media 索引量：

```sql
SELECT COUNT(*) AS raw_count,
       SUM(compressed_size) AS compressed_bytes,
       SUM(uncompressed_size) AS uncompressed_bytes
FROM raw_payloads;

SELECT COUNT(*) AS asset_count,
       SUM(size_bytes) AS unique_media_bytes
FROM media_assets;
```

还应从文件系统或 MinIO 实际测量占用，数据库统计不包含孤立文件、目录开销和压缩差异。

容量告警至少覆盖：

- PostgreSQL data volume。
- raw filesystem 或 MinIO quota。
- local media volume。
- backup destination。
- inode/file count。

任何 retention 策略都应先明确 event archive、报告引用、raw evidence 和 media reuse 的保留级别；当前项目没有可安全直接启用的 purge 命令。

## 15. Account Operations

登录命令必须以与服务相同的用户、配置和 accounts 路径运行：

```bash
uv run python main.py login qr
uv run python main.py login status
```

请求前 provider 检查凭据文件 version token，其他进程写入新快照后无需重启 worker。自动 refresh 成功也会原子轮换到新快照。

运维注意：

- 备份 key + ciphertext，缺一不可。
- logout 会删除指定 account 的全部本地 snapshots。
- invalid/缺失 Cookie 会匿名降级，不会停止采集。
- 不要通过多 account 扩大请求预算；当前只启用一个 configured account。

完整说明见 [LOGIN](LOGIN.md)。

## 16. Scaling

### Same Host

split Compose 可在同一主机增加 worker：

```bash
docker compose --env-file deploy/docker.env -f compose.split.yaml up -d --scale worker=3
```

同一 bind mount 让 worker 共享 raw/media/accounts；PostgreSQL 负责 task lease 和 token budget。

### Multiple Hosts

多主机前必须处理本地状态：

- filesystem raw 需要所有 worker 可见的同一路径，或把 raw 切到 MinIO。
- media 必须保持本地文件系统；需要共享 POSIX filesystem，或将 media task 固定到一个能访问该目录的 worker 集合。
- accounts provider 只观察本机文件；各请求主机必须安全共享/同步同一最新凭据快照，否则“最新 Cookie”只在单机范围成立。
- `file://` URI 必须在读取它的主机上可解析。

因此当前最稳妥的规模是单主机多 worker，或 PostgreSQL/MinIO 远端但 application/media/accounts 保持同一主机。跨主机扩展不是只增加副本数即可完成。

### Shared Constraints

- 所有实例 rate-limit 配置一致。
- PostgreSQL connection pool 总量不超过服务器预算。
- 只运行一个 scheduler 是推荐拓扑；job lease 能容错，但不需要主动运行多个 scheduler。
- worker 增加只提高可并行等待/处理能力，不会绕过共享 token bucket。
- media phash 相似分析是全量两两比较，不应随采集 worker 自动横向扩展。

### Changing Shared Rate Rules

`request_budget_states` 不会因 YAML 改动自动更新。修改现有 key 的 rps/burst 时：

1. 停止所有连接该数据库的 worker/scheduler 和会发请求的诊断 CLI。
2. 记录旧/新规则。
3. 在事务中更新每个受影响 key，并把 token 压到新 burst 以内。
4. 部署完全一致的新配置并统一启动。

示例把 media image 改为 `rps=0.1, burst=1`：

```sql
BEGIN;
SELECT *
FROM request_budget_states
WHERE budget_key = 'bilibili:media_image'
FOR UPDATE;

UPDATE request_budget_states
SET refill_rate = 0.1,
    burst = 1,
    tokens = LEAST(tokens, 1.0),
    last_refill_at = clock_timestamp(),
    updated_at = clock_timestamp()
WHERE budget_key = 'bilibili:media_image';
COMMIT;
```

若该 key 尚无行，无需手工 INSERT，下一次请求会按新规则创建。删除 row 也会重建并获得完整 burst，可能造成启动瞬间突发，因此更推荐受控 UPDATE。

## 17. Upgrade And Rollback

### Upgrade

1. 阅读 migration 和 release diff。
2. 运行测试环境 `alembic upgrade head` + `alembic check`。
3. 优雅停止生产服务。
4. 创建完整备份和 manifest。
5. 更新代码，`uv sync --frozen --no-dev`。
6. `uv run alembic upgrade head`。
7. `service doctor`。
8. 启动并执行 health/status/failed task 检查。

### Application Rollback

回滚旧 commit 前确认旧代码能读取当前 schema。不要默认 Alembic downgrade 安全，也不要在没有数据库/文件备份时执行 downgrade。

如果新版本已经写入旧代码不认识的数据：

- 首选恢复升级前数据库 + raw/media/accounts 一致备份。
- 或编写并验证专门兼容迁移。
- 不要只回滚 Python 代码而保留未知 schema。

## 18. Routine Checklists

### Daily

```bash
uv run python main.py service health
uv run python main.py service status --limit 50
uv run python main.py task list --status failed --limit 100
```

- 查看 active alerts。
- 确认 worker heartbeat。
- 检查 oldest pending、failure rate、disk usage。
- 检查重点事件 coverage 和 latest baseline/frontier。

### Weekly

- 生成 database maintenance dry-run。
- 检查 PostgreSQL/raw/media 增长。
- 抽样 `raw inspect` 和 media hash。
- 验证 backup job 成功并记录 hash。
- 检查长期 failed tasks 是否可重试或应解释。

### Monthly

- 做一次隔离 restore drill。
- 核对应用 commit、Alembic revision 和备份 manifest。
- 复评 rate limits、worker 数和数据库 pool。
- 检查 BRIN/ANALYZE 状态和慢查询。
- 审查 active/closed/archived event 及外部评论 timer。

故障症状到具体处理步骤见 [TROUBLESHOOTING](TROUBLESHOOTING.md)。
