"""Pydantic v2 基础模型配置。

本项目使用 Pydantic v2 做数据验证与序列化。
所有 Pydantic schema / DTO 都应继承 ``AppBaseModel``。

与 SQLAlchemy ORM 的互操作:
    - 设置 ``from_attributes = True`` 支持 ``model_validate(obj)``
      从 ORM 实例直接构造 Pydantic 模型。
    - 需要自定义序列化时使用 ``field_serializer`` 装饰器。

用法:
    class UserRead(AppBaseModel):
        id: int
        name: str
        created_at: datetime
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AppBaseModel(BaseModel):
    """项目全局 Pydantic 基础模型。"""

    model_config = ConfigDict(
        # 允许从 ORM 属性读取
        from_attributes=True,
        # 禁止额外字段（严格校验）
        extra="forbid",
        # 使用 Python 原生类型（如 datetime）而非序列化后的 str
        use_enum_values=True,
    )
