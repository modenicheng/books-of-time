# Books of Time TODO

本文件用于追踪项目执行进度。Roadmap 说明方向和阶段，TODO 记录可以实际推进的任务。

状态标记：

- `[x]` 已完成
- `[ ]` 未开始
- `[~]` 进行中或已有基础但未达完成标准

## P0: Project Foundation

- [x] 建立 `books_of_time` 标准包结构。
- [x] 使用 uv 管理依赖和锁文件。
- [x] 配置 Ruff lint/format。
- [x] 配置 pytest 和异步测试依赖。
- [x] 建立 Alembic 基础配置。
- [x] 建立 PostgreSQL async SQLAlchemy engine/session。
- [x] 建立 YAML 配置加载。
- [x] 建立 Rich logger。
- [x] 清理旧顶层 `utils` 和 `task_orchestrator` 兼容代码。

## P0: Request Layer And Raw Evidence

- [x] 建立 `FetchResult` 统一请求结果对象。
- [x] 建立 `RawHttpClient`。
- [x] 建立 token bucket 限流器。
- [x] 接入 bilibili-api-python 自定义 `BiliAPIClient` 后端。
- [x] 确保 bilibili-api-python 构造请求，我们的后端负责限流和发请求。
- [x] 建立 raw payload 文件归档，使用 `.json.zst`。
- [x] 建立 `raw_payloads` ORM 索引表。
- [x] 保存 raw payload hash、storage URI、status code、request type 和 parser version。
- [x] 建立 `raw_page_observations` 表。
- [ ] 为请求失败建立统一错误类型：timeout、403、429、captcha、5xx、parse_error。
- [ ] 建立 `request_backoff_states` 表。
- [ ] 将失败退避接入 worker 和 request layer。
- [ ] 增加 raw inspect CLI：`bot raw inspect <raw_payload_id>`。

## P0: Task Queue And Worker

- [x] 建立 `collection_tasks` ORM 表。
- [x] 支持任务 enqueue。
- [x] 支持 due task lease。
- [x] 支持 worker run-once。
- [x] 支持任务成功状态。
- [x] 支持失败后 retry/backoff 到 `not_before`。
- [ ] 增加 `worker loop`。
- [ ] 增加 `task list` CLI。
- [ ] 增加 `task retry-failed` CLI。
- [ ] 增加 running task lease 过期回收。
- [ ] 增加任务唯一性约束或幂等键，避免重复入队。
- [ ] 增加 collection run id 与 run 生命周期表。

## P0: Video Discovery And Snapshot

- [x] 建立阶梯式快照策略：前 30 分钟 1 分钟一次，30 分钟到 6 小时 5 分钟一次，之后动态退火。
- [x] 建立核心时段判断：10:00 到 22:00 详情轮询。
- [x] 建立 `KnownVideo` 基础表。
- [x] 建立 `DiscoveryScheduler.handle_discovered_videos()`。
- [x] 新发现视频自动触发 `fetch_video_stats` 任务。
- [x] 使用 bilibili-api-python `User.get_videos()` 构造 UID 投稿列表请求。
- [ ] 实现常驻 discovery loop，每分钟扫描配置的矩阵 UID。
- [ ] 支持事件级 UID 池和游戏级 UID 池。
- [ ] 增加 Redis Set 或数据库幂等机制记录已处理 BV。
- [ ] 对 `pubdate <= now - 2min` 的视频按策略处理，避免发现延迟造成黄金窗口缺口。
- [ ] 在 22:00 强制生成当日终结快照任务。

## P0: Video Metrics

- [x] 使用 bilibili-api-python `Video.get_info()` 构造视频信息请求。
- [x] 从 `data.stat` 解析播放、点赞、投币、收藏、转发、评论、弹幕。
- [x] 建立 `video_metric_snapshots` 宽表。
- [x] CLI 支持 `monitor-video BVxxxx`。
- [x] worker 可执行视频指标采集任务。
- [x] 真实 B站视频指标采集烟测通过。
- [ ] 保存视频标题、简介、tag、UP 主信息快照。
- [ ] 记录视频删除、不可见、权限异常状态。
- [ ] 增加 `bot video stats BVxxxx` 查询 CLI。
- [ ] 基于最近 1 小时播放增量计算动态下次快照时间。
- [ ] 将快照策略接入 scheduler，而不只是纯函数测试。

## P1: Hot Comments

- [x] 调研 bilibili-api-python 评论接口，确认热门评论和最新评论方法。
- [x] 建立 `comment_entities` ORM 表。
- [x] 建立 `comment_observations` ORM 表。
- [x] 建立热门评论 parser。
- [x] 建立评论 content hash，并保留公开用户字段用于核验。
- [x] 实现 `HotCommentCollector`。
- [x] 支持热门评论第一页采集。
- [ ] 支持按视频 tier 配置热门评论页数。
- [x] 写入 raw page observation。
- [x] 写入 comment entities。
- [x] 写入 comment observations。
- [x] CLI 支持 `bot video comments BVxxxx --mode hot`。
- [x] 测试同一 rpid 多次观测不会重复创建 entity。

## P1: Latest Comments Frontier

- [x] 建立 `frontier_states` 基础 ORM 表。
- [ ] 实现 latest comments parser。
- [ ] 实现 `LatestCommentCollector`。
- [ ] 第一次采集按 page limit 扫描 N 页。
- [ ] 第二次采集遇到旧 frontier 后停止。
- [ ] 未遇到旧 frontier 时标记 `last_scan_truncated=true`。
- [ ] 更新 `frontier_rpid`、`frontier_time`、`cursor`。
- [ ] CLI 支持 `bot collect-latest-comments BVxxxx`。
- [ ] 测试 frontier 正常到达和 truncated 两种情况。

## P1: Coverage And Data Quality

- [ ] 建立 `collection_runs` 表。
- [ ] 建立 `collection_coverage_stats` 表。
- [ ] 记录 hot pages requested/succeeded。
- [ ] 记录 latest pages requested/succeeded。
- [ ] 记录 latest frontier reached。
- [ ] 记录 reply roots requested/succeeded。
- [ ] 记录 request success rate。
- [ ] 记录 parse error count。
- [ ] CLI 支持 `bot coverage BVxxxx`。
- [ ] 所有 collector 在成功或失败后都写覆盖率摘要。

## P2: Comment State Events

- [ ] 建立 `comment_state_events` 表。
- [ ] 建立 `comment_visibility_events` 表。
- [ ] 实现 `FIRST_SEEN` 事件。
- [ ] 实现 like bucket 变化事件。
- [ ] 实现 reply count 变化事件。
- [ ] 实现 hot position 变化事件。
- [ ] 实现 content hash 变化事件。
- [ ] 实现 disappeared/reappeared/folded/unfolded 事件。
- [ ] 区分 `missing_after_seen`、`not_reached`、`unknown_due_to_fetch_error`。
- [ ] 确保无变化时不写 state event。

## P2: Important Replies

- [ ] 建立 `important_comment_watchlist` 表。
- [ ] 定义 root priority 计算：回复增长、点赞增长、热门位置、争议关键词、最近出现。
- [ ] 热门评论前排进入 watchlist。
- [ ] 回复数增长快的 root 进入 watchlist。
- [ ] 实现 `FetchCommentRepliesTask`。
- [ ] 实现 `ReplyCollector`。
- [ ] 楼中楼写入 comment entities 和 observations。
- [ ] Watchlist 支持 expires_at。

## P2: Event Archive

- [ ] 建立 `events` 表。
- [ ] 建立 `event_targets` 表：UID、关键词、种子 BV、游戏。
- [ ] 建立 `event_videos` 表。
- [ ] 建立 `event_keywords` 表。
- [ ] CLI 支持 `bot event create`。
- [ ] CLI 支持 `bot event add-target`。
- [ ] CLI 支持 `bot event list-videos`。
- [ ] Scheduler 可按事件目标池发现新视频。
- [ ] 事件级覆盖率汇总。
- [ ] 事件基础时间线导出。

## P3: Analysis

- [ ] 关键词趋势分析：按事件、视频、时间窗口聚合。
- [ ] 关键词共现分析。
- [ ] 热门评论 Top N 换血率。
- [ ] 支持/批评/中性词表的初版配置。
- [ ] 模板化评论候选检测：相似文本、短时间、跨视频。
- [ ] 重复评论 flag：same rpid duplicate display。
- [ ] 重复评论 flag：same user duplicate submission。
- [ ] 重复评论 flag：template-like comment。
- [ ] 传播节点初版评分：originator、amplifier、bridge、responder、official。
- [ ] 事件转折点检测：关键词突增、评论突增、热门换血、大 UP 介入。

## P4: Replay And Reports

- [ ] 视频指标时间线回放。
- [ ] 热门评论历史回放。
- [ ] 评论消失/重现时间线。
- [ ] 事件传播链回放。
- [ ] 报告生成器初版。
- [ ] 报告包含事件概述。
- [ ] 报告包含数据覆盖情况。
- [ ] 报告包含关键时间线。
- [ ] 报告包含核心视频节点。
- [ ] 报告包含热门评论变化。
- [ ] 报告包含关键词趋势。
- [ ] 报告包含模板化评论簇。
- [ ] 报告包含结论限制。
- [ ] 报告中的关键结论能追溯 raw evidence。

## P5: Operations And Scaling

- [ ] 支持 raw storage backend 抽象：filesystem / MinIO。
- [ ] 为 comment observations 设计月分区。
- [ ] 为大时间表建立 BRIN 索引。
- [ ] 增加数据库维护脚本。
- [ ] 增加 worker health check。
- [ ] 增加任务积压监控。
- [ ] 增加请求失败率监控。
- [ ] 评估 TimescaleDB 是否必要。
- [ ] 评估 ClickHouse 分析副本是否必要。
- [ ] 评估 OpenSearch / Meilisearch 全文检索是否必要。

## Near-term Sprint

建议下一轮优先做：

1. `raw_page_observations` + 热门评论第一页采集。
2. `comment_entities` + `comment_observations` 基础模型。
3. 最新评论 frontier 增量采集。
4. `collection_runs` + `collection_coverage_stats`。
5. `worker loop` 和 `task list` CLI。
