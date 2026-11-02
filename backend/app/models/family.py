"""宗族与支系。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.mysql import BIGINT, CHAR, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Family(Base, TimestampMixin):
    """宗族。一棵家族树 = 一条记录。"""

    __tablename__ = "family"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)

    # ---- 标识 ----
    surname: Mapped[str] = mapped_column(String(16), nullable=False, comment="主姓氏")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="宗族名")
    tang_hao: Mapped[str | None] = mapped_column(String(32), comment="堂号")
    jun_wang: Mapped[str | None] = mapped_column(String(32), comment="郡望")
    origin_place: Mapped[str | None] = mapped_column(String(128), comment="始迁地")
    settle_place: Mapped[str | None] = mapped_column(String(128), comment="落籍地")
    founder_name: Mapped[str | None] = mapped_column(String(64), comment="始迁祖名讳")
    founder_era: Mapped[str | None] = mapped_column(String(64), comment="始迁年代")

    # ---- 字辈诗（决策第 1 条：只做提示参考，不做强制校验）----
    # generation_poem 第 i 个汉字对应第 (first_gen_label + i) 世
    generation_poem: Mapped[str | None] = mapped_column(String(512), comment="字辈诗")
    generation_poem_note: Mapped[str | None] = mapped_column(String(512), comment="字辈说明")
    first_gen_label: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    gen_poem_enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    # ---- 树配置 ----
    show_external_in_overview: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)

    # ---- 全族浏览码（方案 C 的 type=1）----
    view_code: Mapped[str | None] = mapped_column(CHAR(8), unique=True)
    view_code_enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    # ---- 数据版本号：树布局缓存失效的依据 ----
    data_rev: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)

    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    __table_args__ = (Index("idx_surname", "surname"),)


class Branch(Base):
    """支系（房）。"""

    __tablename__ = "branch"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="房名")
    founder_person_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    __table_args__ = (Index("idx_family", "family_id", "sort_no"),)
