"""人物、婚姻单元、亲子关系——本项目最核心的三张表。

【硬约束】严禁在 Person 上增加 father_id / mother_id 字段。
关系一律走 FamilyUnion + ParentChild 两层，否则再婚、过继、入赘全部无法表达。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import BIGINT, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Person(Base, TimestampMixin):
    """人物。

    注意 name 允许为 NULL —— 老谱"配王氏"场景，只知道姓。
    这类记录会自动进入待补全清单，由后代补全姓名。
    """

    __tablename__ = "person"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)

    # ---- 谱系定位 ----
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    branch_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    generation: Mapped[int | None] = mapped_column(Integer, comment="世次，始迁祖=1")
    generation_hint: Mapped[int] = mapped_column(
        TINYINT, nullable=False, default=0, comment="0未校验 1匹配 2不匹配(仅提示) 3用户已忽略"
    )
    generation_name: Mapped[str | None] = mapped_column(String(16), comment="谱名中的字辈用字")

    # ---- 谱系角色 ----
    lineage_role: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    is_lineage_carrier: Mapped[int] = mapped_column(
        TINYINT, nullable=False, default=0, comment="全族树纵向主链是否从此人往下走"
    )

    # ---- 姓名 ----
    name: Mapped[str | None] = mapped_column(String(64), comment="姓名全称，可空")
    surname: Mapped[str | None] = mapped_column(String(16), comment="姓氏")
    name_pinyin: Mapped[str | None] = mapped_column(String(64), comment="拼音，检索用")
    gender: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    courtesy_name: Mapped[str | None] = mapped_column(String(64), comment="字")
    art_name: Mapped[str | None] = mapped_column(String(64), comment="号")
    rank_in_family: Mapped[str | None] = mapped_column(String(32), comment="排行")
    name_remark: Mapped[str | None] = mapped_column(String(128), comment="名讳说明")

    # ---- 生卒葬 ----
    birth_date: Mapped[date | None] = mapped_column(Date)
    birth_date_precision: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    death_date: Mapped[date | None] = mapped_column(Date)
    death_date_precision: Mapped[int] = mapped_column(TINYINT, nullable=False, default=3)
    # 默认**在世**。默认为「已故」曾导致新添的人一建出来就挂个「故」字
    # （2026-10-29 实报：给陈国栋添陈老三，只填名字，卡片显示已故）。
    # 谱源导入（source_type=1）要按史料判断，必须显式写 2，不要靠默认值。
    living_status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    marital_status: Mapped[int | None] = mapped_column(
        TINYINT,
        comment="婚姻状态 1未婚 2已婚 3丧偶 4离异；NULL=未确认（会继续问配偶）",
    )
    childless: Mapped[int] = mapped_column(
        TINYINT,
        nullable=False,
        default=0,
        comment="1=本人已确认没有子女（不再问这一项）",
    )
    external_opt_out: Mapped[int] = mapped_column(
        TINYINT,
        nullable=False,
        default=0,
        comment="1=已明确「外孙不入谱」（不再追问这位的女儿所出子女）",
    )
    burial_place: Mapped[str | None] = mapped_column(String(255))
    burial_note: Mapped[str | None] = mapped_column(String(512))

    # ---- 社会信息（在世者受脱敏约束）----
    education: Mapped[str | None] = mapped_column(String(64))
    occupation: Mapped[str | None] = mapped_column(String(64))
    biography: Mapped[str | None] = mapped_column(Text)
    native_place: Mapped[str | None] = mapped_column(
        String(128), comment="籍贯，仅到乡镇级；严禁填写详细住址"
    )

    # ---- 史料来源 ----
    source_type: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    source_note: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[int] = mapped_column(TINYINT, nullable=False, default=2)
    verify_status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    # ---- 隐私与生命周期 ----
    privacy_level: Mapped[int] = mapped_column(TINYINT, nullable=False, default=2)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    merged_into_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))

    # ---- 预留：决策第 3 条 ----
    # 将来做母系时升级为中间表 person_family_membership，不必改主表
    alt_family_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), comment="【预留】跨族关联"
    )

    created_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))

    __table_args__ = (
        Index("idx_family_gen", "family_id", "generation"),
        Index("idx_family_role", "family_id", "lineage_role"),
        Index("idx_branch", "branch_id"),
        Index("idx_family_name", "family_id", "name"),
        Index("idx_surname_name", "surname", "name"),
        Index("idx_status", "status"),
        Index("idx_merged", "merged_into_id"),
        Index("idx_alt_family", "alt_family_id"),
        Index("idx_living", "family_id", "living_status"),
    )


class FamilyUnion(Base, TimestampMixin):
    """婚姻单元（设计概念叫 Union，物理表名避开 SQL 保留字）。

    约定：partner_a_id 永远是"承袭世系的一方"（通常父方；招赘时为女儿本人）。
    partner_b_id 允许为 NULL —— 老谱"配某氏"、未婚生育都需要这个能力。
    """

    __tablename__ = "family_union"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    partner_a_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    partner_b_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    union_type: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    union_seq: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    start_date: Mapped[date | None] = mapped_column(Date)
    start_date_precision: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    end_date: Mapped[date | None] = mapped_column(Date)
    end_reason: Mapped[int | None] = mapped_column(TINYINT)
    remark: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    __table_args__ = (
        Index("idx_family", "family_id"),
        Index("idx_partner_a", "partner_a_id"),
        Index("idx_partner_b", "partner_b_id"),
        Index("idx_status", "status"),
    )


class ParentChild(Base):
    """亲子关系。

    过继场景：同一个 child 挂两条记录——
        生父的 union -> relation=1(亲生), is_primary=0, is_biological=1
        嗣父的 union -> relation=2(过继), is_primary=1, is_biological=0
    查父子关系时永远优先取 is_primary=1。
    """

    __tablename__ = "parent_child"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    union_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    child_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    relation: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    is_primary: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    is_biological: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    remark: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    # 这条亲子关系何时录入。审计线索：过继/认领类纠纷要能追溯到录入时间。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("union_id", "child_id", "relation", name="uk_union_child_rel"),
        Index("idx_child", "child_id", "is_primary"),
        Index("idx_family", "family_id"),
        Index("idx_union", "union_id"),
    )
