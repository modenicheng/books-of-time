# Scaling Technology Evaluation

## Decision

截至 2026-07-11，生产主路径继续只依赖 PostgreSQL。本阶段不引入
TimescaleDB、ClickHouse、OpenSearch 或 Meilisearch。raw payload 和本地 media
仍是证据源，任何未来分析副本或搜索索引都只能是可重建的派生数据，不能成为
raw evidence 的唯一持有者。

当前优先级是先使用已经落地的 UTC 月分区迁移方案、PostgreSQL-only BRIN
时间索引、受限查询、维护命令和查询观测。没有测量数据证明这些能力不足前，
新增数据库会增加备份、升级、权限、监控、一致性和恢复演练成本，却不会提高
采集正确性。

## TimescaleDB

**当前结论：不需要。** Timescale hypertable 会按时间自动拆成 chunks，continuous
aggregate 会增量维护时间桶汇总。这与指标时间线和关键词趋势有潜在匹配点，
但 comment observation 的复合身份、跨表 evidence join 和现有 v2 分区迁移仍需
解决，扩展本身不会消除这些约束。

先使用 PostgreSQL 原生月分区、BRIN 和显式聚合表。满足任一条件时复评：

- 单张时间表超过 500,000,000 行或 1 TiB。
- 合理分区裁剪与索引后，常用时间桶查询 p95 仍超过 5 秒。
- 小时/日聚合刷新持续占用超过 20% 数据库 CPU，或维护窗口无法容纳。
- 团队明确需要自动 retention/downsampling，并完成 raw evidence 保留审查。

参考：[Timescale hypertables](https://docs.timescale.com/use-timescale/latest/hypertables/)、
[continuous aggregates](https://docs.timescale.com/use-timescale/latest/continuous-aggregates/about-continuous-aggregates/)。

## ClickHouse

**当前结论：不需要分析副本。** ClickHouse 可通过 PostgreSQL table integration
查询数据，也可通过 CDC 将事务库变化同步到分析库。它适合高并发、大范围列式
扫描，但需要额外的数据模型、复制延迟监控、回填和一致性校验。

满足任一条件时复评只读分析副本：

- 报告或探索查询扫描超过 100,000,000 行，且 p95 超过 10 秒。
- 分析负载长期占 PostgreSQL 超过 30% CPU/I/O，开始影响 collector 写入延迟。
- 同时分析查询超过 10 个，PostgreSQL 资源隔离仍不能满足 SLA。
- 已定义 CDC 延迟、断点续传、全量重建和 PostgreSQL/raw 对账验收。

优先使用 CDC，而不是应用双写；PostgreSQL 继续是事实源。参考：
[ClickHouse PostgreSQL integration](https://clickhouse.com/integrations/postgres)、
[Postgres CDC connector](https://clickhouse.com/blog/postgres-cdc-connector-clickpipes-ga)。

## Full-Text Search

**当前结论：先使用 PostgreSQL。** 第一阶段采用 PostgreSQL full-text search 与
`pg_trgm` 候选检索，再由事件、时间和作者等结构化字段过滤。`pg_trgm` 提供文本
相似度和可索引的相似搜索，足够支撑内部核验工具的初期需求。

满足任一条件时复评独立搜索服务：

- 出现面向用户的搜索即输即得、拼写容错、高亮、同义词或复杂相关性排序需求。
- 代表性中文语料和并发下，PostgreSQL 搜索 p95 超过 500 ms。
- 搜索索引写入/维护开始影响采集事务，或需要独立扩缩容。

需要复杂查询 DSL、多字段聚合和索引生命周期管理时优先评估 OpenSearch；只需要
轻量应用搜索、前缀和拼写容错时优先评估 Meilisearch。两者都必须使用稳定
observation/asset ID，并支持从 PostgreSQL 全量重建和按 raw evidence 回链。

参考：[PostgreSQL pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html)、
[OpenSearch full-text queries](https://docs.opensearch.org/latest/query-dsl/full-text/index/)、
[Meilisearch full-text search](https://www.meilisearch.com/docs/capabilities/full_text_search/overview)。

## Review Procedure

每季度或发生上述阈值时，从 `service status`、PostgreSQL `pg_stat_statements`、表
尺寸、查询计划和报告耗时中收集一周数据。评估文档必须附真实 p50/p95、峰值写入
速率、恢复时间和额外运维成本；不能只用总行数或产品宣传决定引入新系统。
