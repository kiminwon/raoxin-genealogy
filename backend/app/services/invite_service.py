"""邀请码解析。

## 一个必须集中处理的坑

**两种码存在两个地方：**
  - 族级浏览码（code_type=1）→ `family.view_code` 字段，一族人一个，长在族身上
  - 编辑码（code_type=2）    → `invite_code` 表的记录，一人签一张

但对外只有一个入口：用户拿到的是一个 8 位码，系统必须能从码找到邀请。

所以所有"按码取邀请"的地方都必须走 `load_invite()`——先查表，查不到再兜族浏览码。
一旦哪里绕过去直接 `select(InviteCode)`，族浏览码就会表现为「链接已失效」。
而族浏览码恰恰是家族群里传播最广的那个码，断了等于把最大的入口关掉。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import InviteCodeType
from app.core.errors import BizError, ErrCode
from app.models.family import Family
from app.models.user import InviteCode


def load_invite(db: Session, code: str) -> InviteCode | None:
    """按码取邀请，不校验有效性。找不到返回 None。"""
    normalized = (code or "").upper().strip()
    if not normalized:
        return None

    inv = db.execute(
        select(InviteCode).where(InviteCode.code == normalized)
    ).scalar_one_or_none()
    if inv is not None:
        return inv

    return _synthetic_view_code(db, normalized)


def load_invite_or_raise(db: Session, code: str) -> InviteCode:
    """按码取邀请并校验。任何不通过都抛 40302。"""
    inv = load_invite(db, code)
    if inv is None or inv.status != 1:
        raise BizError(ErrCode.INVITE_INVALID, "链接已失效，请联系邀请人")

    if (
        inv.code_type == int(InviteCodeType.EDIT)
        and inv.expires_at
        and inv.expires_at < datetime.now()
    ):
        raise BizError(ErrCode.INVITE_INVALID, "链接已过期，请联系邀请人重新生成")

    if inv.max_uses and inv.used_count >= inv.max_uses:
        raise BizError(ErrCode.INVITE_INVALID, "该链接已被使用完")

    return inv


def _synthetic_view_code(db: Session, code: str) -> InviteCode | None:
    """把族浏览码包装成一个不落库的 InviteCode，让上层不必区分两种码。

    刻意不写库：这是 GET 预览路径，未登录用户就能触发，
    不该在只读请求里产生写入。调用方对 `id` 为 None 要能容忍——
    族浏览码不授编辑权，所以不会走到 `invite_code_id` 那一步。
    """
    family = db.execute(
        select(Family).where(
            Family.view_code == code,
            Family.view_code_enabled == 1,
            Family.status == 1,
        )
    ).scalar_one_or_none()
    if family is None:
        return None

    return InviteCode(
        family_id=family.id,
        code=family.view_code,
        code_type=int(InviteCodeType.VIEW),
        anchor_person_id=None,
        anchor_user_id=None,
        inviter_user_id=None,
        invite_note=None,
        scope_node_count=0,
        max_uses=0,
        used_count=0,
        status=1,
    )
