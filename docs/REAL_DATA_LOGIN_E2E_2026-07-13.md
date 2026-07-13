# Authenticated Real Bilibili End-to-End Smoke 2026-07-13

本文记录 2026-07-13 在 Windows、PostgreSQL、Python 3.12 环境中执行的一次
真实 Bilibili 全流程验收。流程从二维码登录开始，覆盖账号加密存储、统一 Cookie
注入、视频指标、热门评论、latest frontier、楼中楼、图片归档、raw 证据、事件
分析、报告和有限轮次长期服务。

本文不记录 Cookie、CSRF、refresh token、二维码 URL 或登录快照 ID。真实平台
数据会变化，下面的数量只描述本次运行结果。

## 1. Test Window And Preconditions

采集起始时间：

```text
2026-07-13T06:59:38.185774+00:00
2026-07-13T14:59:38.185774+08:00
```

预检：

```bash
uv run python main.py service doctor
uv run python main.py service status --limit 20
uv run python main.py login status --account default
```

结果：数据库、Alembic revision、raw storage 和 media storage 均可用；初始队列
为空，账号状态为 `anonymous`。当时存在一条历史 worker heartbeat 告警，因为没有
常驻服务正在运行，不代表采集失败。

## 2. QR Login And Credential Rotation

执行：

```bash
uv run python main.py login qr --account default --timeout-seconds 240
uv run python main.py login status --account default
```

使用 Bilibili 手机客户端扫码并确认后：

- 登录命令返回成功。
- `login status` 显示 `health=valid`、`source=qr_login`。
- 新凭据写入 `data/accounts/credentials.enc`，主密钥位于
  `data/accounts/master.key`。
- `data/` 被 Git 忽略，账号文件和后续 smoke 产物均未进入版本控制。
- 后续真实请求由统一 client 自动读取当前凭据；日志和本文均未输出秘密字段。

长期服务阶段再次执行 Cookie 检查，结果为 `action=unchanged`，并更新
`last_checked_at`，证明 QR 快照可以被服务 scheduler 读取和验证。

## 3. Real Collection Targets

本次使用三个公开视频：

| BVID | 用途 |
| --- | --- |
| `BV1kZNN6iEPq` | 热门评论、楼中楼、多图媒体下载和既有 asset 复用 |
| `BV1gkDNBoEsi` | 已有 frontier 上的 latest 增量采集 |
| `BV1BPTj6FEYw` | 数据库中没有 coverage/frontier 的新样本，用于首次 tail + head baseline |

新 baseline 样本来自 MID `2882352` 的公开投稿列表。选择请求通过统一 Bilibili
client 执行；当时列表显示该视频约 15 条评论，数据库查询确认其 coverage 为空。

每个视频执行指标和热门第一页：

```bash
uv run python main.py monitor-video <BVID> --priority 100
uv run python main.py video comments <BVID> --mode hot --tier c --page-limit 1 --priority 80
```

latest 增量样本执行一次：

```bash
uv run python main.py collect-latest-comments BV1gkDNBoEsi --priority 70 --max-scan-seconds 55
```

新 baseline 样本执行两次，并在每次之后运行 worker：

```bash
uv run python main.py collect-latest-comments BV1BPTj6FEYw --priority 70 --max-scan-seconds 55
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle

uv run python main.py collect-latest-comments BV1BPTj6FEYw --priority 70 --max-scan-seconds 55
uv run python main.py worker loop --idle-sleep-seconds 0.2 --stop-when-idle
```

首次 latest coverage 为：

```text
status=succeeded reason=baseline_tail_complete pages=1/1 items=9
frontier_reached=False truncated=False corrupted=False
```

第二次为：

```text
status=succeeded reason=baseline_complete pages=1/1 items=9
frontier_reached=True truncated=False corrupted=False
```

因此本次不把 `baseline_tail_complete` 误认为正式 baseline，tail 和 head 两阶段均被
真实执行。

## 4. Collection Results

按采集起始时间汇总最终数据库结果：

| Task kind | Succeeded tasks | Pages | Items | Request errors | Parse errors | Truncated | Corrupted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `fetch_video_stats` | 6 | 6/6 | 6 | 0 | 0 | 0 | 0 |
| `fetch_hot_comments` | 3 | 3/3 | 38 | 0 | 0 | 0 | 0 |
| `fetch_latest_comments` | 3 | 3/3 | 26 | 0 | 0 | 0 | 0 |
| `fetch_comment_replies` | 9 | 9/9 | 68 | 0 | 0 | 0 | 0 |
| `fetch_media_asset` | 35 | 35/35 | 35 | 0 | 0 | 0 | 0 |

总计 56 个采集 task，全部 succeeded。结束时：

```text
pending=0
running=0
failed=0
active_backoffs=0
request_failure_rate=0.0
```

本轮写入的评论 observation 均保留公开 `author_mid`，并带有 raw payload 引用。
同一评论在 tail/head 或不同排序快照中再次出现会形成新 observation，因此
observation 数不等于去重评论数。

## 5. Raw And Media Verification

本轮 raw payload：

| Request type | Count | HTTP 200 |
| --- | ---: | ---: |
| `bilibili:video_stats` | 12 | 12 |
| `bilibili:comment_hot` | 3 | 3 |
| `bilibili:comment_latest` | 3 | 3 |
| `bilibili:comment_reply` | 9 | 9 |
| `bilibili:media_image` | 35 | 35 |

共 62 个 raw payload。使用 raw storage backend 重新读取并解压全部 62 个对象，逐一
验证 SHA-256 和 `uncompressed_size`，无不一致。代表性证据：

```bash
uv run python main.py raw inspect 90 --preview-bytes 160
uv run python main.py raw inspect 84 --preview-bytes 160
uv run python main.py raw inspect 91 --preview-bytes 160
uv run python main.py raw inspect 73 --preview-bytes 32
```

这四条分别覆盖新 baseline 视频信息、热门评论、完成 head sweep 的 latest JSON 和
媒体 JPEG 二进制。运行后的 raw ID 取决于数据库历史，不能作为其他环境的固定值。

媒体结果：

- 本轮生成 39 条 `comment_observation_media` 关联，39 条均回填 asset。
- 其中 35 个 source/asset 为本轮新增，4 个引用复用此前 asset。
- 35 个新文件共 31,301,707 bytes，包含 JPEG 和 PNG 及多种尺寸。
- 从 `storage_uri` 重新读取全部 35 个新文件并验证 blob SHA-256 和文件长度，无不一致。

这同时覆盖了单评论多图、本地文件系统保存、source -> asset 回填和完全一致文件
复用路径。

## 6. Event, Analysis And Report

事件：

```text
slug=smoke-login-e2e-20260713-1459
window=[2026-07-13T14:55:00+08:00, 2026-07-13T16:00:00+08:00)
videos=BV1kZNN6iEPq,BV1gkDNBoEsi,BV1BPTj6FEYw
keyword=鬼图
```

事件 coverage：

```text
videos=3/3
rows=12
statuses succeeded/partial/failed=12/0/0
pages=12/12
items=70
raw=18
request_errors=0
parse_errors=0
truncated=0
corrupted=0
```

产物位于 `data/smoke/2026-07-13-login-e2e/`：

| Artifact | Result |
| --- | ---: |
| `timeline.jsonl` | 122 records |
| `keyword-trends.jsonl` | 13 records |
| `propagation-nodes.jsonl` | 29 records |
| `turning-points.jsonl` | 1 record |
| `propagation-replay.jsonl` | 5 records |
| `event-report.md` | 37,554 bytes |
| `event-report.json` | 45,344 bytes |

报告 JSON schema 为 `event-report-v1`。`evidence_index` 共 48 项，其中：

- 39 个 `comment_observation` ID 全部在 PostgreSQL 中解析成功。
- 9 个 `raw_payload` ID 全部在 PostgreSQL 中解析成功。

## 7. Long-Running Service Smoke

为了不让验收扩大请求范围，测试使用位于 ignored data 目录的临时配置，保留同一
数据库、账号、存储和限流配置，只清空 discovery UID pools，并把 worker idle
sleep 调低。执行：

```bash
uv run python main.py \
  --config data/smoke/2026-07-13-login-e2e/service-smoke.yaml \
  service run --max-worker-iterations 12
```

结果：

- startup doctor 全部通过。
- worker+scheduler 实例写入 running heartbeat。
- 历史 `worker_heartbeat` 告警被自动标记 resolved。
- Cookie refresh job 返回 `action=unchanged`，没有轮换或失效。
- 服务达到有限 worker 轮次后协作停止，实例状态为 `stopped`、`error_type=None`。
- 最终没有后台进程、pending/running/failed task、backoff 或 active alert。

## 8. Explicit Limitations

- 本次没有执行 logout，因为它会删除刚验证成功的本地凭据；logout 已由自动化测试
  覆盖，不应作为每次线上 smoke 的收尾动作。
- 为控制真实请求预算，长期服务 smoke 刻意清空 discovery pools，因此没有在当前
  时钟等待并实测七个重点时点的 T+0/T+30 调度；该纯调度行为由策略和 handler
  自动化测试覆盖。
- 热门评论显式限制为 1 页，不声称评论全量覆盖。
- Bilibili 返回的页面 item 数、公开评论数和媒体数量会随时间变化，复验时只要求
  状态机、证据完整性和错误语义一致，不要求数量与本文相同。
- 本次没有人为制造 403、429、captcha 或网络中断；失败分类和退避由自动化测试
  覆盖。

## 9. Acceptance Conclusion

从二维码登录到加密凭据读取、真实采集、首次 latest baseline、图片本地归档、raw
证据复核、事件分析、报告 evidence index 和长期服务 Cookie 检查的链路全部通过。
本次没有发现需要修复的代码错误。
