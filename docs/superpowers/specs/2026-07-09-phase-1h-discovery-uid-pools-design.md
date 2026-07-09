# Phase 1H Discovery UID Pools Design

## Context

Phase 1G added `bot discovery loop` for `discovery.matrix_uids`. The next P0
TODO item asks for event-level and game-level UID pools. Phase 2 will add full
event archive models, so this slice should not create event tables yet.

## Goal

Let discovery scan configured global, game-level, and event-level UID pools in
one loop while preserving source metadata for later event/archive work.

## Configuration

Keep the existing global list:

```yaml
discovery:
  matrix_uids: []
```

Add two optional pool maps:

```yaml
discovery:
  game_uid_pools:
    genshin: ["100", "200"]
    hsr:
      uids: ["300"]
  event_uid_pools:
    version_42:
      uids: ["400"]
```

Both `pool: [uids]` and `pool: {uids: [...]}` are accepted. UID values may be
strings or numbers and are normalized to strings.

## Runtime Model

Add a small `DiscoveryUidSource` dataclass:

```python
@dataclass(frozen=True)
class DiscoveryUidSource:
    mid: str
    pool_type: str
    pool_id: str | None = None
```

Pool types are:

- `matrix`
- `game`
- `event`

`DiscoveryLoop` accepts `uid_sources` and keeps `matrix_uids` as a backward
compatible constructor argument. It scans sources in stable config order.

## Source Metadata

`DiscoveredVideo` carries optional `source_pool_type` and `source_pool_id`.
`DiscoveryScheduler.handle_discovered_videos()` writes these into the
`fetch_video_stats` task payload:

```json
{
  "source_mid": "100",
  "source_pool_type": "game",
  "source_pool_id": "genshin"
}
```

No event/game relationship tables are added in this slice.

## Acceptance Criteria

- `DiscoveryLoop` can scan `DiscoveryUidSource` entries.
- CLI config resolution includes `matrix_uids`, `game_uid_pools`, and
  `event_uid_pools`.
- Task payloads preserve source pool metadata.
- `config.yaml.example` documents the new shape.
- TODO item `支持事件级 UID 池和游戏级 UID 池。` is marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Event archive tables.
- Video-event association tables.
- Late-discovery compensation.
- 22:00 terminal snapshot tasks.
- Multi-page UID scans.
