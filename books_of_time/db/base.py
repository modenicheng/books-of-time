"""SQLAlchemy 声明式基类与通用 Mixin。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """项目全局 SQLAlchemy 声明式基类。

    所有 ORM 模型都应继承此类。
    """

    pass


class TimestampMixin:
    """自动记录创建／更新时间的 Mixin。

    用法:
        class User(TimestampMixin, Base):
            __tablename__ = "users"
            ...
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="记录创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="记录最后更新时间",
    )
