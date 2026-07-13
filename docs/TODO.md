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
- [x] 为请求失败建立统一错误类型：timeout、403、429、captcha、5xx、parse_error。
- [x] 建立 `request_backoff_states` 表。
- [x] 将失败退避接入 worker 和 request layer。
- [x] 增加 raw inspect CLI：`bot raw inspect <raw_payload_id>`。

## P0: Task Queue And Worker

- [x] 建立 `collection_tasks` ORM 表。
- [x] 支持任务 enqueue。
- [x] 支持 due task lease。
- [x] 支持 worker run-once。
- [x] 支持任务成功状态。
- [x] 支持失败后 retry/backoff 到 `not_before`。
- [x] 增加 `worker loop`。
- [x] 增加 `task list` CLI。
- [x] 增加 `task retry-failed` CLI。
- [x] 增加 running task lease 过期回收。
- [x] 增加任务唯一性约束或幂等键，避免重复入队。
- [x] 增加 collection run id 与 run 生命周期表。

## P0: Long-running Service Foundation

- [x] 建立 `books_of_time/service/`，由 `ServiceHost` 统一管理运行时资源和协作循环。
- [x] 建立 `service_instances` 表，记录实例身份、角色、状态、心跳和停止原因。
- [x] 实现服务启动检查：数据库连接、Alembic schema revision、核心表和 raw/media 目录可写。
- [x] 实现 `bot service run`，作为 Docker 和 Linux 原生部署的正式入口。
- [x] 实现 `SIGINT` / `SIGTERM` 优雅停止，停止领取新任务并为活动任务保留宽限期。
- [x] 实现 `bot service health`，供 Docker `HEALTHCHECK` 和运维探针调用。
- [x] 实现 `bot service status`，展示实例心跳、队列积压、最老待处理任务和请求退避。
- [x] 实现 `bot service doctor`，只执行部署前检查而不启动循环。
- [x] 建立 `scheduled_jobs` 表及持久化 job lease、失败重试和重启补跑。
- [x] 将 UID 发现改为 `DISCOVER_USER_VIDEOS` 任务，统一经过 worker、限流、退避、raw archive 和 coverage。
- [x] 将视频快照 sweep 从采集结果回调补全为独立持久化调度作业。
- [x] 将每日终结快照改为独立持久化调度作业，不依赖 UID discovery 是否执行。
- [x] 增加 YAML 的 `service` 配置和 `BOT_*` 部署环境变量覆盖。
- [x] 初始 worker concurrency 固定为 1，跨进程全局限流完成前不启动多个 HTTP worker。
- [x] 提交可复现的 Alembic revision，并停止忽略 `alembic/versions/*.py`。
- [x] `init-db` 使用 Alembic 创建带 revision 的新库，并提供严格差异白名单的 `--adopt-legacy` 旧库接管。
- [x] 增加只运行 Books of Time 的 Dockerfile 和 Compose 示例，不捆绑 PostgreSQL。
- [x] Docker 支持连接宿主机或局域网已有 PostgreSQL，并挂载本地 raw/media 持久目录。
- [x] 增加 Linux systemd unit 和部署说明，连接已有 PostgreSQL。
- [x] 保留 Windows 下 `uv run python main.py service run` 开发入口，并通过新进程组 `CTRL_BREAK_EVENT` 烟测确认协作式停止和 `stopped` 状态落库。
- [x] 服务运行、重启恢复、健康检查和外部 PostgreSQL 连接具备自动化验收或 smoke test：已覆盖 SQLite/Windows 控制事件、PostgreSQL 隔离 schema service run、调度租约恢复、health、Compose config、migration cycle、Docker daemon build，以及容器连接宿主机 PostgreSQL 的 `service doctor`。

## P0: Video Discovery And Snapshot

- [x] 建立阶梯式快照策略：前 30 分钟 1 分钟一次，30 分钟到 6 小时 5 分钟一次，之后动态退火。
- [x] 将 10:00（含）到 22:00（不含）的北京时间窗口只用于自动新视频发现，不限制视频指标和已入队采集任务。
- [x] 重点覆盖 11:00、12:00、13:00、18:00、19:00、19:30、20:00，每个时点执行 T+0 与 T+30 秒两次幂等检查，并记录模式、重点时间、偏移和原始计划槽。
- [x] 视频指标阶梯快照全天运行；22:00 日终快照只作为额外幂等检查点，不结束常规 sweep。
- [x] 评论、回复、media、重试等已入队任务全天可由 worker 领取，不引入 discovery 时间窗耦合。
- [x] 建立 `KnownVideo` 基础表。
- [x] 建立 `DiscoveryScheduler.handle_discovered_videos()`。
- [x] 新发现视频自动触发 `fetch_video_stats` 任务。
- [x] 使用 bilibili-api-python `User.get_videos()` 构造 UID 投稿列表请求。
- [x] 实现常驻 discovery loop，每分钟扫描配置的矩阵 UID。
- [x] 支持事件级 UID 池和游戏级 UID 池。
- [x] 增加 Redis Set 或数据库幂等机制记录已处理 BV。
- [x] 对 `pubdate <= now - 2min` 的视频按策略处理，避免发现延迟造成黄金窗口缺口。
- [x] 在 22:00 强制生成当日终结快照任务。

## P0: Video Metrics

- [x] 使用 bilibili-api-python `Video.get_info()` 构造视频信息请求。
- [x] 从 `data.stat` 解析播放、点赞、投币、收藏、转发、评论、弹幕。
- [x] 建立 `video_metric_snapshots` 宽表。
- [x] CLI 支持 `monitor-video BVxxxx`。
- [x] worker 可执行视频指标采集任务。
- [x] 真实 B站视频指标采集烟测通过。
- [x] 保存视频标题、简介、tag、UP 主信息快照。
- [x] 记录视频删除、不可见、权限异常状态。
- [x] 增加 `bot video stats BVxxxx` 查询 CLI。
- [x] 基于最近 1 小时播放增量计算动态下次快照时间。
- [x] 将快照策略接入 scheduler，而不只是纯函数测试。

## P0: Media Assets

- [x] 建立 `media` 子系统目录：downloader、hasher、storage、normalizer、similarity。
- [x] 建立 `media_sources` ORM 表，记录评论中看到的图片 URL 引用。
- [x] 建立 `media_assets` ORM 表，使用 `blob_sha256` 做完全一致去重。
- [x] 建立 `comment_observation_media` ORM 表，支持单评论多图和同图多评论。
- [x] 评论 parser 提取图片引用到 `ParsedComment.media`。
- [x] 评论写入阶段登记 media source 和 observation-media 关系。
- [x] 为 pending media source 生成 `FETCH_MEDIA_ASSET` 任务。
- [x] 新增 `bilibili:media_image` 请求类型和限流配置。
- [x] 实现本地文件系统 media storage：`data/media/sha256/ab/cd/<hash>.<ext>`。
- [x] 实现 media 图片下载 worker，走统一 http 请求层和限流。
- [x] 下载后计算 `blob_sha256` 并复用已有 `media_asset`。
- [x] 图片保存到本地文件系统，不引入外部 S3/OSS。
- [x] 记录 MIME、文件扩展名、width、height、size_bytes。
- [x] 计算 `pixel_sha256`，作为像素完全一致候选依据。
- [x] 预留并写入 `phash` / `dhash` / `ahash` 字段。
- [x] 回填 `media_sources.media_asset_id` 和 `comment_observation_media.media_asset_id`。
- [x] 建立 `media_similarity_edges` ORM 表。
- [x] 建立 `media_clusters` 和 `media_cluster_members` ORM 表。
- [x] 实现离线相似图片分析任务，不阻塞采集链路。
- [x] 图片参与评论状态指纹：`media_ordered_hash` / `media_set_hash`。
- [x] 评论状态事件支持 `MEDIA_CHANGED` / `MEDIA_ADDED` / `MEDIA_REMOVED` / `MEDIA_ORDER_CHANGED`。

## P1: Hot Comments

- [x] 调研 bilibili-api-python 评论接口，确认热门评论和最新评论方法。
- [x] 建立 `comment_entities` ORM 表。
- [x] 建立 `comment_observations` ORM 表。
- [x] 建立热门评论 parser。
- [x] 建立评论 content hash，并保留公开用户字段用于核验。
- [x] 实现 `HotCommentCollector`。
- [x] 支持热门评论第一页采集。
- [x] 支持按视频 tier 配置热门评论页数。
- [x] 写入 raw page observation。
- [x] 写入 comment entities。
- [x] 写入 comment observations。
- [x] CLI 支持 `bot video comments BVxxxx --mode hot`。
- [x] 测试同一 rpid 多次观测不会重复创建 entity。

## P1: Latest Comments Frontier

- [x] 建立 `frontier_states` 基础 ORM 表。
- [x] 实现 latest comments parser。
- [x] 实现 `LatestCommentCollector`。
- [x] 第一次采集按 cursor baseline tail scan，并支持 55 秒暂停恢复。
- [x] baseline tail 完成后执行 head sweep，并在完成后设置官方 frontier。
- [x] 增量采集遇到旧 frontier 后停止。
- [x] 未遇到旧 frontier 到达服务端末尾时标记 `frontier_missing`。
- [x] 更新 `frontier_rpid`、`frontier_time`、`cursor`。
- [x] 实现 page-level retry/backoff。
- [x] 实现 paused/corrupted 状态落库。
- [x] CLI 支持 `bot collect-latest-comments BVxxxx`。
- [x] 测试 frontier 正常到达、暂停恢复、frontier_missing 和 corrupted 情况。

## P1: Coverage And Data Quality

- [x] 建立 `collection_runs` 表。
- [x] 建立 `collection_coverage_stats` 表。
- [x] 记录 hot pages requested/succeeded。
- [x] 记录 latest pages requested/succeeded。
- [x] 记录 latest frontier reached。
- [x] 记录 reply roots requested/succeeded。
- [x] 记录 request success rate。
- [x] 记录 parse error count。
- [x] CLI 支持 `bot coverage BVxxxx`。
- [x] 所有 collector 在成功或失败后都写覆盖率摘要。

说明：Phase 1C 以 requested/succeeded/error 计数保存请求成功情况，查询层可由此计算 success rate。

## P1: Account And Cookie Management

- [x] 建立独立 `accounts` 子系统，不依赖 PostgreSQL 或有效 Cookie 才能初始化采集服务。
- [x] 使用本地加密凭据文件保存 Cookie 快照；密钥和密文均限制为当前系统用户可读，禁止日志输出秘密字段。
- [x] 当前只启用单个 `default` 账号，同时在存储格式和 provider 接口保留 `account_id` 扩展点。
- [x] 明确上述 `account_id` 是避免未来重写持久化格式的兼容边界，不实现账号池、并发多账号调度或风控规避。
- [x] 实现独立二维码登录 CLI：`bot login qr`，登录成功后原子切换到新 Cookie 快照。
- [x] 实现 `bot login status` 和 `bot login logout`，输出不得包含 Cookie、refresh token 或 CSRF 值。
- [x] 统一 HTTP 层在每次请求前读取当前有效 Cookie，自动热加载其他进程写入的最新快照。
- [x] 托管 Cookie 覆盖 bilibili-api-python 传入的空值或旧值；登录和刷新握手可显式禁用自动注入。
- [x] 实现服务内定时 Cookie 有效性/刷新检查，刷新成功后保存新版本并自动轮换。
- [x] Cookie 缺失或确认失效时自动退回匿名请求，不阻止 service、worker 或 scheduler 启动。
- [x] 增加 Linux、Docker、Windows 共用的配置、权限说明和 `docs/LOGIN.md` 使用文档。
- [x] 覆盖加密存储、原子更新、热加载、请求注入、匿名降级、QR 登录和自动刷新测试。

## P1: Collection-First Snapshot Cohorts

设计基线：`docs/superpowers/specs/2026-07-13-collection-snapshot-cohorts-design.md`。本主线优先保证不可逆采集完整、可恢复、可审计；机器人识别、玩家聚类、神经网络和 LLM/Agent 分析不进入当前采集实现。

- [x] **C1 Evidence Foundations**：补齐评论平台时间与稳定公开作者字段、视频多来源/游戏归属、无响应 HTTP attempt 证据及可回溯迁移。
- [ ] **C2 Cohort State And Policy**：建立 policy version、video collection state、snapshot cohort/component、schedule gap 模型和纯函数时间/评级/生命周期策略。
- [ ] **C3 Persistent Planner And Shadow Mode**：增加 30 秒持久化 cohort planner、幂等组件任务、checkpoint 恢复和 shadow planning，不与旧 sweep 重复调度。
- [ ] **C4 Hot Core And Deep Scans**：实现 S/A/B/C 常规多页热门采集、checkpoint 20/10/3/1 页目标、55 秒编号切片和 all-status slice 幂等键。
- [ ] **C5 Latest Scan Runs And Automatic Baseline**：建立 scan run、CAS frontier、多锚点 baseline tail -> linked head sweep、增量 continuation 和单 BVID 活跃扫描约束。
- [ ] **C6 Visibility And Reconciliation**：建立 visibility watchlist/check、10/30 分钟高优先级复核、两次独立证据确认删除及 100 页内全量/超限分段 reconciliation。
- [ ] **C7 Capacity, Fairness And Storage Gates**：实现 15 分钟容量预测、游戏间 deficit round-robin、显式 miss/gap、raw storage 请求前熔断和新旧调度所有权迁移。
- [ ] **C8 Activity Window Adaptation**：按游戏与工作日/周末学习 28 天 30 分钟桶窗口，使用曝光归一化、中位数、边界限制、版本化自动激活和回滚。
- [ ] **C9 Integrity And Live Acceptance**：实现 raw/media/reference 完整性审计、20 个 S 过载模拟、shadow -> 单游戏 2 小时 -> 全游戏 24 小时验收及完整运维文档。

每阶段必须独立满足：测试先行、Alembic upgrade/downgrade、Ruff、相关 PostgreSQL 集成测试、覆盖/失败语义可查询，并使用单独 Conventional Commit。C1-C9 全部完成前，本 P1 主线不标记完成。

## P2: Comment State Events

- [x] 建立 `comment_state_events` 表。
- [x] 建立 `comment_visibility_events` 表。
- [x] 实现 `FIRST_SEEN` 事件。
- [x] 实现 like bucket 变化事件。
- [x] 实现 reply count 变化事件。
- [x] 实现 hot position 变化事件。
- [x] 实现 content hash 变化事件。
- [x] 实现 disappeared/reappeared/folded/unfolded 事件；folded 仅依据评论级平台字段 `folder.is_folded`，页面级 folder 只作为覆盖证据，不推断单条评论状态。
- [x] 区分 `missing_after_seen`、`not_reached`、`unknown_due_to_fetch_error`：只有完整到达服务端尾部仍缺少旧 frontier 才写消失事件；未到达和请求失败仅写入 coverage 的 `frontier_outcome`，不伪造可见性变化。
- [x] 确保无变化时不写 state event。

## P2: Important Replies

- [x] 建立 `important_comment_watchlist` 表。
- [x] 定义 root priority 综合计算：回复增长、点赞增长、热门位置、可配置争议关键词、首次观测加分。
- [x] 热门评论前排进入 watchlist。
- [x] 回复数增长快的 root 进入 watchlist。
- [x] 实现 `FetchCommentRepliesTask`。
- [x] 实现 `ReplyCollector`。
- [x] 楼中楼写入 comment entities 和 observations。
- [x] Watchlist 支持 expires_at。

## P2: Event Archive

- [x] 建立 `events` 表。
- [x] 建立 `event_targets` 表：UID、关键词、种子 BV、游戏。
- [x] 建立 `event_videos` 表。
- [x] 建立 `event_keywords` 表。
- [x] CLI 支持 `bot event create`。
- [x] CLI 支持 `bot event add-target`。
- [x] CLI 支持 `bot event list-videos`。
- [x] Scheduler 可按事件目标池发现新视频，并按 UID 合并请求、自动写入事件视频关联。
- [x] 事件级覆盖率汇总：视频覆盖比、页面成功率、raw 数量和错误/截断/损坏计数。
- [x] 事件基础时间线 JSONL 导出：关联、指标、评论状态/可见性事件和证据引用。

## P3: Analysis

- [x] 关键词趋势分析：按事件/视频、UTC 时间桶聚合去重评论数与观测命中数，并导出 JSONL。
- [x] 关键词共现分析：按事件/视频和时间窗口统计关键词对的去重评论数与观测命中数，并导出 JSONL。
- [x] 热门评论 Top N 换血率：按成功热门第一页快照比较进入、退出、保留评论和替换率。
- [x] 支持/批评/中性词表的初版配置：版本化词表、跨类别歧义校验和可解释 JSONL 命中统计，不对评论或用户强制贴标签。
- [x] 模板化评论候选检测：基于首见评论的相似文本、短时间、跨视频证据对，保留公开作者与 raw 引用供核验，不直接定性为组织行为。
- [x] 重复评论 flag：same rpid duplicate display，按同一 raw page 内 rpid 重复展示持久化证据。
- [x] 重复评论 flag：same user duplicate submission，按公开 author_mid 和首见文本 hash 生成幂等关联。
- [x] 重复评论 flag：template-like comment，持久化跨视频模板候选及算法、阈值与 raw 证据。
- [x] 传播节点初版评分：按事件窗口输出 originator、amplifier、bridge、responder、official 候选分数、公开账号和可解释证据；不作为用户身份标签。
- [x] 事件转折点检测：输出关键词/评论相邻时间桶突增、热门 Top N 换血和显式 `major_creator` UID 介入的可回溯启发式信号。

## P4: Replay And Reports

- [x] 视频指标时间线回放：导出窗口内快照原值、相邻增量、间隔和 raw payload 证据，并使用窗口前最后快照作基线。
- [x] 热门评论历史回放：按成功热门首页快照还原 Top N 图文评论、公开作者、互动数、visibility 与 observation/raw/media 证据。
- [x] 评论消失/重现时间线：展开 visibility event 的前后 observation 原文、公开作者、媒体 hash 和 raw/page 证据，不将采集缺失等同于平台删除。
- [x] 事件传播链回放：按时间导出视频关联、楼中楼回应和跨视频模板传播的有向证据边，不补造无证据因果关系。
- [x] 报告生成器初版：输出可读 Markdown，并可选输出稳定 `event-report-v1` JSON 伴随文件。
- [x] 报告包含事件概述。
- [x] 报告包含数据覆盖情况。
- [x] 报告包含关键时间线。
- [x] 报告包含核心视频节点。
- [x] 报告包含热门评论变化。
- [x] 报告包含关键词趋势。
- [x] 报告包含模板化评论候选簇。
- [x] 报告包含结论限制。
- [x] 报告生成统一 evidence index，各章保留 observation、raw page、raw payload 和分析 flag 证据引用。

## P5: Operations And Scaling

- [x] 支持 raw storage backend 抽象：filesystem / MinIO；worker、raw inspect 和 doctor 共用后端工厂，media 保持本地文件系统。
- [x] 为 comment observations 设计月分区：提供 UTC 月边界/DDL 生成器和 v2 双写、回填、校验、切换、DEFAULT 分区及 rollback 契约；现有单主键表不自动原地转换。
- [x] 为大时间表建立 PostgreSQL-only BRIN 时间索引，启用 autosummarize；SQLite 不生成伪 B-tree，Alembic metadata 保持一致。
- [x] 增加 dry-run-first 数据库维护命令：ANALYZE、可选 VACUUM、BRIN summarization 和经 catalog 验证后的未来月分区，`--execute` 才执行。
- [x] 增加 worker health check：`service health` 独立验证新鲜 worker 角色心跳。
- [x] 增加任务积压监控：`service status` 输出 pending/running/failed、最老 pending 时间与活跃退避。
- [x] 增加请求失败率监控：`service status` 输出可配置滚动窗口内的请求页数、错误数、失败率与解析错误数。
- [x] 评估 TimescaleDB：当前不引入，先使用原生分区/BRIN/显式聚合，并记录行数、查询 p95、CPU 与 retention 复评阈值。
- [x] 评估 ClickHouse 分析副本：当前不引入；分析扫描、资源隔离或并发达到阈值后仅以 CDC 构建可重建只读副本。
- [x] 评估 OpenSearch / Meilisearch：当前先用 PostgreSQL FTS/`pg_trgm`；出现用户搜索 SLA、容错或独立扩缩容需求后复评。
- [x] 实现 PostgreSQL 行锁协调的跨进程 global/host/request-type 原子请求预算；保留单容器 Compose，并提供独立 scheduler、可配置 worker 副本的 split Compose，配置与生命周期测试已覆盖。

## Near-term Sprint

建议下一轮优先做：

1. [x] P1 Account And Cookie Management：二维码登录、加密快照、统一请求注入和自动刷新。
2. [x] P2 Event Archive：事件目标池调度、事件级覆盖率和基础时间线。
3. [x] 补全 Important Replies 的点赞增长、争议关键词和最近出现优先级。
4. [x] 在可用 Docker daemon 上执行镜像 build，并完成 Windows Ctrl+C、PostgreSQL service run 和容器连接宿主机 PostgreSQL 环境烟测。
5. [~] P1 Collection-First Snapshot Cohorts：C1 Evidence Foundations 已完成，下一步执行 C2 Cohort State And Policy，随后按 C3-C9 分阶段验收。

## Completion Audit Follow-up

上一轮复选框清零只证明既有清单已执行，不等于 ROADMAP 的整个项目已经完成。按最终验收标准补入以下缺口：

### P1: Basic Data Management

- [x] 支持更新事件名称、游戏、描述、状态、时间窗和时区，并保留稳定 slug。
- [x] 支持列出、停用和恢复事件 target；keyword target 状态同步到版本化关键词，UID 停用后不再参与 discovery。
- [x] 支持停用和恢复事件视频关联，历史采集与证据不删除。
- [x] 为事件、target 和视频生命周期操作提供 CLI、边界校验、repository 测试和 `docs/EVENTS.md` 使用说明。

### P4: Window-Accurate Reports

- [x] 事件 coverage summary 支持 `since` / `until`，CLI 和报告均按 `finished_at` 的 `[since, until)` 汇总，窗外失败不会污染报告覆盖结论。
- [x] 报告允许按 active BVID 和关键词缩小 coverage、timeline 与分析查询范围，并在 Markdown/JSON 中记录规范化筛选条件。

### P5: Operational Completion

- [x] 增加可配置失败告警规则、周期调度和 `operational_alert_states` 去重/恢复状态，覆盖 worker 心跳、任务积压、失败率和连续调度失败；active 告警进入 `service status`，默认仅记录日志。
- [x] 增加 raw payload filesystem -> MinIO 迁移/校验 CLI，复制后校验 hash，数据库 URI 仅在目标对象验证成功后更新，并支持 dry-run。

### End-to-End Acceptance

- [x] 提供可重复的真实 Bilibili API smoke：BV 入队、视频指标、热门评论、latest frontier、图片下载、coverage、raw inspect 全链路。
- [x] 用真实采集数据创建事件并生成 timeline、分析输出和 evidence-backed report，记录命令、产物及覆盖限制。

### Operator Documentation

- [x] 建立 `docs/README.md` 文档索引和从零到长期服务的 `USER_GUIDE.md`。
- [x] 完整记录当前 CLI、配置、生效/保留字段和环境变量覆盖。
- [x] 完整记录 task、discovery、视频/评论、latest frontier、watchlist、raw 和 media 采集链路。
- [x] 完整记录全部数据库表、逻辑引用、hash、append-only 语义和备份一致性。
- [x] 完整记录所有分析/replay/report schema、算法口径、限制和复现要求。
- [x] 提供 Docker、Linux、Windows 共用的长期运维、备份恢复、容量、升级和故障排查手册。
