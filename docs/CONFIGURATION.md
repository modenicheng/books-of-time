# Configuration Reference

本项目使用 YAML 作为完整配置源，并允许部署环境通过有限的 `BOT_*` 环境变量覆盖敏感或实例相关字段。模板位于 `config/config.yaml.example`。

## Resolution Order

配置文件路径按以下优先级选择：

1. CLI 全局参数 `--config <PATH>`。
2. 环境变量 `BOT_CONFIG`。
3. 仓库默认路径 `config/config.yaml`。

选定 YAML 后，再应用受支持的环境变量覆盖。环境变量不会替换整个配置树，只修改明确列出的键。

示例：

```bash
uv run python main.py --config config/production.yaml service doctor
```

Windows PowerShell 临时覆盖：

```powershell
$env:BOT_DATABASE_URL = "postgresql+asyncpg://user:password@127.0.0.1:5432/books_of_time"
uv run python main.py service doctor
```

不要提交本地 `config/config.yaml`、数据库密码、MinIO secret 或账号凭据。

## Database

```yaml
database:
  url: postgresql+asyncpg://user:password@127.0.0.1:5432/books_of_time
  pool_size: 5
  max_overflow: 10
  pool_pre_ping: true
  echo: false
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `url` | 无 | 必填。运行服务使用 async SQLAlchemy URL；生产使用 `postgresql+asyncpg://` |
| `pool_size` | `5` | 常驻连接池大小 |
| `max_overflow` | `10` | pool 满时允许的临时连接数 |
| `pool_pre_ping` | `true` | checkout 前检测失效连接 |
| `echo` | `false` | 输出 SQLAlchemy SQL，包含参数形状，通常只用于本地调试 |

PostgreSQL 才提供跨进程 task lease、scheduled job lease 和共享 token bucket。SQLite 仅用于测试和单进程开发。

## Storage

```yaml
storage:
  backend: filesystem
  raw_dir: ./data/raw
  media_dir: ./data/media
  minio:
    endpoint: ""
    access_key: ""
    secret_key: ""
    bucket: books-of-time-raw
    prefix: raw
    secure: true
    create_bucket: false
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `backend` | `filesystem` | 只控制 raw payload，允许 `filesystem` 或 `minio` |
| `raw_dir` | `./data/raw` | filesystem raw 根目录，也是 MinIO 模式读取历史 `file://` URI 的回退目录 |
| `media_dir` | `./data/media` | media asset 本地根目录；不受 raw backend 影响 |
| `minio.endpoint` | 空 | `host:port`，不要带 `http://` |
| `minio.access_key` | 空 | MinIO access key |
| `minio.secret_key` | 空 | MinIO secret key |
| `minio.bucket` | `books-of-time-raw` | raw 对象 bucket |
| `minio.prefix` | `raw` | 对象 key 前缀 |
| `minio.secure` | `true` | 是否使用 TLS |
| `minio.create_bucket` | `false` | 是否允许应用创建 bucket；生产建议预创建并保持 `false` |

切换到 MinIO 后，新 raw 写入 `s3://` URI，router 仍能读取旧 `file://` URI。历史迁移方法见 [DEPLOYMENT](DEPLOYMENT.md#migrate-existing-raw-payloads-to-minio)。media 始终保存在本地文件系统。

## Accounts

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

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | 是否加载本地凭据并注册自动刷新 job；`false` 时始终匿名 |
| `active_account_id` | `default` | 当前单账号 ID；是未来多账号兼容点，不是 Cookie 池 |
| `credentials_path` | `./data/accounts/credentials.enc` | 加密快照文件 |
| `key_path` | `./data/accounts/master.key` | 本地加密 key，必须与密文一起备份 |
| `history_limit` | `5` | 保留的凭据快照数量 |
| `auto_refresh` | `true` | 是否启用 scheduler Cookie refresh job |
| `refresh_check_seconds` | `21600` | 刷新检查周期，scheduler 最小按 60 秒处理 |

登录本身是独立 CLI，不依赖 PostgreSQL。详见 [LOGIN](LOGIN.md)。

当前 job reconciliation 边界：从未 bootstrap 的数据库中，`enabled: false` / `auto_refresh: false` 不会创建 refresh job；但如果数据库已经存在旧的 `account-cookie-refresh:*` job，后续配置关闭或更换 account ID 不会自动停用缺席的旧 job。变更这两项时按 [OPERATIONS](OPERATIONS.md#5-scheduled-jobs) 停 scheduler 并核对/停用旧 job。

## HTTP

```yaml
http:
  timeout_seconds: 10
  user_agent: "BooksOfTime/0.1 research collector"
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `timeout_seconds` | `10` | 单次 HTTP 请求超时；latest 的 55 秒是整个任务片段预算，不替代此超时 |
| `user_agent` | 项目默认 UA | 所有统一 HTTP 请求的 User-Agent |

正式 worker 请求会额外写入 `http_request_attempts`。该行为不需要配置开关：
成功响应在 collector 保存 raw 后才标记为 `succeeded`；403、429、captcha、5xx
等带 body 的失败响应先保存 raw 再抛错；timeout/network 只记录无 body attempt。
登录、Cookie 刷新和诊断入口不在 collection worker context 内，不会被强制绑定到
collection task。

## Rate Limits

```yaml
rate_limit:
  global: {rps: 1.0, burst: 3}
  host:bilibili: {rps: 0.8, burst: 2}
  bilibili:video_stats: {rps: 0.5, burst: 2}
  bilibili:user_video_list: {rps: 0.2, burst: 1}
  bilibili:comment_hot: {rps: 0.2, burst: 1}
  bilibili:comment_latest: {rps: 0.2, burst: 1}
  bilibili:comment_reply: {rps: 0.1, burst: 1}
  bilibili:media_image: {rps: 0.2, burst: 1}
```

每个规则都包含：

- `rps`：每秒补充 token 数，必须大于 0。
- `burst`：bucket 最大 token 数，必须至少为 1。

一次 Bilibili 请求会同时获取 `global`、`host:bilibili` 和具体 request type 三层 token。没有配置的 key 不额外限速，但仍受已配置的 global/host 层约束。

PostgreSQL 模式使用 `request_budget_states` 和行锁原子保留多层 token，多个 worker 共享额度。同一数据库上的所有实例必须使用一致规则；数据库中已存在规则与进程配置不一致时，请求会明确失败，不会静默扩大预算。SQLite 使用进程内 token bucket。

## Scheduler

```yaml
scheduler:
  lease_seconds: 120
  default_retry_delay_seconds: 300
  max_retries: 3
  discovery_scan_seconds: 60
  discovery_start_hour: 10
  discovery_stop_hour: 22
  discovery_timezone: Asia/Shanghai
  discovery_focus_times: ["11:00", "12:00", "13:00", "18:00", "19:00", "19:30", "20:00"]
```

当前生效字段：

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `lease_seconds` | `120` | collection task worker lease 时长 |
| `default_retry_delay_seconds` | `300` | 普通 collector exception 的任务重试延迟，以及 scheduled job 失败重试延迟 |
| `discovery_scan_seconds` | `60` | UID discovery scheduled job 和诊断 discovery loop 的默认周期；服务值必须为 1 到 60 秒，以覆盖每个重点分钟 |
| `discovery_start_hour` | `10` | 自动 UID discovery 的本地起始小时，包含该小时 |
| `discovery_stop_hour` | `22` | 自动 UID discovery 的本地停止小时，不包含该小时 |
| `discovery_timezone` | `Asia/Shanghai` | 发现窗口和重点分钟使用的 IANA 时区 |
| `discovery_focus_times` | 见示例 | 严格 `HH:MM` 列表；每个重点时点生成 T+0 和 T+30 秒两次 discovery，优先级从 110 提升到 120，并写入审计 payload |

当前保留但未接入运行时的字段：

- `max_retries`：新 task 当前仍使用 repository 默认值 3；此键不会全局改写 task。

自动窗口只约束 `service run` 的持久化 UID discovery job。显式执行的
`discovery loop` 是诊断入口，不会被窗口静默拦截。视频指标 sweep 和已入队的
评论、回复、media、重试任务不读取这些字段，全天都可运行。

重点补检查固定为 30 秒，不单独配置。若 scheduler 晚于重点时点才执行 handler，
主任务立即变为可执行，补任务仍至少比主任务的 `not_before` 晚 30 秒。

## Service

```yaml
service:
  roles: [worker, scheduler]
  worker_idle_sleep_seconds: 5
  scheduler_idle_sleep_seconds: 1
  scheduler_lease_seconds: 60
  heartbeat_seconds: 10
  heartbeat_timeout_seconds: 30
  request_failure_window_seconds: 3600
  shutdown_grace_seconds: 60
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `roles` | `[worker, scheduler]` | 当前进程角色；只能包含 `worker`、`scheduler` |
| `instance_id` | 主机名 | 可由环境变量注入的实例名前缀，运行时再追加 PID 和随机后缀 |
| `worker_idle_sleep_seconds` | `5` | worker 空队列轮询间隔 |
| `scheduler_idle_sleep_seconds` | `1` | scheduled job coordinator 空闲轮询间隔 |
| `scheduler_lease_seconds` | `60` | scheduled job lease 时长 |
| `heartbeat_seconds` | `10` | service instance heartbeat 周期 |
| `heartbeat_timeout_seconds` | `30` | `service health` 判断 heartbeat 新鲜度的阈值 |
| `request_failure_window_seconds` | `3600` | `service status` 请求失败统计窗口，范围 60 到 604800 秒 |
| `shutdown_grace_seconds` | `60` | 收到停止信号后等待协作式退出的上限 |

split Compose 会覆盖 roles：scheduler 容器只运行 scheduler，worker 容器只运行 worker。

## Operational Alerts

```yaml
operations:
  alerts:
    enabled: true
    evaluation_seconds: 60
    worker_heartbeat_timeout_seconds: 90
    pending_task_threshold: 1000
    oldest_pending_seconds: 900
    request_failure_window_seconds: 3600
    request_failure_min_pages: 20
    request_failure_rate: 0.25
    scheduled_job_failure_threshold: 3
    repeat_notification_seconds: 3600
```

| 键 | 作用 |
| --- | --- |
| `enabled` | 创建并执行持久化告警评估 job |
| `evaluation_seconds` | 评估周期 |
| `worker_heartbeat_timeout_seconds` | 没有新鲜 worker heartbeat 的触发阈值 |
| `pending_task_threshold` | pending 数量达到此值时触发 backlog 告警 |
| `oldest_pending_seconds` | 最老 pending 年龄达到此值时触发 backlog 告警 |
| `request_failure_window_seconds` | 请求失败率统计窗口 |
| `request_failure_min_pages` | 样本页数不足时不判断失败率 |
| `request_failure_rate` | 触发阈值，范围 `(0, 1]` |
| `scheduled_job_failure_threshold` | enabled job 连续失败触发阈值 |
| `repeat_notification_seconds` | active 告警重复通知间隔 |

除 `enabled` 和 `evaluation_seconds` 外，未知 alert 键会被拒绝。默认 notifier 只写日志；状态持久化在 `operational_alert_states`。

在新数据库上 `enabled: false` 不会创建告警 job。若 `operational-alert-evaluation` 已由旧配置 bootstrap，当前 coordinator 不会因定义缺席而自动把数据库行设为 disabled；关闭功能时还需执行 [OPERATIONS](OPERATIONS.md#5-scheduled-jobs) 的已有 job 停用步骤。

## Latest Comments

```yaml
latest_comments:
  max_scan_seconds: 55
  page_retry_attempts: 3
  page_retry_backoff_seconds: [1, 3, 5]
```

| 键 | 作用 |
| --- | --- |
| `max_scan_seconds` | 旧式手工 latest task 的默认总时间片；CLI 非默认 `--max-scan-seconds` 会写入 task payload 覆盖它 |
| `page_retry_attempts` | 同一 cursor 的最大请求尝试次数 |
| `page_retry_backoff_seconds` | 每次重试前的等待序列，超过列表后使用最后一个值 |

旧式手工 task 没有 `comment_scan_run_id`，继续使用兼容 collector 和上述
`max_scan_seconds`。C5 的 scan-backed cohort task 会把时间片直接写入 payload：routine
使用 `min(55, max(10, floor(effective_interval_seconds * 0.4)))`，因此 60 秒间隔为
24 秒、120 秒间隔为 48 秒，138 秒及以上为 55 秒；checkpoint/recovery 固定 55 秒。
这不是单次 HTTP timeout，仍受 `http.timeout_seconds` 约束。

时间片耗尽会 CAS 保存 cursor、retry progress 和 frontier version，并使用
`<scan_id>:<mode>:<slice_no>` 派生全状态唯一 follow-up。同一 cursor 的失败次数跨 slice
延续；尝试耗尽或 cursor loop 会把逻辑 scan 标为 corrupted，并保持正式 frontier
不变。成功重试会携带最新 CAS 状态继续持久化页面，不依赖 ORM 对象是否原地刷新。

## Request Budget Tiers

```yaml
request_budget:
  c:
    per_round: 3
    hot_pages: 1
    latest_pages: 0
    reply_roots: 0
```

当前只有 `hot_pages` 生效：手工执行 `video comments --tier <s|a|b|c>` 且没有显式 `--page-limit` 时读取对应值，并保证至少 1 页。它只控制旧式单任务热门采集，不控制 snapshot cohort 的 `hot_core` / `hot_deep` 扫描；后者使用下面版本化的 `snapshot_cohorts.hot_comments`。

`per_round`、`latest_pages` 和 `reply_roots` 当前是保留的规划元数据，不会限制 collector。latest 使用时间片和 frontier；重点回复由 watchlist 自动派生。不要依赖这些保留字段施加请求上限。

## Snapshot Cohort Policy

```yaml
snapshot_cohorts:
  enabled: false
  policy_version: cohort-default-v2
  rollout_mode: shadow
  planning_seconds: 30
  timezone: Asia/Shanghai
  checkpoint_hours: [6, 12, 18, 24]
  checkpoint_max_lateness_minutes: 60
  downgrade_confirmations: 2
  tier_policy:
    official_s_age_hours: 6
    reassess_after_24h_minutes: 60
    hot_turnover_confirmations: 2
    s: {view_growth_per_hour: 6000, comment_growth_per_hour: 60, hot_top20_turnover_ratio: 0.35}
    a: {view_growth_per_hour: 1200, comment_growth_per_hour: 20, hot_top20_turnover_ratio: 0.20}
    b: {view_growth_per_hour: 300, comment_growth_per_hour: 5}
  lifecycle:
    dormant_after_days: 7
    archive_after_days: 30
    dormant_interval_minutes: 1440
    archived_metric_probe_minutes: 10080
  activity_windows:
    defaults:
      - {name: lunch, start: "11:30", end: "13:30"}
      - {name: dinner, start: "17:30", end: "20:30"}
      - {name: night, start: "21:30", end: "00:30"}
  tier_intervals_minutes:
    s: {active: 2, normal: 10}
    a: {active: 10, normal: 30}
    b: {active: 30, normal: 60}
    c: {active: 60, normal: 120}
  hot_comments:
    routine_pages: {s: 3, a: 2, b: 1, c: 1}
    checkpoint_pages: {s: 20, a: 10, b: 3, c: 1}
    max_pages_per_slice: 10
    max_slice_seconds: 55
```

C5 已把分层热门页目标、持久 latest scan、CAS frontier、多锚点自动 baseline 和编号切片接入底层 planner/worker，但当前服务仍只允许 `shadow`。模板保持 `enabled: false`，便于升级后先完成 migration、doctor 和容量检查；改为 `true` 后会注册一个 30 秒 planner job，写入 policy、video state、cohort、component 和 schedule gap 证据，但不会创建 `comment_scan_runs`、`collection_tasks`，也不会发起 Bilibili 请求。

`rollout_mode: live` 在 C5 会让 `service run` 启动失败并给出 C7 所有权迁移提示。这是刻意的安全边界：现有 `video_snapshot_sweep`、日终任务和外部评论定时器仍在运行，提前启用 live 会造成重复采集。底层 live 物化、编号续片、单 BVID latest owner、共享 cohort 扇出和 worker 终态清理已经有测试覆盖，只供 C7 迁移复用，不是当前运维开关。

| 键 | 含义 |
| --- | --- |
| `enabled` | 是否注册持久 planner job；C5 启用后仍只运行 shadow |
| `policy_version` | 不可变策略内容的全局版本 ID；默认 `cohort-default-v2` |
| `rollout_mode` | C5 仅允许 `shadow`；`live` 留给 C7 所有权迁移 |
| `planning_seconds` | planner 的固定评估周期；默认 30 秒 |
| `timezone` | 活跃窗口使用的 IANA 时区；持久化时间仍为 UTC |
| `checkpoint_hours` | 从不可变 Bilibili `pubdate` 起算的强制年龄点，必须为严格递增正整数 |
| `checkpoint_max_lateness_minutes` | checkpoint 允许开始请求的最大延迟 |
| `downgrade_confirmations` | effective tier 降级所需连续相同评估次数；升级立即生效 |
| `official_s_age_hours` | monitored official 视频从发布时刻起自动为 S 的时长；发现时间不重置 |
| `hot_turnover_confirmations` | turnover 信号可参与评级前所需连续完整 hot pair 数 |
| `reassess_after_24h_minutes` | 24 小时后的默认评级复评间隔 |
| `lifecycle.*` | active 到 dormant/archive 的低增长年龄门槛及低频探测间隔 |
| `activity_windows.defaults` | 本地时间半开区间 `[start, end)`；允许跨午夜和重叠，重叠只算一个 active 状态 |
| `tier_intervals_minutes` | 各 tier 在 active/normal 时间的最大采集间隔；active 不得大于 normal |
| `hot_comments.routine_pages` | active routine 的热门总页数；默认 S/A/B/C = 3/2/1/1 |
| `hot_comments.checkpoint_pages` | 6/12/18/24 小时 checkpoint 与首次 active 采纳的热门总页数；默认 20/10/3/1，不能低于对应 routine 页数 |
| `hot_comments.max_pages_per_slice` | 一个编号热门 task 最多成功采集的页数；默认 10 |
| `hot_comments.max_slice_seconds` | 一个编号热门 task 的最长运行时间；默认 55 秒，必须小于 worker lease |

每个 tier 的播放增长、评论增长和可用 turnover 信号使用 OR 语义，并按 S、A、B 首个命中；未命中为 C。阈值必须从 S 到 B 单调不增，turnover ratio 必须在 `[0, 1]`。`tier_policy` 或 `tier_intervals_minutes` 中的未知 tier 键会直接拒绝，不能静默回退到默认采集频率。机器人、立场、协调和“带节奏”模型输出不是调度输入。

纯时间策略仍保留现有年龄/播放增长间隔，再与 tier ceiling 和下一 checkpoint 取最小值。生命周期要求明确低增长证据；缺失证据不会把旧视频归档，事件、人工 pin 或增长恢复会立即回到 active。

热门目标按两层组件保存。routine 的 `hot_core` 使用 3/2/1/1 页；checkpoint 和首次 active 采纳仍先采相同 core，再由 `hot_deep` 只补足到 20/10/3/1 页，所以 S 的 deep 是第 4-20 页、A 是第 3-10 页、B 是第 2-3 页、C 没有 deep。dormant routine 固定一页 core，archived 只保留指标探测。页数是请求目标而不是服务端事务快照；平台提前返回明确末尾或成功空页时允许少于目标结束。

`policy_version` 与 policy 内容一一对应。planner 首次看到该版本时会把标准化策略 JSON 写入 `collection_policy_versions` 并激活；同一版本再次启动时必须得到完全相同的内容。C4/C5 的热门与 latest 调度策略默认属于 `cohort-default-v2`。已有显式 v1 配置仍可解析，但不能在保持旧版本名时静默换成 v2 内容；修改 checkpoint、热门页数、切片限制、阈值、窗口、生命周期、间隔或 planner 周期时必须使用新的版本名。

启用 shadow 的最小配置：

```yaml
snapshot_cohorts:
  enabled: true
  policy_version: cohort-default-v2
  rollout_mode: shadow
  planning_seconds: 30
```

shadow 可在 Docker、Linux 原生和 Windows 原生服务中使用，三者连接同一个外部 PostgreSQL 时共享 scheduled-job lease 和 planner 状态。真正启用编号 scan 后，`scan_slice_key` 会跨 task 的 pending/running/backoff/succeeded/failed 全状态保持唯一；hot 从 `next_page_number` 继续，latest 从 `frontier_states.cursor`、持久 retry progress 和 frontier version 继续。PostgreSQL 用于单 BVID active latest 条件唯一索引、行锁和 CAS 竞争；SQLite 只支持单进程开发，不用于验证该并发模型。

C5 没有新增用户可调的 latest 页数。一个 BVID 在任意时刻最多拥有一个 active latest
scan；同一 scan 可以被多个 cohort component 以 `joined_active_task` 共享。只有
`baseline_head_sweep` 或 `incremental` 第一张成功头页的 `head_captured_at` 落在组件
`[scheduled_for, deadline)` 内才满足 current-head 合同。历史 `baseline_tail` 页面不能
完成该组件；deadline 到达仍无有效头页时，组件以
`baseline_tail_in_progress` 或 `current_head_not_captured` 结束为 partial。

C6 负责 full/segmented reconciliation 和 visibility watchlist；C7 才负责启用 live、
迁移旧调度 owner、拆分 page-level 短事务、续租长任务并执行容量/存储 gate。不要把 C5
底层 live 测试能力当成已获准的生产开关。

## Important Reply Watchlist

```yaml
watchlist:
  hot_max_position: 3
  reply_growth_min: 5
  like_growth_min: 20
  recent_first_seen_bonus: 2
  controversy_keywords: []
```

根评论满足以下任一证据信号时进入 `important_comment_watchlist` 并派生一页楼中楼任务：

- 热门排序位置不大于 `hot_max_position`。
- 相邻观测 reply 增量达到 `reply_growth_min`。
- 相邻观测 like 增量达到 `like_growth_min`。
- 文本包含显式配置的 `controversy_keywords`。

`recent_first_seen_bonus` 提高首次发现候选的 task priority。关键词会去空白、casefold 和去重；项目不提供主观默认词表。

## Analysis Lexicon

```yaml
analysis:
  stance_lexicon:
    version: 2026-07-v1
    support: ["赞同", "支持"]
    criticism: ["质疑", "反对"]
    neutral: ["求证", "观望"]
```

`event stance-evidence` 要求存在非空 `version`，分类只能是 `support`、`criticism`、`neutral`。同一规范化词不能跨分类重复。结果是词表命中证据，不是完整立场分类器；修改术语或含义时必须更新 version。

## Discovery Pools

```yaml
discovery:
  matrix_uids: []
  game_uid_pools:
    genshin_impact:
      game_id: genshin_impact
      official: true
      monitored: true
      uids: [401742377]
    wuthering_waves:
      game_id: wuthering_waves
      official: true
      monitored: true
      uids: [1955897084]
    honkai_star_rail:
      game_id: honkai_star_rail
      official: true
      monitored: true
      uids: [1340190821]
    zenless_zone_zero:
      game_id: zenless_zone_zero
      official: true
      monitored: true
      uids: [1636034895]
    honkai_impact_3rd:
      game_id: honkai_impact_3rd
      official: true
      monitored: true
      uids: [27534330]
    arknights_endfield:
      game_id: arknights_endfield
      official: true
      monitored: true
      uids: [1265652806]
    arknights:
      game_id: arknights
      official: true
      monitored: true
      uids: [161775300]
  event_uid_pools: {}
```

- `matrix_uids`：通用矩阵账号；来源默认为 `pool_id=matrix`、
  `game_id=null`、`official=false`、`monitored=true`。
- `game_uid_pools`：保留 `pool_type=game` 和 pool ID 的静态分组。省略元数据时，
  `game_id` 默认为 pool key，`official` 与 `monitored` 均默认为 `true`。
- `event_uid_pools`：保留 `pool_type=event` 和 pool ID 的静态分组；默认
  `game_id=null`、`official=false`、`monitored=true`。
- active 事件中的 UID target 会在 scheduler 运行时动态合并，不需要重复写入 YAML。

pool 值既可写成 `{uids: [...]}`，也可直接写列表或单个 UID。`official` 和
`monitored` 必须是真正的 YAML boolean；`"false"` 这类字符串会被拒绝。MID、
pool type、pool ID 和非空 game ID 不允许只含空白。

服务每轮按 MID 合并来源，但不会丢弃重复归属：一个 MID 同时位于 matrix、game、
event pool 时只生成一条 discovery task，完整、去重、稳定排序后的来源写入
`source_associations`。legacy `source_pool_type/source_pool_id` 只保留首个排序来源供
兼容诊断。active event UID target 使用独立 `pool_id=target:<target_id>`；仅
`extra.role=official` 会设置 `official=true`，`major_creator` 不会被等同为官方账号。

模板默认监测以下于 2026-07-13 经 B 站用户检索确认的官方主发布账号：

| Pool ID | 游戏 | B 站账号 | MID |
| --- | --- | --- | ---: |
| `genshin_impact` | 原神 | 原神 | `401742377` |
| `wuthering_waves` | 鸣潮 | 鸣潮 | `1955897084` |
| `honkai_star_rail` | 崩坏：星穹铁道 | 崩坏星穹铁道 | `1340190821` |
| `zenless_zone_zero` | 绝区零 | 绝区零 | `1636034895` |
| `honkai_impact_3rd` | 崩坏三 | 崩坏3第一偶像爱酱 | `27534330` |
| `arknights_endfield` | 终末地 | 明日方舟终末地 | `1265652806` |
| `arknights` | 明日方舟 | 明日方舟 | `161775300` |

默认池只收录每款游戏持续发布版本内容的核心账号，不自动加入动画项目、赛事、角色或同人运营账号。账号迁移或运营策略变化时，应重新检索并更新对应 pool；需要覆盖其他官方账号时，直接把 MID 追加到该 pool 的 `uids` 即可。

## Environment Variables

| 环境变量 | 覆盖目标 |
| --- | --- |
| `BOT_CONFIG` | 配置文件路径 |
| `BOT_DATABASE_URL` | `database.url` |
| `BOT_RAW_DIR` | `storage.raw_dir` |
| `BOT_MEDIA_DIR` | `storage.media_dir` |
| `BOT_RAW_STORAGE_BACKEND` | `storage.backend` |
| `BOT_MINIO_ENDPOINT` | `storage.minio.endpoint` |
| `BOT_MINIO_ACCESS_KEY` | `storage.minio.access_key` |
| `BOT_MINIO_SECRET_KEY` | `storage.minio.secret_key` |
| `BOT_MINIO_BUCKET` | `storage.minio.bucket` |
| `BOT_MINIO_PREFIX` | `storage.minio.prefix` |
| `BOT_MINIO_SECURE` | `storage.minio.secure` |
| `BOT_MINIO_CREATE_BUCKET` | `storage.minio.create_bucket` |
| `BOT_INSTANCE_ID` | `service.instance_id` |
| `BOT_SERVICE_ROLES` | `service.roles`，逗号分隔 |
| `BOT_SHUTDOWN_GRACE_SECONDS` | `service.shutdown_grace_seconds` |
| `BOT_ACCOUNT_ENABLED` | `accounts.enabled` |
| `BOT_ACCOUNT_ID` | `accounts.active_account_id` |
| `BOT_ACCOUNT_CREDENTIALS_PATH` | `accounts.credentials_path` |
| `BOT_ACCOUNT_KEY_PATH` | `accounts.key_path` |
| `BOT_ACCOUNT_REFRESH_SECONDS` | `accounts.refresh_check_seconds` |
| `BOT_ACCOUNT_AUTO_REFRESH` | `accounts.auto_refresh` |

布尔环境变量接受 `true/false`、`1/0`、`yes/no` 或 `on/off`，忽略大小写；其他值会报错。

`BOT_DATA_DIR` 和 `BOT_WORKER_REPLICAS` 由 Docker Compose 自己消费，不进入 Python 配置：前者控制宿主机 volume 根目录，后者控制 split Compose worker replicas。

`BOT_DATABASE_SCHEMA` 只用于 Alembic/legacy adoption 的隔离 schema 流程，普通应用 engine 不读取它，不应把它当作生产 runtime schema 配置。

## Post-Change Checks

修改配置后至少运行：

```bash
uv run python main.py service doctor
uv run python main.py login status
uv run python main.py service status --limit 20
```

修改 rate limit 规则时，数据库已有 bucket 不会自动改写。先停止所有连接该任务库的请求进程，再按 [OPERATIONS](OPERATIONS.md#16-scaling) 在事务中把受影响 `request_budget_states` 的 refill/burst 更新为新配置，最后统一重启全部实例。只重启进程仍会与旧数据库规则冲突。
