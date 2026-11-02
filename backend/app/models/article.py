"""谱文：编修历史、迁徙源流、谱序、凡例等长文章。

为什么独立成表而不是塞进 family 表：一篇迁徙源流几千字，一篇谱序一修一篇，
同分类要能存多篇、能排序、能软删留痕——一行字段装不下，一对多就该是一张表。

权限（启明 2026-10-21 拍板）：
    读 = 本族成员（user_family 里有行）。
    写 = 房长及以上（**按族查 user_family.role**，不是 app_user.role——
         那是"当前族"的快照，文章接口的 family_id 在路径里，两者可能不同族）。
    写直接生效、不走修订审核：文章不像世系字段那样容易引发家族纠纷，
    卡一道审核只会让房长懒得写。留痕靠 created_by / updated_by + 软删。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.mysql import BIGINT, MEDIUMTEXT, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FamilyArticle(Base):
    __tablename__ = "family_article"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    category: Mapped[int] = mapped_column(TINYINT, nullable=False, default=6)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    updated_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("idx_family_cat", "family_id", "category", "sort_order"),)
