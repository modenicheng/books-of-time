# Event Archive Management

事件 slug 是报告、任务和脚本使用的稳定引用。创建后可修改名称、状态和时间窗，但不能修改 slug。

事件如何进入正式 discovery 见 [COLLECTION](COLLECTION.md#4-uid-discovery)，全部分析命令和输出见 [ANALYSIS](ANALYSIS.md)，参数速查见 [CLI_REFERENCE](CLI_REFERENCE.md#event-lifecycle)。

## Create And Update

```bash
uv run python main.py event create ghost-picture-war \
  --name "鬼图战争" --game "Example Game" \
  --start-at 2026-07-10T00:00:00+08:00

uv run python main.py event update ghost-picture-war \
  --status closed --end-at 2026-07-18T00:00:00+08:00
```

可更新 `--name`、`--game`、`--description`、`--status`、`--start-at`、`--end-at` 和 `--timezone`。使用 `--clear-game`、`--clear-description`、`--clear-start-at` 或 `--clear-end-at` 清空可选字段。时间必须包含 UTC offset，更新后的起止时间会重新校验。

事件状态为 `active` 且处于配置时间窗内时，UID target 才参与 discovery。`closed` 或 `archived` 不会删除已有视频、评论、图片、coverage 或 raw 证据。

## Targets

```bash
uv run python main.py event add-target ghost-picture-war uid 12345 --priority 100
uv run python main.py event add-target ghost-picture-war uid 23456 --priority 100 --role official
uv run python main.py event add-target ghost-picture-war uid 34567 --priority 90 --role major_creator
uv run python main.py event add-target ghost-picture-war keyword "鬼图战争"
uv run python main.py event add-target ghost-picture-war seed_bvid BV1xx411c7mD
uv run python main.py event add-target ghost-picture-war game "Example Game"

uv run python main.py event list-targets ghost-picture-war
uv run python main.py event list-targets ghost-picture-war --all
uv run python main.py event set-target-status ghost-picture-war 42 inactive
uv run python main.py event set-target-status ghost-picture-war 42 active
```

列表默认只显示 active target，`--all` 包含停用历史。停用 keyword target 会同步停用对应版本化关键词；停用 UID target 后 scheduler 不再为它创建 discovery 任务；停用 seed BVID target 会停用由该 target 建立的视频关联。所有操作保留原数据库行。

`official` role 参与传播节点 official 证据，`major_creator` role 参与转折信号；role 只允许用于 UID。`game` target 保存分类范围，当前不会自动触发平台游戏搜索。

## Event Videos

```bash
uv run python main.py event list-videos ghost-picture-war
uv run python main.py event list-videos ghost-picture-war --all
uv run python main.py event set-video-status ghost-picture-war BV1xx411c7mD inactive
uv run python main.py event set-video-status ghost-picture-war BV1xx411c7mD active
```

视频关联停用后不会进入 active-video 分析范围；报告仍可列出这条历史关联并标记 `active=false`。其采集任务、observation、状态事件、媒体和 raw payload 不会删除。

## Windowed Coverage

```bash
uv run python main.py event coverage ghost-picture-war \
  --since 2026-07-10T00:00:00+08:00 \
  --until 2026-07-18T00:00:00+08:00
```

不传时间参数时查询事件当前全部 coverage；传入时必须同时提供 `--since` 和 `--until`。有界汇总只包含 `finished_at` 位于 `[since, until)` 的采集记录，事件报告始终使用其报告时间窗执行同口径汇总。

## Filtered Reports

```bash
uv run python main.py event report ghost-picture-war \
  --since 2026-07-10T00:00:00+08:00 \
  --until 2026-07-18T00:00:00+08:00 \
  --bvid BV1xx411c7mD --keyword "鬼图战争" \
  --output reports/ghost-picture-war.md \
  --json-output reports/ghost-picture-war.json
```

`--bvid` 和 `--keyword` 可单独使用，也可组合。筛选值必须属于事件当前 active 视频和关键词；筛选会下推到 coverage、timeline、转折信号、热门变化、关键词趋势和模板候选查询。Markdown 的“筛选条件”和 JSON 的 `filters` 字段会记录最终规范化值。
