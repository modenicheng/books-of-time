# Analysis, Replay And Reports

本文覆盖当前所有公开分析命令、算法口径、输出 schema、运行顺序和解释边界。命令参数速查见 [CLI_REFERENCE](CLI_REFERENCE.md)，事件范围管理见 [EVENTS](EVENTS.md)。

## 1. Common Contract

### Time Windows

所有 `--since` / `--until` 必须带 UTC offset：

```text
2026-07-13T00:00:00+08:00
2026-07-12T16:00:00Z
```

窗口均按 `[since, until)` 处理。naive datetime、`until <= since` 会报错。

### Event Scope

事件分析通常只读取 active `event_videos` 和 active event keyword 的最新版本。例外会在对应章节明确说明。

事件本身的 start/end 不会自动裁剪命令窗口。operator 应显式传入研究时间，并使用 `event coverage` 检查同一窗口的数据质量。

### Output Files

- replay、timeline 和独立 analyzer 使用 UTF-8 JSONL，每行一个 object。
- event report 输出 UTF-8 Markdown，可选输出一个格式化 JSON object。
- writer 先写同目录临时文件，再原子替换目标文件。
- 输出目录不存在时自动创建。
- 无结果的 JSONL 是合法的 0-byte 空文件，不会写伪造的空记录。
- 超出上限时命令失败，不静默截断。

### Evidence Before Interpretation

推荐每次分析前执行：

```bash
uv run python main.py event coverage <EVENT> --since <SINCE> --until <UNTIL>
uv run python main.py task list --status failed --limit 100
```

分析只能解释已采到的公开数据。以下说法都不成立：

- “没有 observation”等于“平台删除”。
- “文本相似”等于“组织协同”。
- “role score 高”等于“该用户长期属于某角色”。
- “turning point signal”等于“因果转折已证明”。

## 2. Recommended Event Workflow

```text
1. 创建事件和 target
2. 确认 active event videos / keywords
3. 运行采集并检查 coverage
4. refresh-comment-flags 持久化模板/重复证据
5. 运行独立趋势、节点、转折和 replay
6. 生成 Markdown + JSON report
7. 从 evidence_index 回查 raw
```

典型命令：

```bash
uv run python main.py event refresh-comment-flags <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/flags.jsonl
uv run python main.py event propagation-nodes <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/nodes.jsonl
uv run python main.py event turning-points <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/turning-points.jsonl
uv run python main.py event replay-propagation <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/propagation.jsonl
uv run python main.py event report <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/event.md --json-output data/reports/event.json
```

`refresh-comment-flags` 不是生成 report 的硬前置，但 propagation node 的 originator/amplifier 和 propagation replay 的 template edge 依赖已持久化的 template flags。

## 3. Video-Level Analysis

### Hot Comment Turnover

```bash
uv run python main.py video hot-turnover <BVID> --since <SINCE> --until <UNTIL> --top-n 20 --output data/reports/hot-turnover.jsonl
```

只读取成功的热门评论第 1 页，按时间比较相邻快照。`top_n` 范围 1–20。少于两张快照时输出空文件。

换血率：

```text
1 - retained_count / max(previous_count, current_count, 1)
```

`hot-comment-turnover-v1` 字段：

| 字段 | 含义 |
| --- | --- |
| `bvid`, `top_n` | 视频和比较范围 |
| `previous_at`, `current_at` | 相邻快照时间 |
| `previous_raw_page_id`, `current_raw_page_id` | 两个页面证据 ID |
| `previous_rpids`, `current_rpids` | 顺序保留的 Top N RPID |
| `retained_count` | 两组交集大小 |
| `entered_rpids`, `exited_rpids` | 新进入/退出项，保留各侧顺序 |
| `turnover_rate` | 0–1 换血率 |

它比较可见集合，不证明评论被删除；退出 Top N 也可能只是排名变化。

## 4. Video Replay

### Metric Replay

```bash
uv run python main.py video replay-metrics <BVID> --since <SINCE> --until <UNTIL> --max-points 100000 --output data/reports/metrics.jsonl
```

`max_points` 范围 1–1000000。analyzer 会读取窗口前最后一张 metric 作为第一条窗口内记录的基线，但不会把该基线单独输出。

`video-metric-replay-v1` 字段：

- `bvid`, `captured_at`, `previous_at`, `elapsed_seconds`
- `metrics`：view/like/coin/favorite/share/reply/danmaku 当前值
- `deltas`：当前值和前值都非 NULL 时才出现该 key，允许负值
- `raw_payload_id`, `previous_raw_payload_id`

负增量可能来自平台校正、计数回退或数据差异，不能自动当作采集错误。

### Hot Comment Replay

```bash
uv run python main.py video replay-hot-comments <BVID> --since <SINCE> --until <UNTIL> --top-n 20 --max-snapshots 10000 --output data/reports/hot-replay.jsonl
```

`top_n` 1–20，`max_snapshots` 1–100000。每行是一张成功热门第 1 页快照。

`hot-comment-replay-v1` 顶层字段：

- `bvid`, `captured_at`
- `raw_page_observation_id`, `raw_payload_id`
- `top_n`
- `comments`

每个 comment 包含 position、rpid、原文/content hash、互动数、公开作者、visibility、observation/raw ID、两个 media hash，以及按 position 排序的 source/asset link。图片下载尚未完成时 `media_asset_id` 可为 NULL。

### Visibility Replay

```bash
uv run python main.py video replay-visibility <BVID> --since <SINCE> --until <UNTIL> --max-events 100000 --output data/reports/visibility.jsonl
```

`max_events` 范围 1–1000000。每行一个 folded/unfolded/disappeared/reappeared event。

`comment-visibility-replay-v1` 字段：

- `event_id`, `bvid`, `rpid`, `event_type`, `occurred_at`
- `old_visibility`, `new_visibility`, `missing_reason`
- `previous_observation`, `current_observation`
- `interpretation_limit`

observation evidence 包含原文、公开作者、互动数、visibility、raw/page ID 和 media hashes。disappeared 一侧可能没有 current observation。

## 5. Event Timeline

```bash
uv run python main.py event export-timeline <EVENT> --output data/reports/timeline.jsonl
```

当前公开命令导出事件全部关联视频的全历史，没有 `--since`、`--until` 或 max-records 参数；大事件应优先使用有界 report，或在导出后按 timestamp 处理。

`event-timeline-v1` 公共字段：

- `event_id`, `event_slug`, `timestamp`
- `record_type`, `source_table`, `source_key`, `bvid`
- `data`

record type：

| 类型 | `data` 主要内容 |
| --- | --- |
| `event_video_associated` | active、reason、confidence、source target |
| `video_metric_snapshot` | 全部指标和 raw ID |
| `comment_state_event` | rpid、type、前后 observation、old/new value |
| `comment_visibility_event` | rpid、type、前后 observation、visibility、missing reason |

timeline 包含 active 和 inactive event-video 历史关联。它不直接包含全部 comment observation；只包含由 observation 派生的状态/可见性事件。

## 6. Keyword Trends

```bash
uv run python main.py event keyword-trends <EVENT> --since <SINCE> --until <UNTIL> --bucket-minutes 60 --output data/reports/keyword-trends.jsonl
```

可用 `--bvid` 缩小到一个 active event video。bucket 1–1440 分钟，最多 10000 个 bucket。analyzer 对每个 active normalized keyword 选择最高 version，并输出完整时间序列，包括零值桶。

匹配方式是 comment observation 原文 casefold 后的子串匹配。

`keyword-trend-v1` 字段：

- event ID/slug、`scope_type` / `scope_id`
- keyword ID、原文、normalized value、version
- `bucket_start`, `bucket_end`
- `distinct_comment_count`：去重 RPID 数
- `observation_count`：所有命中 observation 数

同一评论反复被采到会增加 observation count，但不会增加该桶内 distinct comment count；同一 rpid 若跨桶出现，会分别计入各桶。

## 7. Keyword Co-occurrence

```bash
uv run python main.py event keyword-cooccurrence <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/cooccurrence.jsonl
```

可用 `--bvid`。同一条 comment observation 同时包含两个 active keyword 才形成一条边。少于两个 active keyword 或无共同命中时输出空文件。

`keyword-cooccurrence-v1` 字段：

- event/scope
- keyword A/B 的 ID、原文和 version
- `distinct_comment_count`
- `observation_count`

该结果表示文本共同出现，不表示语义关系、赞同或反对。

## 8. Stance Lexicon Evidence

先在 YAML 配置版本化词表：

```yaml
analysis:
  stance_lexicon:
    version: 2026-07-v1
    support: ["赞同", "支持"]
    criticism: ["质疑", "反对"]
    neutral: ["求证", "观望"]
```

运行：

```bash
uv run python main.py event stance-evidence <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/stance.jsonl
```

文本和词项都使用 NFKC、casefold 和空白归一化。同一 normalized term 不能配置在多个类别；一条评论如果分别命中不同词项，仍可能进入多个类别汇总。

输出固定为 support、criticism、neutral 三条 `stance-evidence-v1`：

- event/scope/window
- lexicon version、category 和该类 terms
- distinct comment count、observation count
- `matched_term_counts`

这是词表命中统计，不是情感模型，也不会给单个用户持久贴标签。

## 9. Template Candidates

```bash
uv run python main.py event template-candidates <EVENT> --since <SINCE> --until <UNTIL> --window-minutes 60 --min-similarity 0.85 --min-text-chars 8 --max-comments 5000 --max-comparisons 100000 --output data/reports/templates.jsonl
```

有效范围：

| 参数 | 范围 |
| --- | --- |
| window | 1–1440 分钟 |
| similarity | 0.5–1.0 |
| min text chars | 4–1000 |
| max comments | 2–50000 |
| max comparisons | 1–5000000 |

算法：

1. 只读 comment entity 的首见正文和首见时间。
2. NFKC + casefold，只保留字母和数字。
3. 只比较不同 BVID、时间差不超过 window 的评论。
4. 先做长度比上界过滤，再用 `difflib.SequenceMatcher(autojunk=False)`。
5. similarity 达到阈值才输出。

`template-candidate-v1` 保存左右评论完整可核验信息：RPID、BVID、公开作者、原文、首见时间/raw ID，以及 similarity、时间差、候选原因、算法、阈值、窗口和解释限制。

该 analyzer 要求跨视频。直接传 `--bvid` 会把范围缩到一个视频，因此当前实现不会产生跨视频 pair；该参数主要保持分析接口一致，不适合模板发现主流程。

## 10. Persistent Comment Flags

```bash
uv run python main.py event refresh-comment-flags <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/flag-refresh.jsonl
```

命令会写数据库，不只是导出。它生成三类候选：

| flag type | 证据 |
| --- | --- |
| `same_rpid_duplicate_display` | 同一 raw page 内相同 rpid 出现多次 |
| `same_user_duplicate_submission` | 同一公开 author MID 在窗口内提交规范化后完全相同的首见文本 |
| `template_like_comment` | 上一节的跨视频短时间相似文本 pair |

对应 algorithm version 分别是 `raw-page-duplicate-v1`、`normalized-exact-v1`，以及包含 similarity/window/minchars 参数的动态 `sequence_matcher-v1:*` 版本字符串。

stable key 包含 event、type、subject/related、algorithm version；重复运行相同算法版本不会复制相同 flag。

refresh 当前只新增缺失 stable key，不删除、失效或重算覆盖旧 flag。改变窗口或算法参数会保留历史版本；审计时应查看每条 flag 的 algorithm version 和 evidence。

唯一输出行 `comment-flag-refresh-v1`：event ID/slug、window、detected time、`matched_count`、`created_count`。matched 包含已存在稳定键，created 只统计本轮新行，因此重复运行时 created 通常为 0。

## 11. Propagation Node Scores

先运行 `refresh-comment-flags`，再执行：

```bash
uv run python main.py event propagation-nodes <EVENT> --since <SINCE> --until <UNTIL> --max-comments 50000 --output data/reports/nodes.jsonl
```

`max_comments` 范围 1–500000。只统计 active event videos 中在窗口首次出现、且有公开 author MID 的 comment entities。

角色分数：

| 角色 | 当前证据 |
| --- | --- |
| `originator` | 作为 template flag subject 的次数，相对窗口最大值归一化 |
| `amplifier` | 作为 template flag related 的次数，相对最大值归一化 |
| `bridge` | 涉及不同视频数，相对窗口最大值归一化 |
| `responder` | 楼中楼评论数，相对最大值归一化 |
| `official` | MID 是否被 active UID target 明确标为 role=official |

`overall_score` 是五项最大值，不是加权总分。

`propagation-node-score-v1` 保存公开 MID/name、各分数、overall、comment/RPID/raw/video、reply、template flag 和 official target 证据，以及 `event_scoped_candidate_scores_not_identity_labels` 限制。

输出 `algorithm` 固定为 `event-comment-evidence-v1`。

分数是同一窗口内部相对值；改变窗口、事件视频或 flags 后不可直接横向比较。

当前 analyzer 读取该 event 的全部 `template_like_comment` flags，不按 flag `detected_at` 过滤，再与本窗口 comment entities 交叉。因此历史 flag 只要涉及窗口内评论就可能贡献 originator/amplifier；使用前应核对 flag evidence 的原始窗口和算法版本。

## 12. Turning-Point Signals

```bash
uv run python main.py event turning-points <EVENT> --since <SINCE> --until <UNTIL> --bucket-minutes 60 --spike-multiplier 3 --min-count 5 --turnover-threshold 0.5 --top-n 20 --max-records 200000 --output data/reports/turning-points.jsonl
```

范围：bucket 1–1440 分钟且最多 10000 桶；spike multiplier `(1, 100]`；min count 1–1000000；turnover threshold `[0,1]`；Top N 1–20；max records 1–2000000。

四类 `turning-point-signal-v1`：

| signal type | 条件 |
| --- | --- |
| `comment_spike` | 相邻桶首见评论数达到 min count 且至少为前桶 multiplier 倍 |
| `keyword_spike` | active keyword 的去重 RPID 数满足同一突增规则 |
| `hot_turnover` | 热门第一页 Top N 相邻快照换血率达到阈值 |
| `major_creator_involvement` | 视频 owner MID 被 active UID target 明确标为 `major_creator`，且首次 info snapshot 落入窗口 |

每行保存 detected time、scope、magnitude 和可回查 evidence。`--bvid` / `--keyword` 在 event report 中可下推；独立 turning-points CLI 当前只暴露全事件窗口参数。

输出 `algorithm` 固定为 `adjacent-bucket-event-signals-v1`。

信号是 adjacent-bucket heuristic，不处理节假日、日周期、采集密度变化或外部因果变量。

## 13. Event Propagation Replay

```bash
uv run python main.py event replay-propagation <EVENT> --since <SINCE> --until <UNTIL> --max-records 100000 --output data/reports/propagation.jsonl
```

`max_records` 范围 1–1000000。该命令读取事件全部 video association，包括后来 inactive 的关联，并只输出有数据库证据的边：

| record type | source -> target |
| --- | --- |
| `video_associated` | event -> BVID |
| `comment_reply` | root comment -> reply comment |
| `template_propagation` | template flag subject -> related comment |

`event-propagation-replay-v1` 保存 event、type、occurred time、source/target node、evidence 和 `evidenced_edges_only_not_complete_causal_graph`。

template edge 依赖已经运行 `refresh-comment-flags`。该 replay 不根据相近发布时间、相同图片或共同关键词补造边，也不代表时间顺序已证明因果方向。

replay 同样读取 event 的全部 template flags，再以 related comment 的首见时间筛选输出窗口；source comment 可以早于窗口。旧算法版本不会被自动清理，必须通过 `comment_analysis_flag_id` 回查 evidence。

## 14. Event Report

```bash
uv run python main.py event report <EVENT> --since <SINCE> --until <UNTIL> --output data/reports/event.md --json-output data/reports/event.json
```

可用 `--bvid` 和 `--keyword` 筛选，值必须属于当前 active event 范围。筛选会进入 JSON `filters`，并下推到 coverage、timeline、turning points、observations、热门变化、关键词趋势和模板候选。

主要上限：

- `max_videos` 1–1000，默认 100。
- `max_records` 1–2000000，默认 5000；各章节超限均失败。
- 其他 bucket/spike/turnover/template/Top N 范围与独立 analyzer 相同。

### Markdown Sections

1. 事件概述。
2. 筛选条件。
3. 数据覆盖。
4. 关键时间线。
5. 核心视频节点。
6. 热门评论变化。
7. 关键词趋势。
8. 模板化评论候选簇。
9. 结论限制。
10. 证据索引。

report 不自动包含 stance summary、propagation node score 或 propagation replay；这些是独立产物。

### `event-report-v1`

顶层字段：

- `generated_at`, `window`, `filters`
- `event`
- `coverage`
- `key_timeline`
- `core_videos`
- `hot_comment_changes`
- `keyword_trends`
- `template_clusters`
- `limitations`
- `evidence_index`

coverage 包含 active video 数、已有 coverage 的视频数/比例、各状态数、页面成功率、items/raw/errors、truncated/corrupted 和首末完成时间，并明确 timestamp field 是 `finished_at`、until exclusive。

`template_clusters` 是本次 report 根据 template candidate 连通分量临时生成的文本候选簇，不是 `media_clusters` 表。

### Evidence Index

report 递归扫描各章节中的 ID，输出去重排序的：

```json
{"kind":"raw_payload","id":123}
{"kind":"raw_page_observation","id":456}
{"kind":"comment_observation","id":789}
{"kind":"comment_analysis_flag","id":12}
```

raw payload 可直接检查：

```bash
uv run python main.py raw inspect 123 --preview-bytes 1200
```

其他 ID 可通过 PostgreSQL 只读查询定位，并继续追踪 raw ID。

## 15. Output Schema Index

| Schema | 生成命令 | 是否写数据库 |
| --- | --- | --- |
| `hot-comment-turnover-v1` | `video hot-turnover` | 否 |
| `video-metric-replay-v1` | `video replay-metrics` | 否 |
| `hot-comment-replay-v1` | `video replay-hot-comments` | 否 |
| `comment-visibility-replay-v1` | `video replay-visibility` | 否 |
| `event-timeline-v1` | `event export-timeline` | 否 |
| `keyword-trend-v1` | `event keyword-trends` | 否 |
| `keyword-cooccurrence-v1` | `event keyword-cooccurrence` | 否 |
| `stance-evidence-v1` | `event stance-evidence` | 否 |
| `template-candidate-v1` | `event template-candidates` | 否 |
| `comment-flag-refresh-v1` | `event refresh-comment-flags` | 是，upsert flags |
| `propagation-node-score-v1` | `event propagation-nodes` | 否 |
| `turning-point-signal-v1` | `event turning-points` | 否 |
| `event-propagation-replay-v1` | `event replay-propagation` | 否 |
| `event-report-v1` | `event report --json-output` | 否 |

## 16. Reproducibility Checklist

发布或引用分析结果时，至少记录：

- 应用 commit 和 Alembic revision。
- event slug、active video/keyword 状态。
- 完整 CLI 命令和所有非默认参数。
- since/until 及 offset。
- stance lexicon version、template threshold 和算法版本。
- 同窗口 coverage 输出。
- 产物 SHA-256。
- report evidence index 和抽样 raw inspect 结果。
- 已知 partial/failed/truncated/corrupted 窗口。

不同时间重跑时，数据库可能已增加 observation、event active 范围可能变化。需要严格复现时，应使用同一数据库快照和文件证据集，而不是只保留命令行。
