"""认证。"""

from __future__ import annotations

import hashlib
from datetime import datetime

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OptionalUser
from app.core.config import settings
from app.core.errors import BizError, ErrCode
from app.core.security import create_access_token
from app.models.family import Family
from app.models.user import AppUser, Claim, InviteCode

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    code: str = Field(..., description="wx.login() 拿到的 code")
    inviteCode: str | None = Field(None, description="从哪个邀请码进来的")


class UserOut(BaseModel):
    id: int
    role: int
    familyId: int | None
    claimedPersonId: int | None
    needClaim: bool


async def _code2openid(code: str) -> str:
    """code 换 openid。

    开发期走 mock：不调微信接口，用 code 派生一个稳定的假 openid，
    这样小程序在开发者工具里勾选"不校验合法域名"就能完整联调。
    上线前把 WX_MOCK_LOGIN 设为 false。
    """
    if settings.wx_mock_login or not settings.wx_appid:
        # 开发期允许直接指名道姓地登录：code="dev:mock_qiming" -> openid="mock_qiming"。
        # 没有这一支的话，seed_demo 里写死的那些演示用户（管理员、已认领的启明）
        # 永远登不上来——派生出来的 openid 和它们对不上，演示数据就成了摆设。
        if code.startswith("dev:"):
            return code[4:]

        digest = hashlib.sha256(f"mock:{code}".encode()).hexdigest()[:28]
        return f"mock_{digest}"

    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": settings.wx_appid,
                "secret": settings.wx_secret,
                "js_code": code,
                "grant_type": "authorization_code",
            },
        )
    data = resp.json()
    if "openid" not in data:
        raise BizError(
            ErrCode.UNAUTHORIZED,
            f"微信登录失败：{data.get('errmsg', '未知错误')}",
        )
    return str(data["openid"])


@router.post("/login", summary="微信登录")
async def login(body: LoginIn, db: DbSession) -> dict:
    openid = await _code2openid(body.code)

    user = db.execute(select(AppUser).where(AppUser.openid == openid)).scalar_one_or_none()
    if user is None:
        user = AppUser(openid=openid, role=1, status=1)
        db.add(user)
        db.commit()
        db.refresh(user)

    user.last_login_at = datetime.now()
    db.commit()

    token, expires_in = create_access_token(user.id)

    # 带邀请码进来时，返回待接受的邀请信息，让前端弹确认框。
    # 走 load_invite 而不是直接查表：族浏览码存在 family.view_code 上，
    # 直接查表会让最常见的"从家族群浏览码进来"拿不到 pending 提示。
    pending = None
    if body.inviteCode:
        from app.services.invite_service import load_invite

        inv = load_invite(db, body.inviteCode)
        if inv is not None and inv.status == 1:
            anchor_name = None
            if inv.anchor_person_id:
                from app.models.person import Person

                anchor = db.get(Person, inv.anchor_person_id)
                if anchor is not None:
                    anchor_name = anchor.name or (
                        f"{anchor.surname}氏" if anchor.surname else None
                    )
            pending = {
                "codeType": inv.code_type,
                "familyId": inv.family_id,
                "anchorPersonId": inv.anchor_person_id,
                "anchorName": anchor_name,
                "scopeNodeCount": inv.scope_node_count,
            }

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "token": token,
            "expiresIn": expires_in,
            "user": _user_out(db, user),
            "pendingInvite": pending,
        },
    }


@router.get("/me", summary="当前用户状态")
async def me(user: OptionalUser, db: DbSession) -> dict:
    if user is None:
        return {
            "code": 0,
            "msg": "ok",
            "data": {
                "loggedIn": False,
                "user": None,
                "pendingClaim": None,
            },
        }

    pending = db.execute(
        select(Claim)
        .where(Claim.user_id == user.id, Claim.status == 1)
        .order_by(Claim.created_at.desc())
    ).scalars().first()

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "loggedIn": True,
            "user": _user_out(db, user),
            "pendingClaim": (
                {
                    "claimId": pending.id,
                    "personId": pending.person_id,
                    "status": pending.status,
                }
                if pending
                else None
            ),
        },
    }


@router.patch("/profile", summary="更新昵称等资料")
async def update_profile(user: CurrentUser, db: DbSession, nickname: str | None = None) -> dict:
    if nickname:
        user.nickname = nickname[:64]
        db.commit()
    return {"code": 0, "msg": "ok", "data": _user_out(db, user)}


def _user_out(db, user: AppUser) -> dict:
    family_name = None
    if user.family_id:
        fam = db.get(Family, user.family_id)
        family_name = fam.name if fam else None
    return {
        "id": user.id,
        "nickname": user.nickname,
        "role": user.role,
        "familyId": user.family_id,
        "familyName": family_name,
        "claimedPersonId": user.claimed_person_id,
        "needClaim": user.claimed_person_id is None,
    }
