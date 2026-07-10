"""YAML 配置加载器。

用法:
    from books_of_time.config import load_config

    cfg = load_config()
    db_url = cfg["database"]["url"]
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 默认配置路径
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """加载 YAML 配置文件。

    Parameters
    ----------
    path:
        配置文件路径。默认读取 ``config/config.yaml``。

    Returns
    -------
    dict[str, Any]
        解析后的配置字典。
    """
    effective_environ = os.environ if environ is None else environ
    environment_path = effective_environ.get("BOT_CONFIG")
    config_path = (
        Path(path)
        if path is not None
        else Path(environment_path)
        if environment_path
        else _DEFAULT_CONFIG_PATH
    )

    if not config_path.exists():
        example_path = config_path.with_suffix(".yaml.example")
        msg = (
            f"配置文件 {config_path} 不存在。\n"
            f"请从模板复制: cp {example_path} {config_path}"
        )
        raise FileNotFoundError(msg)

    with open(config_path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"配置文件根节点必须是映射: {config_path}")

    cfg: dict[str, Any] = dict(loaded)
    database = _mapping_section(cfg, "database")
    storage = _mapping_section(cfg, "storage")
    service = _mapping_section(cfg, "service")
    accounts = _mapping_section(cfg, "accounts")

    if value := effective_environ.get("BOT_DATABASE_URL"):
        database["url"] = value
    if value := effective_environ.get("BOT_RAW_DIR"):
        storage["raw_dir"] = value
    if value := effective_environ.get("BOT_MEDIA_DIR"):
        storage["media_dir"] = value
    if value := effective_environ.get("BOT_INSTANCE_ID"):
        service["instance_id"] = value
    if value := effective_environ.get("BOT_SERVICE_ROLES"):
        service["roles"] = [role.strip() for role in value.split(",") if role.strip()]
    if value := effective_environ.get("BOT_SHUTDOWN_GRACE_SECONDS"):
        service["shutdown_grace_seconds"] = float(value)
    if value := effective_environ.get("BOT_ACCOUNT_ID"):
        accounts["active_account_id"] = value
    if value := effective_environ.get("BOT_ACCOUNT_CREDENTIALS_PATH"):
        accounts["credentials_path"] = value
    if value := effective_environ.get("BOT_ACCOUNT_KEY_PATH"):
        accounts["key_path"] = value
    if value := effective_environ.get("BOT_ACCOUNT_REFRESH_SECONDS"):
        accounts["refresh_check_seconds"] = int(value)
    if "BOT_ACCOUNT_ENABLED" in effective_environ:
        accounts["enabled"] = _parse_bool_override(
            "BOT_ACCOUNT_ENABLED",
            effective_environ["BOT_ACCOUNT_ENABLED"],
        )
    if "BOT_ACCOUNT_AUTO_REFRESH" in effective_environ:
        accounts["auto_refresh"] = _parse_bool_override(
            "BOT_ACCOUNT_AUTO_REFRESH",
            effective_environ["BOT_ACCOUNT_AUTO_REFRESH"],
        )

    return cfg


def _mapping_section(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    value = cfg.setdefault(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"配置段 {key} 必须是映射")
    return value


def _parse_bool_override(name: str, value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of true/false, 1/0, yes/no, or on/off")
