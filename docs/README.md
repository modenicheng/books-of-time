# Books of Time Documentation

本目录分为操作者手册、架构与数据说明、项目计划记录三层。日常使用从 [USER_GUIDE](USER_GUIDE.md) 开始，不需要先阅读 `superpowers/` 中的设计过程文件。

## Start Here

| 目标 | 文档 | 内容 |
| --- | --- | --- |
| 从零开始运行 | [USER_GUIDE](USER_GUIDE.md) | 安装、初始化、首次采集、事件、分析、证据复核和长期服务 |
| 配置应用 | [CONFIGURATION](CONFIGURATION.md) | YAML 全部配置段、环境变量覆盖、限流和安全边界 |
| 查询命令 | [CLI_REFERENCE](CLI_REFERENCE.md) | 当前所有公开 CLI、参数、默认值、输出和副作用 |
| 理解采集 | [COLLECTION](COLLECTION.md) | 视频、热门评论、latest frontier、楼中楼、coverage、raw 和 media |
| 理解数据 | [DATA_MODEL](DATA_MODEL.md) | PostgreSQL 表、append-only 观测、hash 和证据关系 |
| 管理事件 | [EVENTS](EVENTS.md) | 事件、target、视频关联和生命周期操作 |
| 运行分析 | [ANALYSIS](ANALYSIS.md) | 趋势、共现、立场证据、模板候选、传播节点、转折、回放和报告 |
| 管理账号 | [LOGIN](LOGIN.md) | 二维码登录、Cookie 注入、自动刷新、登出和本地加密文件 |
| 日常运维 | [OPERATIONS](OPERATIONS.md) | 常驻服务、任务队列、健康状态、告警、数据库维护和备份恢复 |
| 部署服务 | [DEPLOYMENT](DEPLOYMENT.md) | Docker、split Compose、Linux systemd、Windows 开发和升级 |
| 排查故障 | [TROUBLESHOOTING](TROUBLESHOOTING.md) | 数据库、任务、限流、Cookie、raw/media、报告和服务问题 |
| 复现真实验收 | [REAL_DATA_SMOKE](REAL_DATA_SMOKE.md) | 真实 Bilibili 数据的端到端命令、结果和覆盖限制 |

## Reading Paths

### 第一次使用

1. [USER_GUIDE](USER_GUIDE.md)
2. [CONFIGURATION](CONFIGURATION.md)
3. [LOGIN](LOGIN.md)，登录是可选步骤
4. [COLLECTION](COLLECTION.md)
5. [EVENTS](EVENTS.md) 与 [ANALYSIS](ANALYSIS.md)

### 部署长期服务

1. [DEPLOYMENT](DEPLOYMENT.md)
2. [CONFIGURATION](CONFIGURATION.md)
3. [OPERATIONS](OPERATIONS.md)
4. [TROUBLESHOOTING](TROUBLESHOOTING.md)

### 核验研究结论

1. [DATA_MODEL](DATA_MODEL.md)
2. [COLLECTION](COLLECTION.md) 中的 coverage 与 frontier 语义
3. [ANALYSIS](ANALYSIS.md) 中的算法边界
4. [REAL_DATA_SMOKE](REAL_DATA_SMOKE.md)

## Project And Engineering Records

- [ROADMAP](ROADMAP.md)：长期目标、阶段范围和验收标准。
- [TODO](TODO.md)：已实现能力的执行清单。
- [PARTITIONING](PARTITIONING.md)：`comment_observations` 月分区迁移契约；当前仍是普通表。
- [SCALING_DECISIONS](SCALING_DECISIONS.md)：TimescaleDB、ClickHouse 和搜索系统的当前采用结论。
- [SCALING_EVALUATION](SCALING_EVALUATION.md)：复评阈值、基准门槛和回滚原则。
- `superpowers/specs/`：设计决策记录，不是日常使用说明。
- `superpowers/plans/`：历史实现计划，不代表当前 CLI 文档。
- `fix/`：审计修复日志和引入/修复 commit 记录。

## Documentation Conventions

- 仓库没有安装名为 `bot` 的 console script。文档统一使用 `uv run python main.py ...`。
- 命令中的 `<BVID>`、`<EVENT>`、`<RAW_ID>` 和 `<OUTPUT>` 是需要替换的占位符，不要连尖括号一起输入。
- 所有分析时间参数必须带 UTC offset，例如 `2026-07-13T11:50:00+08:00` 或 `2026-07-13T03:50:00Z`。
- `since` / `until` 分析窗口统一按 `[since, until)` 解释，结束时间不包含在窗口内。
- `database maintain` 和 `raw migrate-minio` 默认 dry-run；只有显式传入 `--execute` 才修改数据或存储索引。
- PostgreSQL 是结构化事实源；raw payload 和本地 media 文件是证据集的一部分，备份和恢复时不能拆开处理。
- 平台公开 `author_mid` 和 `author_name` 会保留用于人工核验。传播角色和相似文本结果只在事件和证据窗口内解释，不是用户长期身份标签。
