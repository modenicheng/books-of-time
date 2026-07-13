# Complete User Guide

本手册覆盖从空数据库到长期采集、事件分析和 raw 证据复核的完整主流程。所有命令都对应当前公开 CLI；详细参数见 [CLI_REFERENCE](CLI_REFERENCE.md)。

## 1. What The Service Does

Books of Time 记录 Bilibili 公公开视频和评论区在不同时间点的可见状态：

- 视频标题、作者、可用性和播放/点赞/投币/收藏/分享/评论/弹幕指标。
- 热门评论页面及其排序位置。
- 最新评论的首次全量 baseline 和后续 frontier 增量。
- 重点根评论的楼中楼回复。
- 评论图片引用、下载后的本地图片文件、hash 和评论关联。
- 成功返回的 raw 响应、解析页、结构化 observation、状态事件和每个 task 的 coverage；分类为 403/429/captcha/5xx 的失败响应当前会记录错误/退避，但 body 不保证归档。
- 事件级关键词、传播节点、转折信号、回放和 evidence-backed report。

系统不绕过平台风控，不建设代理池或 Cookie 池，不把普通用户的事件内信号解释为长期身份标签。

## 2. Runtime Architecture

```mermaid
flowchart LR
    O["Operator CLI"] --> DB[(PostgreSQL)]
    S["Scheduler role"] --> DB
    DB --> Q["Persistent collection tasks"]
    W["Worker role"] --> Q
    W --> H["Unified HTTP / rate limit / backoff"]
    H --> B["Public Bilibili APIs"]
    H --> R["Raw payload storage"]
    W --> N["Parsers and repositories"]
    N --> DB
    N --> M["Media source tasks"]
    M --> W
    W --> F["Local media filesystem"]
    DB --> A["Event analysis and reports"]
    R --> A
```

正式运行入口是 `service run`。scheduler 只负责持久化周期作业和派生任务，worker 领取 PostgreSQL lease 后请求平台并写入证据。CLI 是管理和诊断界面，不应依靠人工长期维持多个 loop。

## 3. Prerequisites

- Python 3.12 或更高版本。
- [uv](https://docs.astral.sh/uv/)。
- PostgreSQL，可位于本机、宿主机或局域网；连接 URL 必须使用 `postgresql+asyncpg://`。
- raw、media 和 accounts 目录的写权限。
- 可访问 Bilibili 公共 API 和图片域名的网络。

SQLite 只用于测试和单进程开发，不提供 PostgreSQL 的跨进程 task lease 和共享请求预算保证。

## 4. Install And Configure

Windows PowerShell：

```powershell
winget install --id=astral-sh.uv
Copy-Item config/config.yaml.example config/config.yaml
uv sync --group dev
```

Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cp config/config.yaml.example config/config.yaml
uv sync --group dev
```

至少修改 `config/config.yaml` 中的数据库 URL：

```yaml
database:
  url: postgresql+asyncpg://books_of_time_user:password@127.0.0.1:5432/books_of_time
```

完整配置、优先级和环境变量见 [CONFIGURATION](CONFIGURATION.md)。不要提交本地 `config/config.yaml`、数据库密码或账号文件。

## 5. Initialize The Database

新数据库：

```bash
uv run alembic upgrade head
uv run python main.py service doctor
```

也可用：

```bash
uv run python main.py init-db
```

旧版通过 SQLAlchemy `create_all` 创建、但没有 `alembic_version` 的开发库，先备份，再运行：

```bash
uv run python main.py init-db --adopt-legacy
uv run python main.py service doctor
```

`service doctor` 必须确认数据库、Alembic revision、raw backend 和 media 目录都可用。它不要求已有运行中的 worker；`service health` 才检查 heartbeat。

## 6. Optional QR Login

匿名采集可以正常运行。需要维护自己的单账号 Cookie 时：

```bash
uv run python main.py login qr
uv run python main.py login status
```

扫码成功后凭据写入 `data/accounts` 的加密快照。每次请求读取最新有效快照，scheduler 默认每 21600 秒检查刷新。服务没有有效 Cookie 时退回匿名，不停止采集生命周期。详见 [LOGIN](LOGIN.md)。

## 7. First Manual Collection

以下流程适合验证一个公开 BV。将 `<BVID>` 替换为真实 BV 号。

### 7.1 Enqueue Video Metrics

```bash
uv run python main.py monitor-video <BVID>
uv run python main.py worker run-once
uv run python main.py video stats <BVID>
```

`monitor-video` 只入队，不同步请求。worker 成功后会写入视频信息、指标、可用性、raw payload 和 coverage。只有该 BVID 已由 discovery 登记到 `known_videos` 时，才会按快照策略自动安排后续指标任务；手工 BVID 的长期刷新需纳入 discovery/event 或由外部 timer 再次入队。

### 7.2 Collect Hot Comments

```bash
uv run python main.py video comments <BVID> --mode hot --tier c --page-limit 1
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
```

热门评论可能派生重点楼中楼和图片下载任务，所以一次 `video comments` 后执行的任务数可能大于一。`tier` 选择请求预算档位；显式 `--page-limit` 覆盖该档位的 `hot_pages`。

### 7.3 Build Latest-Comment Baseline

```bash
uv run python main.py collect-latest-comments <BVID> --max-scan-seconds 55
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
uv run python main.py coverage <BVID>
```

首次 baseline 是两阶段过程：

1. tail scan 从第一页向旧评论扫描，可能跨多个 55 秒任务片段。
2. 到达尾部后状态为 `baseline_tail_complete`。
3. 下一轮从 head 回扫到 baseline 起点，捕获采集期间新增评论。
4. 只有状态为 `baseline_complete` 时才建立正式 frontier。

tail 完成后再入队一次：

```bash
uv run python main.py collect-latest-comments <BVID> --max-scan-seconds 55
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
uv run python main.py coverage <BVID>
```

后续同一命令执行增量扫描，看到旧 `frontier_rpid` 后停止。`partial`、`truncated` 或 `corrupted` 不能当作全量 baseline。

## 8. Run As A Long-Lived Service

本机开发运行：

```bash
uv run python main.py service run
```

Ctrl+C 或 SIGTERM 会触发协作式停止、释放 lease 并写入 stopped heartbeat。正式 Linux 和 Docker 部署见 [DEPLOYMENT](DEPLOYMENT.md)。

另开终端观察：

```bash
uv run python main.py service health
uv run python main.py service status --limit 20
uv run python main.py task list --status pending --limit 50
```

`health` 失败意味着运行依赖或 heartbeat 不满足健康契约；`status` 中出现 active alert 是运维状态，不会自动改写采集数据。

内置 scheduler 在北京时间 10:00（含）到 22:00（不含）执行 UID discovery，
并全天执行已知视频指标 sweep、每日附加日终快照、Cookie refresh 和告警评估。
常驻 worker 全天处理已入队的评论、回复、media 和重试任务。它当前不会自动周期
入队所有视频的 hot/latest comments；长期评论刷新应由 systemd timer、Windows
Task Scheduler 或现有编排器周期调用入队 CLI。详见
[COLLECTION](COLLECTION.md#13-current-automation-boundary) 和
[OPERATIONS](OPERATIONS.md#6-comment-collection-scheduling)。

## 9. Configure Discovery

自动发现调度：

```yaml
scheduler:
  discovery_scan_seconds: 60
  discovery_start_hour: 10
  discovery_stop_hour: 22
  discovery_timezone: Asia/Shanghai
  discovery_focus_times: ["11:00", "12:00", "13:00", "18:00", "19:00", "19:30", "20:00"]
```

起始时点包含、停止时点不包含。每个重点时点会在 T+0 和 T+30 秒各执行一次
高优先级 discovery，并写入 `focus_offset_seconds` 等审计字段；视频指标和已排队
评论任务不受此窗口限制。

静态 UID 池可放在 YAML：

```yaml
discovery:
  matrix_uids: [12345]
  game_uid_pools:
    example_game:
      uids: [23456, 34567]
  event_uid_pools:
    example_event:
      uids: [45678]
```

也可给 active 事件添加 UID target：

```bash
uv run python main.py event add-target <EVENT> uid 12345 --priority 100 --role official
```

正式 scheduler 在发现窗口内每分钟把 UID discovery 写成
`discover_user_videos` task。worker 保存投稿列表 raw 和 coverage，新发现视频进入
`known_videos` 并产生视频指标任务；事件 UID target 发现的视频会自动关联事件。

## 10. Create An Event

```bash
uv run python main.py event create example-event --name "示例事件" --game "示例游戏" --start-at 2026-07-13T00:00:00+08:00 --timezone Asia/Shanghai
uv run python main.py event add-target example-event seed_bvid <BVID> --priority 100
uv run python main.py event add-target example-event keyword "关键词" --priority 80
uv run python main.py event add-target example-event uid 12345 --priority 100 --role major_creator
uv run python main.py event list-targets example-event
uv run python main.py event list-videos example-event
```

事件 target 只是显式研究范围：

- `seed_bvid` 立即建立视频关联并入队视频指标。
- `uid` 参与 scheduler discovery；仅 active 且处于事件时间窗内时生效。
- `keyword` 同步成版本化 event keyword，供趋势和报告使用。
- `game` 保存事件分类目标，本身不自动搜索平台。

生命周期、停用和恢复操作见 [EVENTS](EVENTS.md)。

## 11. Generate Analysis And Reports

所有时间必须带 offset，窗口为 `[since, until)`：

```bash
uv run python main.py event coverage example-event --since 2026-07-13T00:00:00+08:00 --until 2026-07-14T00:00:00+08:00
uv run python main.py event export-timeline example-event --output data/reports/example-timeline.jsonl
uv run python main.py event keyword-trends example-event --since 2026-07-13T00:00:00+08:00 --until 2026-07-14T00:00:00+08:00 --output data/reports/example-keywords.jsonl
uv run python main.py event report example-event --since 2026-07-13T00:00:00+08:00 --until 2026-07-14T00:00:00+08:00 --output data/reports/example.md --json-output data/reports/example.json
```

报告包含事件概述、筛选条件、覆盖率、关键时间线、核心视频、热门评论变化、关键词趋势、模板候选、限制和 evidence index。模板相似、立场词表和传播角色都是证据候选，不是事实判定。全部分析入口和输出 schema 见 [ANALYSIS](ANALYSIS.md)。

## 12. Verify Evidence

从报告 JSON 的 `evidence_index` 选择 `raw_payload` ID：

```bash
uv run python main.py raw inspect <RAW_ID> --preview-bytes 1200
```

文本 raw 使用可安全输出的转义预览，图片 raw 使用十六进制预览。数据库中的 `payload_hash`、`storage_uri`、解析版本和响应状态可用于重新解析和人工核验。完整证据链见 [DATA_MODEL](DATA_MODEL.md)。

## 13. Routine Operator Checklist

每日或每次部署后：

```bash
uv run python main.py service doctor
uv run python main.py service health
uv run python main.py service status --limit 20
uv run python main.py task list --status failed --limit 100
uv run python main.py database maintain --output data/maintenance-plan.jsonl
```

先审查 maintenance dry-run，确认维护窗口后才使用 `--execute`。备份必须同时包含 PostgreSQL、raw、media 和 accounts；详见 [OPERATIONS](OPERATIONS.md)。

## 14. Next References

- 命令查找：[CLI_REFERENCE](CLI_REFERENCE.md)
- 配置解释：[CONFIGURATION](CONFIGURATION.md)
- 采集与 coverage：[COLLECTION](COLLECTION.md)
- 数据表与 hash：[DATA_MODEL](DATA_MODEL.md)
- 分析算法与限制：[ANALYSIS](ANALYSIS.md)
- 部署：[DEPLOYMENT](DEPLOYMENT.md)
- 故障排查：[TROUBLESHOOTING](TROUBLESHOOTING.md)
