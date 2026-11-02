"""修订队列、布局缓存、审计、通知。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, MEDIUMTEXT, TINYINT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base


class Revision(Base):
    """修订记录。

    V1 采取"先审后发"：所有写操作先落这张表，管理员通过后才写进 person 等业务表。
    理由：家谱错误会被后代当作祖上记载沿用几十年，试错成本极高，宁可慢。
    """

    __tablename__ = "revision"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    target_table: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), comment="NULL=新增")
    op: Mapped[int] = mapped_column(TINYINT, nullable=False, default=2)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False, comment="变更后的字段值")
    before_snapshot: Mapped[Any | None] = mapped_column(JSON, comment="变更前快照，用于回滚")
    diff_summary: Mapped[str | None] = mapped_column(String(512))
    submitter_user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    submitter_person_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    submit_note: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[int] = mapped_column(
        TINYINT, nullable=False, default=1, comment="1待审 2通过 3驳回 4自动合并 5撤回"
    )
    conflict_with_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    reviewer_user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    review_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_target", "target_table", "target_id"),
        Index("idx_family_status", "family_id", "status", "created_at"),
        Index("idx_submitter", "submitter_user_id", "status"),
        Index("idx_conflict", "conflict_with_id"),
    )


class TreeLayoutCache(Base):
    """家族树布局缓存。

    是否有效由 family_rev 与 family.data_rev 比对决定。
    布局计算是 O(n) 的，单次不慢，但"我的一家"是主屏，每次进小程序都触发，
    几百毫秒的计算 + 几百 KB 的传输会让首屏明显迟滞。
    """

    __tablename__ = "tree_layout_cache"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    view_type: Mapped[int] = mapped_column(TINYINT, nullable=False, comment="1我的一家 2我的支系 3全族总览")
    anchor_person_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    view_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="如 1:10086 / 3:none")
    layout_json: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    family_rev: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    calc_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("family_id", "view_key", name="uk_view_key"),
        Index("idx_family_rev", "family_id", "family_rev"),
        Index("idx_anchor", "anchor_person_id"),
    )


class AuditLog(Base):
    """审计日志。敏感动作必须留痕：认领审批、合并节点、生成邀请码、批量导入。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    family_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    user_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_table: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    detail: Mapped[Any | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_family_time", "family_id", "created_at"),
        Index("idx_user", "user_id", "created_at"),
        Index("idx_action", "action", "created_at"),
    )


class Notification(Base):
    """站内通知。"""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), nullable=False)
    family_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))
    type: Mapped[int] = mapped_column(TINYINT, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str | None] = mapped_column(String(512))
    payload: Mapped[Any | None] = mapped_column(JSON)
    is_read: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("idx_user_read", "user_id", "is_read", "created_at"),)
