"""ORM 基类与通用 Mixin。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, SmallInteger, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有模型的基类。"""


class TimestampMixin:
    """创建/更新时间。数据库层面由 DEFAULT CURRENT_TIMESTAMP 兜底。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SoftDeleteMixin:
    """软删除。设计约定：一律 status=0，不做物理 DELETE。"""

    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )


class IdMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
