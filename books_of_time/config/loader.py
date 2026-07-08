"""YAML 配置加载器。

用法:
    from books_of_time.config import load_config

    cfg = load_config()
    db_url = cfg["database"]["url"]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 默认配置路径
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
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
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    if not config_path.exists():
        example_path = config_path.with_suffix(".yaml.example")
        msg = (
            f"配置文件 {config_path} 不存在。\n"
            f"请从模板复制: cp {example_path} {config_path}"
        )
        raise FileNotFoundError(msg)

    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)
