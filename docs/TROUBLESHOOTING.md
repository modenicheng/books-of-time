# Troubleshooting

本手册按症状定位 Books of Time 的配置、数据库、服务、任务、平台请求、Cookie、采集、raw/media、分析和部署问题。

## 1. First Five Commands

先在与正式服务相同的配置和环境下运行：

```bash
uv run python main.py service doctor
uv run python main.py service health
uv run python main.py service status --limit 50
uv run python main.py task list --status failed --limit 100
uv run alembic current
```

解释：

- doctor 失败：先修依赖，不要反复启动服务。
- doctor 成功、health 失败：通常是 heartbeat/worker 没运行。
- status 有 active alert：按 alert key 查对应章节。
- failed task 存在：先查错误和 backoff，修复后再重试。
- Alembic revision 不匹配：按部署流程升级或切回匹配代码。

同时记录：应用 commit、配置文件路径、操作系统、部署形态、完整错误类型/消息和发生时间。不要记录 Cookie 值。

## 2. Configuration Problems

### `配置文件 ... 不存在`

确认解析优先级：CLI `--config` > `BOT_CONFIG` > `config/config.yaml`。

Windows PowerShell：

```powershell
Copy-Item config/config.yaml.example config/config.yaml
$env:BOT_CONFIG = "D:\coding\books-of-time\config\config.yaml"
uv run python main.py service doctor
```

Linux：

```bash
cp config/config.yaml.example config/config.yaml
BOT_CONFIG=/opt/books-of-time/config/config.yaml uv run python main.py service doctor
```

全局 `--config` 必须放在子命令前：

```bash
uv run python main.py --config config/production.yaml service doctor
```

### YAML Root/Section Must Be A Mapping

检查缩进、冒号和列表格式。以下是 mapping：

```yaml
database:
  url: postgresql+asyncpg://...
```

不要写成：

```yaml
database: postgresql+asyncpg://...
```

### Boolean Environment Override Rejected

布尔值只接受 `true/false`、`1/0`、`yes/no`、`on/off`，忽略大小写。空字符串不会当作 false。

### Runtime Config Appears Ignored

当前保留但未接入运行时：

- `scheduler.max_retries`
- request tier 的 `per_round`、`latest_pages`、`reply_roots`

只有 tier `hot_pages` 当前影响 `video comments`。详情见 [CONFIGURATION](CONFIGURATION.md)。

`scheduler.discovery_start_hour`、`discovery_stop_hour`、
`discovery_timezone` 和 `discovery_focus_times` 已接入正式 UID discovery handler。
它们不会限制视频快照或 worker 领取其他 collection task。

`BOT_DATABASE_SCHEMA` 也不是普通 runtime engine 的 schema selector，只用于 migration/test 隔离流程。

## 3. Database Connectivity

### Connection Refused / Timeout

检查：

1. PostgreSQL 正在运行并监听目标地址/端口。
2. URL driver 是 `postgresql+asyncpg://`。
3. 用户、密码、database 名正确。
4. Windows firewall/Linux firewall 允许应用主机。
5. Docker 到宿主机使用 `host.docker.internal`，不是容器内的 `127.0.0.1`。
6. `pg_hba.conf` 允许实际来源网段和目标用户/database。

用 PostgreSQL 客户端先验证同一目标：

```bash
psql "postgresql://USER@HOST:5432/books_of_time" -c "select now();"
```

如果密码包含 `@`、`:`、`/`、`#` 等字符，必须做 URL percent-encoding，或使用部署环境支持的安全 secret 方式。

### Authentication Failed

确认连接 URL 实际来自哪个配置源。环境变量 `BOT_DATABASE_URL` 会覆盖 YAML。Docker 可检查最终配置展开：

```bash
docker compose --env-file deploy/docker.env config
```

不要把展开后含密码的输出贴到公开日志。

### Too Many Connections

总连接上限大致受实例数乘以 `pool_size + max_overflow` 影响。降低每实例 pool、减少副本，或提高 PostgreSQL `max_connections` 前先评估内存。split Compose 增加 worker 时尤其要计算总量。

## 4. Schema And Migration

### `schema revision missing; expected ...`

新库运行：

```bash
uv run alembic upgrade head
uv run python main.py service doctor
```

旧版无 `alembic_version` 的开发库先备份，再使用：

```bash
uv run python main.py init-db --adopt-legacy
```

adoption 只接受代码内白名单差异。未知缺表、列、index 或 constraint 被拒绝时，不要强制 stamp；应审计实际 schema 并写针对性 migration。

### `schema revision X; expected Y`

- 数据库落后：停止匹配服务，备份，运行 `alembic upgrade head`。
- 数据库领先：当前代码比数据库旧；切换到匹配 commit 或按经过验证的 rollback 恢复备份。

不要只修改 `alembic_version` 值绕过检查。

### `alembic check` Reports Drift

说明 ORM metadata 与 migration head 不一致。不要在生产用 `create_all` 修补。开发者应生成/审查 revision；operator 应部署包含该 revision 的正确版本。

## 5. Doctor Failures

### `database`

数据库不可达、认证失败或 service table 不存在。先处理第 3/4 节。

### `schema_revision`

见第 4 节。服务不会自动迁移。

### `raw_storage`

filesystem：

- `storage.raw_dir` 不存在时应用会尝试创建。
- 检查父目录权限、只读 mount、磁盘空间和 inode。

MinIO：

- endpoint 不带 `http://`/`https://`。
- `secure` 与 TLS 实际配置一致。
- bucket 存在，或明确设置 `create_bucket=true` 并授予权限。
- access/secret、DNS、证书和网络可用。

### `media_storage`

media 始终是本地目录。检查 `storage.media_dir`、Docker bind mount、systemd `ReadWritePaths`、owner 和剩余空间。

## 6. Health And Heartbeats

### Doctor Passes, Health Says No Fresh Service

`service health` 要求最近 `heartbeat_timeout_seconds` 内有 `status=running` 实例。先启动：

```bash
uv run python main.py service run
```

若已启动：

- 查服务日志是否在 register 后崩溃。
- 确认 health 和服务连接同一数据库。
- 确认系统时钟/NTP 正常。
- 检查 DB 写入是否阻塞。

### No Fresh Worker Heartbeat

仅 scheduler role 不满足 worker health。split 部署检查 worker container/process 是否启动、roles 是否为 `worker`、是否连接同一 DB。

### Old Instance Still Shows Running

强杀进程来不及写 stopped。status 会保留历史行；heartbeat 超时后不再满足 health。当前没有 service instance 清理 CLI，不需要为历史行手工改状态。

### Disabled Alert/Account Job Still Runs Or Fails

当前 bootstrap 只 ensure 新配置仍声明的 job，不自动 disable 缺席定义。关闭 alerts/auto refresh 或更换 account ID 后，先停止 scheduler，再按 [OPERATIONS](OPERATIONS.md#5-scheduled-jobs) 定向停用旧 `operational-alert-evaluation` / `account-cookie-refresh:*` 行。

## 7. Service Startup And Shutdown

### `Service roles must contain worker and/or scheduler`

YAML/环境变量必须至少包含一个合法 role：

```text
BOT_SERVICE_ROLES=worker,scheduler
```

不要加空格之外的未知名称。

### Service Exits Immediately

常见原因：

- doctor 失败。
- heartbeat loop 数据库写入失败。
- scheduler coordinator 意外退出。
- worker loop 出现未处理的基础设施异常。
- 使用了仅测试用的 `--max-worker-iterations`。

检查 service instance 的 `last_error_type/message` 和进程日志。

### Shutdown Takes Up To A Minute

默认给活动 task 60 秒协作式完成。降低 `shutdown_grace_seconds` 会更快取消，但增加 lease 恢复和重复请求概率。Docker/systemd stop timeout 应略大于应用 grace，仓库示例为 70 秒。

## 8. Task Queue

### Enqueued Command Produced No Data

`monitor-video`、`video comments`、`collect-latest-comments` 只入队。确认 worker 运行，或 smoke 中手动执行：

```bash
uv run python main.py worker run-once
uv run python main.py task list --limit 20
```

### Pending Task Never Runs

检查 task：

- `not_before` 是否尚未到达。
- worker heartbeat 是否新鲜。
- task 是否被更高 priority 队列长期压住。
- PostgreSQL/system clock 是否错误。
- request backoff 是否让 retry 延后。

### Running Task Lease Expired

下一次任意 worker `run_once` 会恢复至多 100 条过期 lease。确认原 worker 已停止，避免实际请求仍在运行但 DB lease 已过期。频繁过期通常意味着任务耗时超过 120 秒、数据库阻塞或进程不稳定。

latest task 自身设计为约 55 秒，通常应在默认 120 秒 lease 内完成。

### Failed Task

先从日志/coverage 定位 reason。根因解除后最小范围重试：

```bash
uv run python main.py task retry-failed --target-id <TARGET> --kind <KIND> --limit 20
```

不要周期性无条件重试全部 failed；这会形成风控请求循环并掩盖持续 parse/config 问题。

### `backoff` Filter Shows Nothing

当前 worker 失败重试写为 `pending` 并设置未来 `not_before`。`backoff` 是保留状态值；查看 pending、not-before 和 `service status` 的 active backoffs。

## 9. Rate Limit And HTTP Failures

### `timeout`

单次请求超过 `http.timeout_seconds`。检查网络和平台状态；不要为 latest 55 秒时间片把单请求 timeout 也盲目设为 55 秒。持续 timeout 应降低调用频率、排查 DNS/TLS/代理，而不是并发重试。

### `403`

HTTP 403 且 body 未命中 captcha/验证码/风控词。可能是权限、请求上下文或平台策略。检查 Cookie 状态、时间、User-Agent 和平台可用性；等待 backoff 后小范围重试。

### `captcha`

HTTP body 含 captcha、验证码、风控，或 status 412。停止激进重试，延长等待并人工验证账号/网络状态。项目不会绕过验证码、切换代理池或轮换账号池。

### `429`

平台明确限流。优先降低 `rate_limit` rps/burst 和外部 comment timer 频率。`Retry-After` 是纯数字秒时会被采用，否则使用默认退避。

### `5xx`

平台服务端错误。保留退避，观察是否恢复。不要立即把 worker 数加大。

### `parse_error`

HTTP 返回但 JSON/schema 与 parser 预期不符，或 collector 无法解析关键字段。可能是平台接口变化、错误 body 未被 HTTP 分类、或数据边界。记录 request type、status 和错误消息，检查同一时段其他 task。

注意：当前统一 HTTP client 对 403/429/captcha/5xx 在 collector 归档前抛出异常；coverage 会记录 request error，但该失败 response body 当前不一定进入 raw storage。成功返回后发生的 parser failure 通常已有 raw。

### Distributed Rate Rule Mismatch

同一 PostgreSQL 中 `request_budget_states` 的 refill/burst 与新进程配置不一致时会明确失败。只让所有实例重启不够，因为旧规则仍在数据库。停止全部请求进程，按 [OPERATIONS](OPERATIONS.md#changing-shared-rate-rules) 受控更新受影响 bucket，再以同一配置统一启动。不要在服务运行中临时改 token 来扩大预算。

## 10. Cookie And QR Login

### QR Does Not Render Correctly

使用支持等宽字符和足够宽度的真实终端，不要把扫码命令运行在吞掉 ANSI/字符宽度的日志收集器里。Docker 使用交互终端：

```bash
docker compose --env-file deploy/docker.env exec books-of-time /app/.venv/bin/python main.py login qr
```

### QR Expired Or Login Timed Out

重新运行 `login qr`，扫码后还要在手机端确认。可合理提高 `--timeout-seconds`；值必须大于 0。

### Missing Required Cookie Fields

登录结果必须包含 `SESSDATA`、`bili_jct`、`DedeUserID`、`ac_time_value`。缺失说明 QR 流程未完整确认或平台返回变化，凭据不会被保存。

### Status Is Anonymous

- 从未登录。
- `--account` 与 `active_account_id` 不同。
- `accounts.enabled=false` 时 HTTP provider 不加载凭据，但 `login status` 仍按指定文件查询。
- 服务和 CLI 使用不同 credentials path。

### Status Is Invalid

定时 refresh check 已确认 Cookie 无效。HTTP 请求会退回匿名。重新扫码登录会创建并激活新 snapshot。

### Credential Store Cannot Be Decrypted

检查 key 与 ciphertext 是否成对、是否从同一备份恢复、文件是否被截断。不要删除 key 后重试；这只会让已有密文永久不可读。恢复正确文件，或在确认不需旧凭据后执行受控 logout/重新登录。

### New Login Not Used By Worker

每次请求会比较 credentials/key 文件 mtime/size version token。确认：

- 登录和 worker 指向同一真实文件。
- 容器/主机共享同一 accounts mount。
- 文件替换在目标文件系统上可见。
- snapshot health 不是 invalid。

无需重启服务；若多主机各自使用本地文件，则必须安全同步，当前 provider 不通过 PostgreSQL广播 Cookie。

## 11. Discovery And Video Metrics

### Event UID Finds No Videos

确认：

- event status 是 active。
- 当前时间不早于 start、不晚于 end。
- UID target active 且是正整数。
- scheduler 和 worker 都运行。
- `discover_user_videos` task/coverage 成功。
- 目标用户最新投稿位于第 1 页；正式 collector 当前只扫第 1 页、每页 10 条。

`discover-user` diagnostic 不合并 event link，因此不能替代正式 event UID discovery。

### Static Discovery Loop Has No Raw/Coverage

这是当前设计：`discovery loop` 和 `discover-user` 是诊断入口。正式使用 `service run` scheduler -> task -> worker。

### Manual Video Is Not Automatically Re-Snapshotted

`monitor-video` 不会把 BVID 插入 `known_videos`。自动 sweep 面向 discovery 发现的 known video。需要长期跟踪手工 BVID 时，应通过 event seed/UID discovery 纳入范围，或由外部 timer 周期调用 `monitor-video`。

### Video Has Availability But No Metrics

平台明确返回不可用状态时 collector 只保存 availability/raw/coverage，不写 metric/info，也不继续快照。检查最新 `video_availability_snapshots` 的 code/message。

## 12. Hot Comments And Replies

### `--page-limit` Did Not Return N Comments

它表示请求页数，不是评论条数。每页数量和空页由平台决定。多页之间也可能因动态排序重复/跳动。

### Hot Output Changes Rapidly

热门评论是动态排序。系统保留每次 page observation 和 comment observation；换血分析只比较第 1 页 Top N。不要把 page 2 的位置与另一次采集 page 1 拼成平台原子快照。

### No Reply Tasks

只有根评论满足 watchlist 信号才派生一页楼中楼。检查热门位置、相邻 like/reply 增量、controversy keywords 和评论是否 root。当前无公开 watchlist CLI，可用只读 SQL 检查表。

### Reply Task Only Has One Page

当前 watchlist payload 固定 `page_limit=1, page_size=20`。这是重点证据采样，不是完整楼中楼遍历。

## 13. Latest Baseline And Frontier

### `baseline_paused` / `reason=time_budget`

正常可恢复中间状态。collector 已保存 cursor 并自动入队 follow-up。确认队列中存在 latest follow-up、worker 继续运行。

### `baseline_tail_complete`

只表示历史 tail 已到达。需要再入队一次 latest，执行 head sweep 并遇到开始锚点后才是 `baseline_complete`。

```bash
uv run python main.py collect-latest-comments <BVID> --max-scan-seconds 55
```

### `baseline_corrupted` / `corrupted`

通常是同 cursor 重试耗尽或 cursor loop。该 collector 会提交 corrupted coverage，不把 task 标为 failed，也不会自动继续。

先保存数据库/raw 备份并查 `frontier_states.extra`、coverage reason 和日志。当前没有公开 frontier reset CLI。若确认要放弃损坏进度并从头重建该视频 latest baseline，先确保没有活动 latest task，再在事务中定向重置唯一 frontier 行：

```sql
BEGIN;
SELECT *
FROM frontier_states
WHERE target_type = 'video'
  AND target_id = '<BVID>'
  AND frontier_type = 'latest_comments'
FOR UPDATE;

UPDATE frontier_states
SET frontier_rpid = NULL,
    frontier_time = NULL,
    cursor = NULL,
    last_scan_at = NULL,
    last_scan_status = NULL,
    last_scan_pages = 0,
    last_scan_truncated = FALSE,
    extra = '{}'::jsonb,
    updated_at = now()
WHERE target_type = 'video'
  AND target_id = '<BVID>'
  AND frontier_type = 'latest_comments';
COMMIT;
```

然后重新入队 baseline。该操作保留旧 raw/observations/coverage，但丢弃可恢复 cursor 状态；只应在审计后针对单一 BVID 执行。

### `frontier_missing`

完整扫描到服务端尾部仍未找到曾见 frontier。系统记录 partial coverage、`missing_after_seen` visibility event，并把本轮头部更新为新 frontier。它不是请求失败，也不证明平台删除；可能涉及折叠、删除或平台集合变化。

### Repeated Latest Enqueue Does Nothing

同一视频 manual/latest follow-up 有活动 idempotency key 时 enqueue 返回现有 task。等待它成功/失败，或检查其 not-before/lease。

## 14. Coverage

### No Coverage Rows

- task 尚未由 worker 执行。
- 查询 BVID/target 不一致。
- diagnostic discovery path 不写 coverage。
- 数据库配置指向另一实例。

### Succeeded Task But Partial Coverage

task status 和 evidence completeness 是不同维度。time budget、frontier missing、truncated 会生成 partial；corrupted 会生成 corrupted。

### `coverage --limit` Behavior

默认 20，当前 CLI 不做 1–200 clamp，直接传给 repository。使用正整数；过大值可能产生慢查询/大量日志，负数在不同数据库方言下行为也不一致。

### Failure Rate Looks Wrong

status/alert 使用 coverage 的 `sum(request_errors) / sum(pages_requested)`，不是 failed task count。某些在请求前失败的 task pages requested 可能为 0；结合 parse errors、failed task 和 reason 一起判断。

## 15. Raw Evidence

### `Raw payload not found`

ID 不存在。当前命令记录提示并正常返回，不把它当成进程错误。核对 report evidence ID 和数据库。

### File Not Found

数据库 URI 指向的文件未恢复、工作目录改变、mount 不一致或文件被手工删除。先停止 destructive cleanup，核对备份和 `storage_uri`。

### Zstd Decompression Error

raw object 损坏或不是预期 zstd frame。从备份恢复，并核对数据库 `payload_hash`、compressed/uncompressed size。`raw inspect` 会读取/解压和显示元数据，但不会自动重新计算并比较 SHA-256；MinIO migration 才执行前后 hash/size 强校验。

### `Unsupported raw payload storage URI`

当前 router 只支持已配置 reader 的 `file://` / `s3://`。检查 backend factory、MinIO 配置和 URI scheme。切换 primary backend不会自动重写历史 URI。

### MinIO Migration Fails One Row

日志会给 source/target/error。修复本地源损坏、bucket 权限、网络或目标回读问题后，使用覆盖该 ID 的 `--after-id`/limit 批次重跑。执行失败不会更新该行 URI，也不会删除源文件。

## 16. Media

### Media Source Stays Pending

确认 `fetch_media_asset` task 已入队且 worker 可执行，`bilibili:media_image` rate limit 不为异常低值，图片 host 可访问。

### Media Source Is Failed

查看 task/coverage/log 和 `media_sources.fetch_error_type/message`。修复网络/风控后：

```bash
uv run python main.py task retry-failed --target-id <MEDIA_SOURCE_ID> --kind fetch_media_asset --limit 1
```

source 在重试成功后会清除错误并回填所有关联 link。

### Asset Metadata Is NULL

Pillow 无法解码 bytes 时，blob 仍可作为 asset 保存，但 width/height/pixel hash/phash 为空；MIME/ext 可能来自 response header 或 URL。先用 raw payload 和实际文件确认它是否真是图片、动图/新格式或错误 body。

### `dhash` / `ahash` Are NULL

这是当前审计后的预期状态。schema 保留列，但 downloader 只写 pixel SHA-256 和 phash。

### Duplicate Images Still Occupy More Raw Space

blob SHA-256 只对 media asset/file 强去重。每次独立图片 HTTP response 仍可保存 raw 证据，因此 raw storage 可能有重复 bytes。

### File Exists On One Worker But Not Another

media 是 local filesystem。多主机 worker 必须共享同一路径，或只让能访问该目录的 worker处理 media task。数据库中的 `file://` URI不会传输图片内容。

### Similarity Tables Are Empty

`analyze_similar_media` handler 已实现，但当前没有公开 CLI 或默认 scheduled job 自动入队。普通采集只计算 phash，不自动生成 edge/cluster。

### Media Changed But No Media Event

当前 event 比较只覆盖前后都有非空 media source 列表的 observation。无图 -> 有图或有图 -> 完全无图不会生成 media_added/media_removed；直接比较 `comment_observations.media_*_hash` 和 `comment_observation_media` link。source URL 改变但 asset blob 相同也可能表现为 source-level 指纹变化。

## 17. Event And Analysis

### Event Not Found

事件可用数字 ID 或 slug。slug 是小写字母/数字/单连字符；创建后不可改。先运行：

```bash
uv run python main.py event list --limit 100
```

### Video/Keyword Filter Rejected

`event report --bvid/--keyword` 只接受当前 active event 范围。检查：

```bash
uv run python main.py event list-videos <EVENT> --all
uv run python main.py event list-targets <EVENT> --all
```

恢复关联/target，或移除筛选。停用历史仍保留，但不进入 active 分析。

### Analysis Output Is Empty

按顺序检查：

- 窗口 offset 和 `[since, until)` 是否正确。
- event 是否有 active videos。
- 窗口内是否有 observations/snapshots。
- keyword 是否 active 且实际命中。
- hot turnover 是否至少有两张成功第 1 页。
- template 是否至少涉及两个视频。
- propagation template edge 前是否运行 refresh flags。

特别地，`template-candidates --bvid` 把范围缩到一个视频，而算法只比较跨视频，因此当前会为空。

### `exceeds max_*; narrow the window`

分析拒绝静默截断。优先缩短时间窗、按 BVID/keyword 筛选或增大 bucket；只有在评估内存/运行时间后才提高 max 上限。

### Datetime Must Include Offset

不要传 `2026-07-13T00:00:00`。使用 `2026-07-13T00:00:00+08:00` 或 `2026-07-12T16:00:00Z`。

### Stance Lexicon Error

- version 必须非空。
- 只支持 support/criticism/neutral。
- 每类必须是字符串列表。
- NFKC/casefold 后相同 term 不能跨类别。

修改词项或类别含义时更新 version，避免把不同规则结果混在一起。

### Propagation Originator/Amplifier All Zero

这些分数依赖窗口内 `template_like_comment` flags。先运行 `event refresh-comment-flags`，并确认 template candidates 存在。bridge/responder/official 不依赖 template flags。

反过来，旧 flag 也不会被 refresh 自动删除。若结果包含非预期 originator/amplifier，按输出中的 flag ID 检查 algorithm version、evidence 窗口和相关评论；当前传播分析不会按 flag detected time 自动隔离版本。

### Report Fails Though Individual Commands Work

report 对多个章节共用 `max_records`，还限制 `max_videos`。timeline、observations、hot changes、keyword points 或 template candidates 任一超限都会整体失败。缩小窗口/筛选，或有意识地提高 limit。

### Result Changed After Rerun

数据库可能新增 observations，active event scope/keywords 或配置词表可能变化。记录 commit、Alembic revision、参数、配置 version、coverage 和数据库快照。严格复现需要同一证据快照，不只是同一命令。

## 18. Docker

### Compose Says Required Database URL Missing

复制并编辑 env：

```bash
cp deploy/docker.env.example deploy/docker.env
docker compose --env-file deploy/docker.env config
```

`BOT_DATABASE_URL` 是必填插值。

### Container Cannot Reach Host PostgreSQL

- URL host 使用 `host.docker.internal`。
- Compose 已添加 `host-gateway`，但 PostgreSQL 仍需监听 bridge 可达地址。
- `pg_hba.conf` 允许 Docker bridge 网段。
- 宿主机 firewall 允许端口。

### Permission Denied Under `/var/lib/books-of-time`

容器以非 root `books-of-time` 用户运行。预创建 `${BOT_DATA_DIR}/raw|media|accounts` 并给 bind mount 合适 owner/ACL。Windows Docker Desktop 还需允许对应 drive/path 文件共享。

### Split Scheduler Is Unhealthy

health 要求数据库中有新鲜 worker heartbeat。确认至少一个 worker 副本 running。scheduler-only 拓扑不能满足完整服务健康契约。

### Image Starts But Migration Missing

容器启动不会自动迁移。启动前运行：

```bash
docker compose --env-file deploy/docker.env run --rm books-of-time /app/.venv/bin/alembic upgrade head
```

## 19. Linux systemd

### `ExecStartPre` Fails

unit 会先运行 `service doctor`。检查：

```bash
systemctl status books-of-time
journalctl -u books-of-time -n 200 --no-pager
```

确认 `/etc/books-of-time/books-of-time.env` 权限和内容、`/opt/books-of-time/.venv` 存在、`/var/lib/books-of-time` 可写、migration 已显式执行。

### Read-Only Filesystem Error

示例 unit 使用 `ProtectSystem=strict`，只允许 `ReadWritePaths=/var/lib/books-of-time`。将 raw/media/accounts 配置到该目录；不要期待服务写 `/opt/books-of-time/data`。

### Restart Loop

`Restart=on-failure` 每 5 秒重试。先 stop service，修复 doctor/schema/config，再 start，避免持续刷日志和数据库连接。

## 20. Windows Development

### PowerShell Environment Override

```powershell
$env:BOT_DATABASE_URL = "postgresql+asyncpg://user:password@127.0.0.1:5432/books_of_time"
uv run python main.py service doctor
```

环境变量只对当前 PowerShell 会话和其子进程生效。

### Ctrl+C Does Not Stop Immediately

等待活动 task 的 grace period。不要连续关闭终端进程；先观察 stopping/stopped。必要时另开终端运行 status。

### Path Or Encoding Issues

- 配置文件用 UTF-8。
- 文档命令不要使用 Bash heredoc。
- 相对 `file://data/...` URI 依赖服务 working directory，长期运行建议使用稳定绝对 data path。
- Git 行尾保持 LF，避免批量 CRLF churn。

## 21. Escalation Data

仍无法定位时，准备以下非敏感信息：

```text
git rev-parse HEAD
uv run alembic current
service doctor output
service health output
service status output
failed task id/kind/target/retry/not_before
coverage status/reason/error counts
scheduled job key/failure type
operating system and deployment topology
```

可以附 raw payload ID、BVID/RPID/MID 供核验，但不要附 Cookie、CSRF、refresh token、数据库密码、MinIO secret 或 account key。
