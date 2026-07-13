# CLI Reference

当前 CLI 入口：

```bash
uv run python main.py [--config PATH] <COMMAND> ...
```

仓库没有安装 `bot` console script。退出码非 0 表示参数、配置、检查、迁移、维护或执行失败。

## Global Option

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--config PATH` | `BOT_CONFIG` 或 `config/config.yaml` | 必须放在子命令之前，例如 `main.py --config x.yaml service doctor` |

## Database Commands

### `init-db`

```bash
uv run python main.py init-db
uv run python main.py init-db --adopt-legacy
```

- 无参数时执行 Alembic `upgrade head`。
- `--adopt-legacy` 只接管没有 `alembic_version` 且 schema 只存在已知旧版漂移的开发库；未知差异会拒绝。
- 正式升级也可直接运行 `uv run alembic upgrade head`。

### `database maintain`

```bash
uv run python main.py database maintain [--execute] [--vacuum] [--months-ahead N] [--output FILE]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--execute` | false | 不传时只输出计划；传入后逐项执行 |
| `--vacuum` | false | 将 `ANALYZE` 改为 `VACUUM (ANALYZE)`，可能产生显著 I/O |
| `--months-ahead` | `3` | 未来分区月数，范围 0 到 24；当前 parent 未切换时分区项会 skipped |
| `--output` | 无 | 将 action 以 `database-maintenance-action-v1` JSONL 写入文件 |

计划覆盖时间表 ANALYZE、可选 VACUUM、BRIN summarize 和经过 catalog 验证的分区 DDL。任一执行失败后，后续 action 标为 skipped，命令非 0 退出。

JSONL 每行 schema 为 `database-maintenance-action-v1`，字段是 `kind`、`target`、`sql`、`status` 和 `reason`。

## Video Collection And Query

### `monitor-video`

```bash
uv run python main.py monitor-video <BVID> [--priority N]
```

入队 `fetch_video_stats`，默认 priority `100`。不在 CLI 进程中直接请求。任务写入视频信息、指标、可用性、raw 和 coverage；只有该 BVID 已在 `known_videos` 中时，collector 才会按内置策略安排下一次自动快照。

### `video comments`

```bash
uv run python main.py video comments <BVID> [--mode hot] [--priority N] [--tier s|a|b|c] [--page-limit N]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--mode` | `hot` | 当前唯一支持值 |
| `--priority` | `80` | task priority |
| `--tier` | `c` | 在未传 page limit 时读取 `request_budget.<tier>.hot_pages` |
| `--page-limit` | tier 配置 | 从第 1 页开始请求的页数，不是评论条数；应传正整数，collector 至少按 1 处理 |

热门评论可派生楼中楼和 media 下载任务。

### `collect-latest-comments`

```bash
uv run python main.py collect-latest-comments <BVID> [--priority N] [--max-scan-seconds S]
```

默认 priority `70`、时间片 `55` 秒。首次调用执行 baseline tail，tail 完成后需要下一轮 head sweep 才成为 `baseline_complete`；后续调用执行 frontier 增量。任务可能自动派生 follow-up。

### `video stats`

```bash
uv run python main.py video stats <BVID> [--limit N]
```

按时间倒序显示指标快照，默认 20，CLI 将 limit 限制为 1 到 200。

### `coverage`

```bash
uv run python main.py coverage <BVID> [--limit N]
```

显示视频目标的最近采集 coverage，包括 task kind、状态、reason、页面、items、frontier、truncated 和 corrupted。默认 20；当前 CLI 不做范围截断，参数会直接传给 repository，应使用合理的正整数。

## Video Analysis And Replay

所有时间必须带 offset，窗口为 `[since, until)`。

### `video hot-turnover`

```bash
uv run python main.py video hot-turnover <BVID> --since TIME --until TIME [--top-n N] --output FILE
```

比较相邻热门评论第 1 页，输出 `hot-comment-turnover-v1` JSONL。`top-n` 默认 20，范围 1 到 20；少于两个快照时输出空文件。

### `video replay-metrics`

```bash
uv run python main.py video replay-metrics <BVID> --since TIME --until TIME [--max-points N] --output FILE
```

输出 `video-metric-replay-v1` JSONL，默认最多 100000 点。超过上限时要求缩小窗口，不静默截断。

### `video replay-hot-comments`

```bash
uv run python main.py video replay-hot-comments <BVID> --since TIME --until TIME [--top-n N] [--max-snapshots N] --output FILE
```

输出 `hot-comment-replay-v1` JSONL。默认 `top-n=20`、`max-snapshots=10000`。

### `video replay-visibility`

```bash
uv run python main.py video replay-visibility <BVID> --since TIME --until TIME [--max-events N] --output FILE
```

输出 `comment-visibility-replay-v1` JSONL，包括 folded、unfolded、disappeared、reappeared 证据。默认最多 100000 条。

## Raw Evidence

### `raw inspect`

```bash
uv run python main.py raw inspect <RAW_PAYLOAD_ID> [--preview-bytes N]
```

显示 request type、时间、状态码、storage URI、压缩/未压缩尺寸、SHA-256 和 parser version。预览默认 1200 bytes，并限制在 0 到 10000；JSON 文本使用 ASCII-safe 转义，media 图片使用 hex。

不存在的 ID 会输出 `Raw payload not found` 并正常返回，不会伪造空内容。该命令读取和解压 payload，但不会自动重新计算 SHA-256 与数据库值比较。

### `raw migrate-minio`

```bash
uv run python main.py raw migrate-minio [--execute] [--limit N] [--after-id ID]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--execute` | false | 默认只列出 `file://` 候选 |
| `--limit` | `100` | 本批数量，范围 1 到 10000 |
| `--after-id` | `0` | 只选择更大的 raw ID，必须非负 |

执行时逐条验证本地源 hash/尺寸，上传后下载并再次验证，最后才更新数据库 URI。单条失败不阻止本批后续行，但最终命令非 0 退出；不会删除本地源文件。

## Worker And Task Queue

### `worker run-once`

```bash
uv run python main.py worker run-once
```

恢复过期 lease，按 priority/not-before 领取至多一个 task。没有可执行 task 时正常返回。collector 失败会写 task/coverage/retry 状态，但当前命令本身也正常返回；必须用 `task list` / `coverage` 判断所领取 task 的结果。

### `worker loop`

```bash
uv run python main.py worker loop [--idle-sleep-seconds S] [--max-iterations N] [--stop-when-idle]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--idle-sleep-seconds` | `5` | 无 task 时睡眠秒数 |
| `--max-iterations` | 无 | loop 次数上限；一次空轮询也计数 |
| `--stop-when-idle` | false | 第一次无可执行 task 时退出，适合 smoke |

长期运行使用 `service run`，不要把诊断 worker loop 当作正式部署。单个 task 失败不会让 loop 崩溃，失败已经持久化并按重试策略处理。

### `task list`

```bash
uv run python main.py task list [--status pending|running|succeeded|failed|backoff] [--limit N]
```

默认显示 20 条，可按状态筛选。结果按 priority 降序，再按创建时间和 ID 升序；不是单纯“最近 20 条”。输出 task ID、kind、target、priority、重试数、not-before 和 lease。limit 会截断到 1–200；`backoff` 是保留状态，当前失败重试通常表现为 `pending + future not_before`。

### `task retry-failed`

```bash
uv run python main.py task retry-failed [--target-id ID] [--kind KIND] [--limit N]
```

将 failed task 重新置为可执行状态。`kind` 可选：

```text
fetch_video_info
fetch_video_stats
fetch_hot_comments
fetch_latest_comments
fetch_comment_replies
fetch_media_asset
discover_user_videos
analyze_similar_media
```

默认最多 100，CLI 限制为 1 到 500。`fetch_video_info` 是保留 enum，当前 worker 没有对应 collector，视频信息实际随 `fetch_video_stats` 写入；不要重试这类 task。先确认其他失败原因已解除；重试不会删除旧 coverage 或 raw 证据。

## Discovery

### `discovery loop`

```bash
uv run python main.py discovery loop [--interval-seconds S] [--max-iterations N] [--stop-when-idle]
```

从 YAML 静态 UID pools 直接执行诊断扫描；默认周期来自 `scheduler.discovery_scan_seconds`。`--stop-when-idle` 在本轮没有新视频时停止。

这是兼容诊断入口：请求经过统一 HTTP/限流，但该直接 loop 不写 discovery raw/coverage，也不合并数据库中的 event UID target。正式证据路径是 `service run` 的 scheduler 产生 `discover_user_videos` task，再由 worker 执行。

### `discover-user`

```bash
uv run python main.py discover-user <MID> [--page N]
```

直接扫描指定用户页并登记新视频，默认第 1 页。它同样是诊断入口，不保存 user-list raw/coverage；正式采集使用事件 UID target 或配置 pool 加 `service run`。

## Service

### `service run`

```bash
uv run python main.py service run [--max-worker-iterations N]
```

启动配置中的 roles，先运行 doctor，再 bootstrap scheduled jobs，注册 heartbeat 并处理停止信号。`--max-worker-iterations` 仅用于测试/有限 smoke；正式运行不传。

### `service doctor`

```bash
uv run python main.py service doctor
```

检查数据库及 service schema、Alembic revision、实际 raw backend 和 media 目录写权限。不检查 heartbeat。

### `service health`

```bash
uv run python main.py service health
```

执行 doctor，并要求存在新鲜 running service heartbeat 和 worker-role heartbeat。没有运行服务时返回非 0 是预期行为。

### `service status`

```bash
uv run python main.py service status [--limit N]
```

默认显示 20 个实例/告警，限制为 1 到 200。输出队列计数、最老 pending、active backoff、请求失败窗口、服务实例和 active operational alerts。status 只读，不因告警自动失败。

## Login

### `login qr`

```bash
uv run python main.py login qr [--account ID] [--timeout-seconds S]
```

默认 account `default`、超时 180 秒。显示二维码，扫码确认后保存加密快照；不打印 Cookie 或 refresh token。

### `login status`

```bash
uv run python main.py login status [--account ID]
```

没有快照时显示 anonymous；已有快照时显示 `unknown`、`valid`、`invalid` 或 `superseded` health 及非敏感元数据。

### `login logout`

```bash
uv run python main.py login logout [--account ID]
```

删除指定账号的全部本地快照；服务后续请求回退匿名。

## Event Lifecycle

事件可用数字 ID 或 slug 引用。slug 只允许小写字母、数字和单连字符，创建后稳定不变。

### `event create`

```bash
uv run python main.py event create <SLUG> --name NAME [--game GAME] [--description TEXT] [--status planned|active|closed|archived] [--start-at TIME] [--end-at TIME] [--timezone ZONE]
```

默认 `status=active`、`timezone=Asia/Shanghai`。时间必须带 offset，end 不能早于 start。

### `event update`

```bash
uv run python main.py event update <EVENT> [--name NAME] [--game GAME|--clear-game] [--description TEXT|--clear-description] [--status STATUS] [--start-at TIME|--clear-start-at] [--end-at TIME|--clear-end-at] [--timezone ZONE]
```

至少提供一个变更；互斥 clear 参数用于显式清空可选字段。slug 不可修改。

### `event list`

```bash
uv run python main.py event list [--limit N]
```

默认 100，repository 接受 1 到 1000。

### `event add-target`

```bash
uv run python main.py event add-target <EVENT> <uid|keyword|seed_bvid|game> <VALUE> [--priority N] [--role official|major_creator]
```

- UID 必须为正整数。
- seed BVID 必须是规范 BV 格式。
- `--role` 只允许 UID target。
- seed BVID 会建立 active 视频关联并在首次创建时入队指标。
- keyword 会同步 event keyword version 1。

### `event list-targets`

```bash
uv run python main.py event list-targets <EVENT> [--type TYPE] [--all] [--limit N]
```

默认只显示 active target；`--all` 包含 inactive。默认 1000，repository 要求 1–1000。

### `event set-target-status`

```bash
uv run python main.py event set-target-status <EVENT> <TARGET_ID> <active|inactive>
```

停用 keyword 会同步停用 event keyword；停用 seed BVID 会停用由该 target 建立的视频关联；历史证据不删除。

### `event list-videos`

```bash
uv run python main.py event list-videos <EVENT> [--limit N] [--all]
```

默认只显示 active 关联；`--all` 包含 inactive。默认 1000，repository 要求 1–1000。

### `event set-video-status`

```bash
uv run python main.py event set-video-status <EVENT> <BVID> <active|inactive>
```

只改变分析范围，不删除采集 task、observation、media 或 raw。

## Event Coverage And Timeline

### `event coverage`

```bash
uv run python main.py event coverage <EVENT> [--since TIME --until TIME]
```

时间参数必须成对出现。有窗口时只汇总 `finished_at` 位于 `[since, until)` 的 active event-video coverage；无窗口时汇总全部历史。

### `event export-timeline`

```bash
uv run python main.py event export-timeline <EVENT> --output FILE
```

导出全部关联视频（包括 inactive 历史关联）的 `event-timeline-v1` JSONL，包括关联、指标、评论状态事件和可见性事件。当前命令没有时间过滤或记录上限参数；报告内部会按报告窗口生成有界 timeline。

## Event Analysis

### `event keyword-trends`

```bash
uv run python main.py event keyword-trends <EVENT> --since TIME --until TIME [--bucket-minutes N] [--bvid BVID] --output FILE
```

默认 60 分钟桶，范围 1 到 1440 分钟，最多 10000 个桶。输出每个 active keyword 的 `keyword-trend-v1` 完整时间序列，包含零值桶。

### `event keyword-cooccurrence`

```bash
uv run python main.py event keyword-cooccurrence <EVENT> --since TIME --until TIME [--bvid BVID] --output FILE
```

输出同一 comment observation 同时命中两个 active keyword 的 `keyword-cooccurrence-v1` 边；少于两个关键词或无命中时为空。

### `event stance-evidence`

```bash
uv run python main.py event stance-evidence <EVENT> --since TIME --until TIME [--bvid BVID] --output FILE
```

使用 `analysis.stance_lexicon` 输出 support/criticism/neutral 三条 `stance-evidence-v1` 汇总。是词表命中证据，不是用户立场分类。

### `event template-candidates`

```bash
uv run python main.py event template-candidates <EVENT> --since TIME --until TIME [--window-minutes N] [--min-similarity F] [--min-text-chars N] [--max-comments N] [--max-comparisons N] --output FILE
```

默认 60 分钟、相似度 0.85、最短 8 字符、最多 5000 comments/100000 comparisons。只比较跨视频文本，输出 `template-candidate-v1`；候选不证明协同行为。使用 `--bvid` 会把范围缩到单视频，因此当前跨视频算法不会产生 pair。

### `event refresh-comment-flags`

```bash
uv run python main.py event refresh-comment-flags <EVENT> --since TIME --until TIME [--template-window-minutes N] [--template-min-similarity F] [--template-min-text-chars N] [--max-comments N] [--max-comparisons N] --output FILE
```

重算重复/模板候选并 upsert 稳定 `comment_analysis_flags`；这是会修改数据库的分析命令。输出单个 `comment-flag-refresh-v1` JSON。重复运行同一算法版本不会复制同一 stable key。

### `event propagation-nodes`

```bash
uv run python main.py event propagation-nodes <EVENT> --since TIME --until TIME [--max-comments N] --output FILE
```

默认最多 50000 comments。输出 `propagation-node-score-v1`，角色分数包括 originator、amplifier、bridge、responder、official；结果仅是事件窗口候选分数。

### `event turning-points`

```bash
uv run python main.py event turning-points <EVENT> --since TIME --until TIME [--bucket-minutes N] [--spike-multiplier F] [--min-count N] [--turnover-threshold F] [--top-n N] [--max-records N] --output FILE
```

默认 60 分钟桶、3 倍 spike、最少 5 条、热门换血阈值 0.5、Top 20、最多 200000 records。输出 `turning-point-signal-v1`，可能包含 comment spike、keyword spike、hot turnover、major creator involvement；信号不是因果结论。

### `event replay-propagation`

```bash
uv run python main.py event replay-propagation <EVENT> --since TIME --until TIME [--max-records N] --output FILE
```

默认最多 100000 records。输出 `event-propagation-replay-v1` 的有向证据边，只包含可证明的 event-video 关联、楼中楼 reply 和跨视频 template flag，不补造完整因果图。

### `event report`

```bash
uv run python main.py event report <EVENT> --since TIME --until TIME [ANALYSIS OPTIONS] [--bvid BVID] [--keyword TEXT] --output FILE [--json-output FILE]
```

分析默认值：

| 参数 | 默认值 |
| --- | --- |
| `--bucket-minutes` | `60` |
| `--spike-multiplier` | `3.0` |
| `--spike-min-count` | `5` |
| `--turnover-threshold` | `0.5` |
| `--top-n` | `20` |
| `--template-window-minutes` | `60` |
| `--template-min-similarity` | `0.85` |
| `--template-min-text-chars` | `8` |
| `--max-videos` | `100` |
| `--max-records` | `5000` |

`--output` 写 Markdown，`--json-output` 可选写 `event-report-v1`。`--bvid` 和 `--keyword` 必须属于事件当前 active 范围，并下推到 coverage、timeline 和分析查询。报告包含 evidence index 和限制说明。

详细解释及输出 schema 见 [ANALYSIS](ANALYSIS.md)。
