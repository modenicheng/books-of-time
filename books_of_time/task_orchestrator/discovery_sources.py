from __future__ import annotations

from books_of_time.task_orchestrator.discovery_loop import DiscoveryUidSource


def resolve_discovery_uid_sources(
    discovery_cfg: dict,
) -> list[DiscoveryUidSource]:
    sources: list[DiscoveryUidSource] = [
        DiscoveryUidSource(mid=str(uid)) for uid in discovery_cfg.get("matrix_uids", [])
    ]

    game_pools = _pool_mapping(discovery_cfg, "game_uid_pools")
    for pool_id, pool_value in game_pools.items():
        options = _pool_options(
            pool_type="game",
            pool_id=str(pool_id),
            pool_value=pool_value,
        )
        sources.extend(
            DiscoveryUidSource(mid=str(uid), **options)
            for uid in _uids_from_pool_value(pool_value)
        )

    event_pools = _pool_mapping(discovery_cfg, "event_uid_pools")
    for pool_id, pool_value in event_pools.items():
        options = _pool_options(
            pool_type="event",
            pool_id=str(pool_id),
            pool_value=pool_value,
        )
        sources.extend(
            DiscoveryUidSource(mid=str(uid), **options)
            for uid in _uids_from_pool_value(pool_value)
        )

    return sources


def _pool_mapping(discovery_cfg: dict, key: str) -> dict:
    value = discovery_cfg.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"discovery.{key} must be a mapping")
    return value


def _pool_options(
    *,
    pool_type: str,
    pool_id: str,
    pool_value: object,
) -> dict:
    metadata = pool_value if isinstance(pool_value, dict) else {}
    official = _strict_pool_bool(
        metadata,
        "official",
        pool_type == "game",
        pool_id,
    )
    monitored = _strict_pool_bool(metadata, "monitored", True, pool_id)
    game_id = metadata.get("game_id", pool_id if pool_type == "game" else None)
    return {
        "pool_type": pool_type,
        "pool_id": pool_id,
        "game_id": game_id,
        "official": official,
        "monitored": monitored,
    }


def _strict_pool_bool(
    metadata: dict,
    key: str,
    default: bool,
    pool_id: str,
) -> bool:
    value = metadata.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"Discovery pool {pool_id!r} field {key} must be a boolean")
    return value


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
