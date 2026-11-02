"""答题自证接口 —— 身份确认的主通道。

跟房长审批的关系：
    这里通过 = 直接生效，不经过任何人。
    `POST /person/{id}/claim`（房长审核）保留为兜底：答题失败、同名歧义、申诉。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, resolve_context
from app.core.errors import BizError, ErrCode
from app.models.person import Person
from app.services.identify_service import VerifyIn, build_quiz, verify

router = APIRouter(prefix="/auth/identify", tags=["identify"])


def _resolve_family_id(db, user, family_id: int | None) -> int:
    fid = family_id or user.family_id
    if fid is None:
        raise BizError(
            ErrCode.PARAM_INVALID,
            "请先通过族浏览码或邀请码进入某个宗族",
        )
    return int(fid)


@router.get("/quiz", summary="出题（答题自证第一步）")
async def get_quiz(
    user: CurrentUser,
    db: DbSession,
    familyId: int | None = Query(None, description="不传则用当前用户已加入的宗族"),
) -> dict:
    fid = _resolve_family_id(db, user, familyId)
    family, graph, viewer = resolve_context(db, user, fid)

    data = build_quiz(family, graph)

    # 已认领过就直接告诉他，别再答一遍题
    already = None
    if user.claimed_person_id:
        p = db.get(Person, user.claimed_person_id)
        if p is not None:
            already = {
                "personId": p.id,
                "name": p.name,
                "generation": p.generation,
            }
    data["alreadyClaimed"] = already
    return {"code": 0, "msg": "ok", "data": data}


class VerifyBody(BaseModel):
    familyId: int | None = None
    relation: str = Field("father", description="father / mother / spouse")
    relativeName: str = Field(..., min_length=1, description="所填亲属的姓名")
    selfName: str = Field(..., min_length=1, description="本人的姓名")
    extraName: str | None = Field(None, description="另一位家长姓名，同名歧义时可补填")
    selfGender: int | None = Field(None, description="本人性别 1男 2女，选填")
    inviteCode: str | None = Field(None, description="进来时带的码，仅留痕")


@router.post("/verify", summary="答题并自动确认身份")
async def verify_identity(body: VerifyBody, user: CurrentUser, db: DbSession) -> dict:
    fid = _resolve_family_id(db, user, body.familyId)
    family, graph, viewer = resolve_context(db, user, fid)

    if body.relation not in ("father", "mother", "spouse"):
        raise BizError(ErrCode.PARAM_INVALID, "relation 只能是 father / mother / spouse")

    data = verify(
        db,
        user=user,
        family=family,
        graph=graph,
        body=VerifyIn(
            relation=body.relation,
            relative_name=body.relativeName.strip(),
            self_name=body.selfName.strip(),
            extra_name=(body.extraName or "").strip() or None,
            self_gender=body.selfGender,
            invite_code=body.inviteCode,
        ),
    )
    return {"code": 0, "msg": "ok", "data": data}
