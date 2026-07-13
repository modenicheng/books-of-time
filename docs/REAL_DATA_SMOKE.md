# Real Bilibili Data Smoke

本文记录一套可重复的真实 Bilibili API 验收流程，以及 2026-07-13 在 Windows、PostgreSQL 18.3、Python 3.12.11 上的实际结果。所有采集请求均经过 Books of Time 的统一 HTTP、限流、退避与 raw archive 链路；验收时账号状态为 `anonymous`。

这是固定时点的验收记录，不替代当前接口文档。完整流程见 [USER_GUIDE](USER_GUIDE.md)，采集状态语义见 [COLLECTION](COLLECTION.md)，分析输出字段见 [ANALYSIS](ANALYSIS.md)。

真实平台数据会变化，下面的数量是本次证据，不是未来运行必须匹配的固定断言。样本失效时应替换为仍公开可见的视频，并保留相同验收步骤和覆盖限制。

## Preconditions

```bash
uv run alembic upgrade head
uv run python main.py service doctor
uv run python main.py login status
```

`service doctor` 必须通过数据库、schema、raw storage 和 media storage 检查。匿名状态不阻止公开接口采集；登录是独立管理能力。

## Collection Smoke

本次使用两个公开视频：

- `BV1kZNN6iEPq`：热门评论含多张图片，用于验证评论、楼中楼和 media 链路。
- `BV1gkDNBoEsi`：评论量较小，用于在有限请求内完成 latest baseline 的 tail scan 与 head sweep。

先采集图片样本。`worker loop --stop-when-idle` 会继续处理热门评论派生的楼中楼和 media 任务：

```bash
uv run python main.py monitor-video BV1kZNN6iEPq --priority 100
uv run python main.py video comments BV1kZNN6iEPq --mode hot --tier c --page-limit 1 --priority 80
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
```

再采集低评论量样本。首次 latest 任务完成 tail scan，coverage 应为 `baseline_tail_complete`；第二次任务从 head 回扫到首次采集起点，之后才建立正式 frontier：

```bash
uv run python main.py monitor-video BV1gkDNBoEsi --priority 100
uv run python main.py video comments BV1gkDNBoEsi --mode hot --tier c --page-limit 1 --priority 80
uv run python main.py collect-latest-comments BV1gkDNBoEsi --priority 70 --max-scan-seconds 55
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle

uv run python main.py collect-latest-comments BV1gkDNBoEsi --priority 70 --max-scan-seconds 55
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
uv run python main.py coverage BV1gkDNBoEsi --limit 20
```

大型评论区的 tail scan 可能跨多个 55 秒任务片段。此时持续运行正式 worker 即可；不要把 `baseline_paused` 或 `baseline_tail_complete` 当作正式 `baseline_complete`。

## Raw And Media Evidence

本次运行可直接复查以下证据：

```bash
uv run python main.py raw inspect 13 --preview-bytes 240
uv run python main.py raw inspect 17 --preview-bytes 32
uv run python main.py raw inspect 30 --preview-bytes 240
```

- raw `13` 是 `bilibili:comment_hot` JSON，HTTP 200，解析版本 `comments.v2`。
- raw `17` 是 `bilibili:media_image` GIF，HTTP 200；二进制预览以 ASCII hex 输出。
- raw `30` 是完成 head sweep 的 `bilibili:comment_latest` JSON，HTTP 200。
- 热门页发现 4 个图片引用，下载后得到 4 个本地 `media_asset`：1 个 GIF 和 3 个 JPEG。
- 图片尺寸分别包含 `640x360` 和 `2652x1200`；4 个 source 均回填 asset，文件位于 `data/media/sha256/`。
- 对 raw `11,13-21,28,30` 和 4 个 media 文件重新读取后，SHA-256 与未压缩尺寸均和数据库一致。

未来运行的 raw ID 会不同。事件报告 JSON 的 `evidence_index` 可找到报告引用的 `raw_payload` ID；media 下载 raw ID 位于 `media_assets.download_raw_payload_id`。

## Event And Report Smoke

本次创建事件 `smoke-real-bilibili-20260713`，关联两个 seed BVID 和关键词“鬼图”。新的重复验收应使用新的日期或时间后缀，避免覆盖历史证据。

```bash
uv run python main.py event create smoke-real-bilibili-20260713 --name "真实 Bilibili 全链路验收 2026-07-13" --game "验收样本" --status active --start-at 2026-07-13T11:50:00+08:00 --end-at 2026-07-13T13:00:00+08:00 --timezone Asia/Shanghai
uv run python main.py event add-target smoke-real-bilibili-20260713 seed_bvid BV1kZNN6iEPq --priority 100
uv run python main.py event add-target smoke-real-bilibili-20260713 seed_bvid BV1gkDNBoEsi --priority 100
uv run python main.py event add-target smoke-real-bilibili-20260713 keyword "鬼图" --priority 80
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
```

seed target 会自动产生视频指标任务。处理完派生任务后，生成事件产物：

```bash
uv run python main.py event coverage smoke-real-bilibili-20260713 --since 2026-07-13T11:50:00+08:00 --until 2026-07-13T13:00:00+08:00
uv run python main.py event export-timeline smoke-real-bilibili-20260713 --output data/smoke/2026-07-13/timeline.jsonl
uv run python main.py event keyword-trends smoke-real-bilibili-20260713 --since 2026-07-13T11:50:00+08:00 --until 2026-07-13T13:00:00+08:00 --bucket-minutes 5 --output data/smoke/2026-07-13/keyword-trends.jsonl
uv run python main.py event propagation-nodes smoke-real-bilibili-20260713 --since 2026-07-13T11:50:00+08:00 --until 2026-07-13T13:00:00+08:00 --output data/smoke/2026-07-13/propagation-nodes.jsonl
uv run python main.py event turning-points smoke-real-bilibili-20260713 --since 2026-07-13T11:50:00+08:00 --until 2026-07-13T13:00:00+08:00 --bucket-minutes 5 --min-count 2 --output data/smoke/2026-07-13/turning-points.jsonl
uv run python main.py event replay-propagation smoke-real-bilibili-20260713 --since 2026-07-13T11:50:00+08:00 --until 2026-07-13T13:00:00+08:00 --output data/smoke/2026-07-13/propagation-replay.jsonl
uv run python main.py event report smoke-real-bilibili-20260713 --since 2026-07-13T11:50:00+08:00 --until 2026-07-13T13:00:00+08:00 --bucket-minutes 5 --spike-min-count 2 --output data/smoke/2026-07-13/event-report.md --json-output data/smoke/2026-07-13/event-report.json
```

## Acceptance Record

本次最终结果：

- 事件 coverage：2/2 active 视频有覆盖，8/8 页面成功，16 个直接观测项，12 个 raw payload；请求错误、解析错误、partial、truncated 和 corrupted 均为 0。
- timeline JSONL：81 行；报告关键时间线为 82 项，其中额外包含 1 个启发式转折信号。
- 关键词趋势：14 个 5 分钟时间点；传播节点和传播回放各 68 项。
- 报告 schema 为 `event-report-v1`，包含 2 个核心视频、14 个关键词趋势点和 86 个 evidence index 项。
- evidence index 中 76 个 `comment_observation` 和 10 个 `raw_payload` 引用均在 PostgreSQL 中成功解析，无缺失 ID。
- 另生成按 `BV1gkDNBoEsi` 与“鬼图”过滤的 Markdown/JSON 报告，coverage 缩小为 1 个视频和 5 条采集记录。

本次样本只有一个关键词，因此关键词共现边为 0；样本窗口短且评论量有限，因此没有模板候选或分析 flag。热门评论显式限制为 1 页，事件 coverage 只汇总视频目标，不把 comment/media 派生任务伪装成视频覆盖。这些都是报告解释边界，不影响采集、证据链和基础分析模块的 smoke 结论。

运行时产物保存在 `data/smoke/2026-07-13/`，该目录随 `data/` 被 git 忽略；数据库、raw 和 media 文件仍应作为同一证据集备份。
