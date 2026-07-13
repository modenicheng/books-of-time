# Discovery Schedule Policy Design

## Goal

Correct the scheduling boundary so that automatic new-video discovery runs only
from 10:00 inclusive to 22:00 exclusive in Asia/Shanghai, while all work for
already discovered videos remains eligible to run around the clock.

## Policy

- Automatic UID discovery is active during `10:00 <= local time < 22:00`.
- The focus minutes are `11:00`, `12:00`, `13:00`, `18:00`, `19:00`, `19:30`,
  and `20:00`.
- A discovery slot inside a focus minute uses a higher collection-task priority
  and records the focus label in its payload for later audit.
- The persisted scheduler keeps the default 60-second discovery cadence. A run
  delayed within the scheduler uses its persisted scheduled slot for window and
  focus classification, so ordinary execution drift does not lose the label.
- Video metric snapshots have no discovery-window gate. Their age/growth cadence
  remains unchanged and applies 24 hours a day.
- The 22:00 terminal snapshot remains an additional idempotent daily checkpoint;
  it does not end video metric collection for that day.
- Already queued hot/latest comment, reply, media, retry, and normalization work
  remains eligible for worker execution at all hours. This change does not invent
  a new recurring comment cadence.
- Explicit diagnostic CLI discovery remains an operator-triggered command and is
  not silently blocked by the automatic-service window.

## Configuration

The scheduler configuration exposes:

```yaml
scheduler:
  discovery_scan_seconds: 60
  discovery_start_hour: 10
  discovery_stop_hour: 22
  discovery_timezone: Asia/Shanghai
  discovery_focus_times: ["11:00", "12:00", "13:00", "18:00", "19:00", "19:30", "20:00"]
```

The start is inclusive and the stop is exclusive. Focus values use strict
24-hour `HH:MM` syntax and must fall inside the active window.

## Verification

- Pure policy tests cover the opening and closing boundaries, timezone
  conversion, all configured focus minutes, and non-focus minutes.
- Scheduled-handler tests cover an inactive slot, a normal slot, and a focus
  slot with priority and payload evidence.
- Snapshot-policy and database scheduler tests prove that metric collection
  continues after 22:00 Asia/Shanghai.
- Full pytest, Ruff, Alembic metadata, and Compose configuration checks remain
  required before commit.
