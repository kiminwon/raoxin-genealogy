"""账号、认领、邀请码、可编辑范围。

注意区分两件事：
  - person 表 = "家谱里关于这个人的记载"（不含任何联系方式）
  - app_user 表 = "这个账号背后是谁"（可以含手机号，但绝不进家谱数据）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import BIGINT, CHAR, TINYINT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin


class AppUser(Base, TimestampMixin):
    """微信用户账号。"""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    openid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    unionid: Mapped[str | None] = mapped_column(String(64))
    nickname: Mapped[str | None] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    real_name: Mapped[str | None] = mapped_column(String(32), comment="用户自填真实姓名，不入家谱")

    # 账号级核验信息：不参与任何家谱查询、不进树、不对族人展示。
    # 若决定不使用微信手机号核验，删掉该字段即可，认领审核改为纯人工线下核实。
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary(255), comment="手机号密文")

    role: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    family_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    claimed_person_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_family_role", "family_id", "role"),
        Index("idx_claimed", "claimed_person_id"),
    )


class UserFamily(Base):
    """用户与宗族的成员关系。一人可同时属于多个族（父系谱、母系谱…）。

    为什么要有这张表：
        V1 只用 app_user.family_id 一列表示"我属于哪个族"，一人只能有一个。
        可现实是父系谱和母系谱都要查——第二个族一出现，一列就装不下了。

    为什么 app_user 的 family_id / role / claimed_person_id 还留着：
        它们是**当前正在看哪个族**的快照。全站鉴权（build_viewer、写权限、
        房长门禁）读的都是这三列的当前值。切族时由 family_member.activate()
        把这三列回填成目标族的值，于是鉴权代码一行都不用改。
        代价是"当前族"必须由后端原子切换，不能让端上自己算。

    claimed_person_id 也按族存：父系认领"陈雨欣"、母系认领"王某某"互不影响。
    """

    __tablename__ = "user_family"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    # 本族内角色，与这个人在别的族是什么角色无关
    role: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    claimed_person_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    status: Mapped[int] = mapped_column(
        TINYINT, nullable=False, default=1, comment="1正常 2已退出"
    )
    # 不继承 TimestampMixin：这张表没有 updated_at 列，多映射一列会让查询报 Unknown column。
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("user_id", "family_id", name="uk_user_family"),
        Index("idx_uf_family_status", "family_id", "status"),
    )


class Claim(Base):
    """认领申请。

    主路径是**答题自证**（verify_method=1）：答出父母/配偶姓名，系统在树里反查，
    唯一命中即通过。原因是房长年纪大、不会用审批界面，人工审核不能挂在主路径上。
    REVIEW（2）保留为兜底：答题失败、同名歧义、申诉。
    """

    __tablename__ = "claim"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    person_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    claim_type: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    evidence: Mapped[str | None] = mapped_column(String(512))
    evidence_files: Mapped[Any | None] = mapped_column(JSON)
    # 怎么证明的 + 答案是原话快照（含命中节点、证据强度、时间）。
    # 不塞进 evidence：那是给房长读的一句话，语义不同，混用会让两者都不可查。
    verify_method: Mapped[int] = mapped_column(
        TINYINT, nullable=False, default=1, server_default="1"
    )
    verify_answers: Mapped[Any | None] = mapped_column(JSON)
    status: Mapped[int] = mapped_column(
        TINYINT, nullable=False, default=1, comment="1待审 2通过 3驳回 4撤回 5冲突"
    )
    reviewer_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    review_note: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 申请提交时间。审核列表要按它排队（admin.py:163），auth.py 也要按它取最近一次认领。
    # 不继承 TimestampMixin：claim 表没有 updated_at 列，多映射一列会让所有查询报 Unknown column。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_person_status", "person_id", "status"),
        Index("idx_user", "user_id"),
        Index("idx_family_status", "family_id", "status"),
    )


class InviteCode(Base):
    """邀请码。决策第 5 条 方案 C：一张表存两种码。

    scope_rule 与 scope_person_ids 两份都存：
      - scope_rule        规则（可读、可重算）
      - scope_person_ids  签发那一刻的快照，鉴权以它为准
    理由：权限只能通过显式动作产生，不能因为别人改了树而悄悄变大。
    """

    __tablename__ = "invite_code"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    code: Mapped[str] = mapped_column(CHAR(8), nullable=False, unique=True)
    code_type: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    # 编辑码专属
    anchor_person_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    anchor_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    scope_rule: Mapped[Any | None] = mapped_column(JSON)
    scope_person_ids: Mapped[Any | None] = mapped_column(JSON, comment="签发时快照")
    scope_node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    inviter_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    invite_note: Mapped[str | None] = mapped_column(String(128))
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="0=不限")
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    # 「我的邀请码」按签发时间倒序（invite.py:63）。同样不继承 TimestampMixin。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_family_type", "family_id", "code_type"),
        Index("idx_anchor", "anchor_person_id"),
        Index("idx_inviter", "inviter_user_id"),
        Index("idx_expires", "expires_at"),
    )


class EditScope(Base):
    """用户可编辑范围。鉴权主查询表。

    刻意用实体表而不是每次动态计算：
    动态算的话，树被误改会导致权限自动扩大；实体表权限只能显式产生。
    代价是新增节点需重新授权（一键申请即可），但权限宁可偏紧不可偏松。
    """

    __tablename__ = "edit_scope"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    person_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    source: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    invite_code_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    granted_by: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    granted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("user_id", "person_id", name="uk_user_person"),
        Index("idx_person", "person_id"),
        Index("idx_user_family", "user_id", "family_id"),
    )
