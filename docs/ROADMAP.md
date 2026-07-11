# Books of Time Roadmap

Books of Time 是一个面向 Bilibili 二游社区公开视频与评论区的时间序列归档系统，用于在合规请求预算内记录、回放和分析游戏社区舆论事件的形成、扩散、转向与沉淀过程。

一句话目标：

```text
Books of Time = 二游社区公共舆论状态的时间机器
```

## Guiding Principles

- 只采集公开数据，不绕过平台风控，不建设代理池、账号池或用于扩大请求量的 Cookie 池；允许一个用户通过二维码维护自己的单账号凭据，并自动刷新其 Cookie。
- 所有请求走统一请求后端，统一限流、退避、审计和 raw payload 归档。
- 结构化数据 append-only，历史状态不覆盖。
- 不声称评论全量覆盖，所有报告都必须展示覆盖率、失败窗口和不确定性。
- 保留平台公开用户字段用于人工核验；分析聚焦公开内容、传播结构和事件内角色，不给普通用户贴身份标签或建立长期画像。
- PostgreSQL 作为第一阶段主库，后期按瓶颈再引入 MinIO、TimescaleDB、ClickHouse 或全文检索。

## Operating Model

Books of Time 是长期运行服务，不以人工维持多个 CLI loop 作为正式运行方式。

- Docker 默认保留单个 Books of Time 应用容器；扩展形态使用独立 scheduler 与可水平扩展的 worker 容器，二者连接宿主机或局域网已有 PostgreSQL，不捆绑数据库容器。
- raw payload 和 media asset 使用宿主机挂载的本地文件系统；media 不迁移到 S3、OSS 或 MinIO。
- Linux 原生部署使用同一服务内核和 systemd；Windows 保留直接运行和调试入口。
- `ServiceHost` 可运行 scheduler、worker 或二者，并为当前实例维护 heartbeat。
- PostgreSQL 持久化 collection task、scheduled job、lease 和 service heartbeat，进程重启后可恢复。
- PostgreSQL `request_budget_states` 通过行锁原子保留 global、host 与 request-type 令牌，使多个 worker 副本共享请求预算；配置漂移会明确失败。
- CLI 保留为管理和诊断界面，`bot service run` 是正式常驻入口。
- 登录是独立管理 CLI；服务没有 Cookie 或 Cookie 失效时继续按匿名能力运行，不把采集生命周期绑定到登录态。

详细设计见 `docs/superpowers/specs/2026-07-10-long-running-service-design.md`。

## Current Baseline

已完成的基础能力：

- 标准 `books_of_time` 包结构。
- PostgreSQL ORM 模型、repository、schema 初始化。
- PG-backed collection task 队列基础模型。
- 视频指标采集 worker 闭环：CLI 入队、worker lease、请求、raw 归档、解析、宽表快照入库。
- bilibili-api-python 自定义 request client 后端，接入现有限流与 raw evidence 管线。
- 阶梯式视频快照策略的纯函数测试。
- UID 投稿发现调度的基础去重和新视频触发任务逻辑。
- pytest + Ruff 验证基础。

## Phase 1: Data Foundation

目标：稳定、合规、可审计地采集视频指标和评论快照。

### Scope

- 视频实体与指标快照。
- 热门评论页面采集。
- 最新评论 frontier 增量采集。
- 楼中楼重点采集。
- 评论图片引用、图片实体、评论-图片关系和本地图片文件归档。
- raw payload 归档和 raw page observation。
- 评论实体、评论观测、状态事件。
- 采集覆盖率统计。
- 任务队列、限流、失败退避、运行审计。

### Acceptance Criteria

- 输入 BV 后可以生成任务、限流请求、保存 raw、写入 `video_metric_snapshots`。
- 输入 BV 后可以抓热门评论第一页并写入 `comment_entities` 与 `comment_observations`。
- 最新评论可以从第一页向旧 frontier 扫描，遇到 frontier 停止，未遇到时标记 truncated。
- 评论中的图片引用可以登记为 `media_sources`，图片二进制可以本地保存为去重后的 `media_assets`，并通过 `comment_observation_media` 关联到评论观测。
- 每轮采集可以输出覆盖率：热门页数、最新评论 frontier 是否到达、楼中楼 root 数、请求成功率。
- raw payload 可以根据数据库索引找到原始文件，并可重新解析。
- 请求失败不会伪装成数据缺失，403/429/timeout 有明确退避状态。

### Non-goals

- 不做复杂分析模型。
- 不做前端 dashboard。
- 不做用户长期画像。
- 不做全量深页评论实时采集。
- 不在采集主链路中做相似图片聚类；相似分析必须离线、可重跑。

## Phase 2: Event Archive

目标：把分散的视频、评论、关键词组织成可追踪的舆论事件。

### Scope

- 事件实体：游戏、事件名、时间范围、状态、描述。
- 事件目标池：官方号、矩阵号、关键词、种子 BV、相关 UP。
- 视频与事件的关联。
- 评论与事件的关联规则。
- 事件关键词词表与版本化。
- 事件级覆盖率汇总。

### Acceptance Criteria

- 可以创建一个事件，并配置目标 UID 池、关键词和种子视频。
- scheduler 可以基于事件目标池进行每分钟 UID 投稿发现。
- 新发现视频能进入事件，并自动触发视频指标快照任务。
- 事件页面或 CLI 可以列出相关视频、采集状态、覆盖率和 raw 证据数量。
- 可以导出事件的基础时间线数据。

### Non-goals

- 不自动判断事件真相。
- 不把弱相关内容强行归入事件。
- 不把普通用户身份作为事件维度。

## Phase 3: Opinion Evolution Analysis

目标：分析话题如何扩散、何时转向、哪些公开节点放大。

### Scope

- 关键词趋势：出现时间、频率、共现、跨视频传播。
- 热门评论换血：Top N 变化率、位置变化、点赞/回复分桶变化。
- 模板化评论候选：相似文本、短时间共现、跨视频出现。
- 传播节点识别：originator、amplifier、bridge、responder、official、media_or_kol。
- 事件转折检测：评论突增、关键词突增、大 UP 介入、官方回应、集中消失。

### Acceptance Criteria

- 可以对一个事件计算关键词时间序列。
- 可以对一个视频生成热门评论变化摘要。
- 可以输出疑似模板化传播簇，并附带证据窗口和置信度说明。
- 可以识别关键公开视频节点及其影响理由。
- 分析输出必须包含覆盖率和限制说明。

### Non-goals

- 不输出“某普通用户是水军”。
- 不把相似文本直接等同于组织化行为。
- 不将低覆盖率数据包装成确定结论。

## Phase 4: Replay And Reports

目标：形成可读、可复查、可引用的事件回放与研究型报告。

### Scope

- 视频状态时间线回放。
- 评论区热门排序回放。
- 事件传播链回放。
- 事件报告生成器。
- raw 证据链跳转和重解析工具。
- 覆盖率、不确定性、采集缺口展示。

### Acceptance Criteria

- 可以选择事件、视频、关键词和时间段，回放当时状态。
- 可以生成包含事件概述、覆盖情况、关键时间线、核心视频节点、热门评论变化、关键词趋势、模板化评论簇、结论限制的报告。
- 报告中每个关键结论可以追溯到 observation、raw page 或 raw payload。
- 报告措辞保持研究型，不做判案型断言。

### Non-goals

- 不追求营销式可视化优先。
- 不隐藏采集失败窗口。
- 不生成缺少证据链的结论。

## Phase 5: Scaling And Operations

目标：在保持合规和可审计的前提下，提高长期运行稳定性和分析性能。

### Scope

- MinIO 或 S3-compatible raw storage。
- PostgreSQL 分区与索引治理。
- 可选 TimescaleDB 存储视频指标和聚合指标。
- 可选 ClickHouse 分析副本。
- 可选 OpenSearch 或 Meilisearch 做全文检索。
- 采集运行监控、失败告警、任务积压观察。
- Docker 应用容器、Linux systemd 和 Windows 开发共用的长期服务内核。
- 服务实例心跳、持久化调度作业、优雅停止和重启恢复。

### Acceptance Criteria

- raw payload 可以从本地文件系统平滑迁移到对象存储。
- 大表有明确分区策略和维护脚本。
- 长期 worker 运行有基本健康检查。
- 请求预算和失败退避可配置、可观察。
- Docker 和 Linux 原生部署可以连接已有 PostgreSQL，不要求额外数据库实例。

## System Success Criteria

项目成功不是因为“爬得最多”，而是因为它能做到：

1. 记录当时公开可见状态。
2. 保留原始响应证据。
3. 解释数据覆盖范围。
4. 追踪评论区变化。
5. 还原事件传播链。
6. 避免普通用户画像滥用。
7. 让争议分析从截图争吵变成时间序列证据。
