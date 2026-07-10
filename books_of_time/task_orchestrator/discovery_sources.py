from __future__ import annotations

from books_of_time.task_orchestrator.discovery_loop import DiscoveryUidSource


def resolve_discovery_uid_sources(
    discovery_cfg: dict,
) -> list[DiscoveryUidSource]:
    sources: list[DiscoveryUidSource] = [
        DiscoveryUidSource(mid=str(uid)) for uid in discovery_cfg.get("matrix_uids", [])
    ]

    for pool_id, pool_value in discovery_cfg.get("game_uid_pools", {}).items():
        sources.extend(
            DiscoveryUidSource(mid=str(uid), pool_type="game", pool_id=str(pool_id))
            for uid in _uids_from_pool_value(pool_value)
        )

    for pool_id, pool_value in discovery_cfg.get("event_uid_pools", {}).items():
        sources.extend(
            DiscoveryUidSource(
                mid=str(uid),
                pool_type="event",
                pool_id=str(pool_id),
            )
            for uid in _uids_from_pool_value(pool_value)
        )

    return sources


def _uids_from_pool_value(pool_value: object) -> list[object]:
    if isinstance(pool_value, dict):
        uids = pool_value.get("uids", [])
    else:
        uids = pool_value
    if uids is None:
        return []
    if isinstance(uids, str | int):
        return [uids]
    return list(uids)
