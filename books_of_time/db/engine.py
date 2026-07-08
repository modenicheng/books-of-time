"""异步数据库引擎与会话工厂。

生命周期
--------
应用启动时调用 :func:`init_db` 创建引擎；
关闭时调用 :func:`shutdown_db` 释放连接池。

用法 (FastAPI 风格):
    from books_of_time.db.engine import get_async_session

    async def create_user(...):
        async with get_async_session() as session:
            ...
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from books_of_time.config import load_config

# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def _make_engine_from_config(cfg: dict[str, Any]):
    """根据配置字典创建异步引擎 (内部)。"""
    db_cfg = cfg.get("database", {})
    return create_async_engine(
        db_cfg["url"],
        pool_size=db_cfg.get("pool_size", 5),
        max_overflow=db_cfg.get("max_overflow", 10),
        pool_pre_ping=db_cfg.get("pool_pre_ping", True),
        echo=db_cfg.get("echo", False),
    )


def init_db(config_path: str | None = None) -> None:
    """初始化数据库引擎与会话工厂。

    可重复调用（幂等）——后续调用不会重建已存在的引擎。
    """
    global _engine, _session_factory

    if _engine is not None:
        return  # 已经初始化

    cfg = load_config(config_path)
    _engine = _make_engine_from_config(cfg)
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def shutdown_db() -> None:
    """关闭数据库引擎并释放连接池。"""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_async_session() -> AsyncSession:
    """获取一个异步会话（必须用作上下文管理器或 ``async with``）。

    用法:
        async with get_async_session() as session:
            ...
    """
    if _session_factory is None:
        # 未显式调用 init_db 时使用默认配置自动初始化
        init_db()
    return _session_factory()  # type: ignore[misc]
