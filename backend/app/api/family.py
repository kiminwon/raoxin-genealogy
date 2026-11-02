"""宗族信息。"""

from __future__ import annotations

import secrets

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OptionalUser, load_family, resolve_context
from app.models.family import Branch, Family
from app.services.family_member import activate, list_for_user
from app.services.generation import explain_poem
from app.services.missing import count_missing_for_family

router = APIRouter(prefix="/family", tags=["family"])

# 与 invite.py 同一套字符表：去掉 I / O / 0 / 1，长辈念出来不会搞混
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _ensure_view_code(db, family: Family) -> None:
    """族里还没有浏览码时补一个。

    为什么**任何族成员**都能触发、而不是只有族管理员：
    分享是接力修谱的起点（决策 5「一人建树 → 码发家族群」）。如果只有管理员
    能生成码，普通族人点分享只会得到一个不带码的链接，对方还得手输家族码——
    这正是"分享出去却进不来"的那道坎。

    权限边界没有松动：浏览码给的是**看脱敏全族树**，能调这个接口的人本来就
    已经是本族成员、本来就能看到这棵树。写权限与它无关（只由认领后的
    `edit_scope` 决定）。族里真想关掉对外分享时，把 `view_code_enabled` 置 0，
    这里就不再补码，端上也就不显示分享按钮。
    """
    if not family.view_code_enabled or family.view_code:
        return
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        taken = db.execute(select(Family.id).where(Family.view_code == code)).first()
        if taken is None:
            family.view_code = code
            db.commit()
            return


@router.get("/mine", summary="我所属的宗族")
async def my_family(user: CurrentUser, db: DbSession) -> dict:
    if user.family_id is None:
        return {"code": 0, "msg": "ok", "data": {"family": None, "joined": False}}

    family = db.get(Family, user.family_id)
    if family is None:
        return {"code": 0, "msg": "ok", "data": {"family": None, "joined": False}}

    # 让分享链接永远带得上码（见 _ensure_view_code 的说明）
    _ensure_view_code(db, family)

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "joined": True,
            "family": _family_brief(family),
            "role": user.role,
            "claimedPersonId": user.claimed_person_id,
        },
    }


@router.get("/list", summary="我加入的全部宗族")
async def my_families(user: CurrentUser, db: DbSession) -> dict:
    """一个账号可以同时属于父系谱和母系谱，这里把它们都列出来。

    当前族排最前 —— 端上弹出的切换面板第一项就是"你现在在哪"，
    不用再去找哪个打了勾。
    """
    members = list_for_user(db, user.id)
    if not members:
        return {"code": 0, "msg": "ok", "data": {"families": [], "currentFamilyId": None}}

    fams = {
        f.id: f
        for f in db.execute(
            select(Family).where(Family.id.in_([m.family_id for m in members]))
        ).scalars()
    }

    items = []
    for m in members:
        fam = fams.get(m.family_id)
        # 族被停用就不列出来，但也不删成员记录 —— 留痕，将来能查
        if fam is None or fam.status != 1:
            continue
        items.append(
            {
                "familyId": fam.id,
                "name": fam.name,
                "surname": fam.surname,
                "role": int(m.role),
                "roleText": _role_text(int(m.role)),
                "claimedPersonId": m.claimed_person_id,
                "current": fam.id == user.family_id,
            }
        )

    # sort 是稳定的，只把当前族提到最前，其余保持"最近活跃倒序"
    items.sort(key=lambda x: not x["current"])

    return {
        "code": 0,
        "msg": "ok",
        "data": {"families": items, "currentFamilyId": user.family_id},
    }


class SwitchIn(BaseModel):
    familyId: int


@router.post("/switch", summary="切换当前宗族")
async def switch_current(body: SwitchIn, user: CurrentUser, db: DbSession) -> dict:
    """把"当前族"切到我在的另一个族。

    只改 app_user 上那三个快照列，不动成员关系 —— 切回来还是原来的角色和认领人。
    没加入过的族切不过去（activate 抛 40300），否则等于靠切族绕过入族。
    """
    family = load_family(db, body.familyId)
    activate(db, user, family.id)
    db.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "family": _family_brief(family),
            "role": int(user.role),
            "claimedPersonId": user.claimed_person_id,
            # 端上拿它决定要不要提示"先确认你是谁"
            "needClaim": user.claimed_person_id is None,
        },
    }


@router.get("/{family_id}/summary", summary="宗族首页信息（无需登录）")
async def family_summary(family_id: int, user: OptionalUser, db: DbSession) -> dict:
    family, graph, viewer = resolve_context(db, user, family_id)

    stats = count_missing_for_family(graph)
    stats["generationCount"] = len({p.generation for p in graph.person_of.values() if p.generation})

    branches = db.execute(
        select(Branch)
        .where(Branch.family_id == family_id, Branch.status == 1)
        .order_by(Branch.sort_no)
    ).scalars().all()

    branch_stats = []
    for b in branches:
        members = [p for p in graph.person_of.values() if p.branch_id == b.id]
        branch_stats.append(
            {
                "branchId": b.id,
                "name": b.name,
                "personCount": len(members),
            }
        )

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **_family_brief(family),
            "stats": stats,
            "branches": branch_stats,
            # 字辈诗拆成「字 -> 世次」，很多年轻人是第一次知道自家字辈的完整内容
            "generationPoem": explain_poem(
                family.generation_poem, family.first_gen_label
            ),
            "viewerScope": viewer.scope.value,
        },
    }


# 角色定位（2026-10-22 启明定）：2 号角色是「理事会成员」——
# 真正的房长年纪大、不操作智能机，实际用后台的是理事会里会用手机的年轻人。
# 房长授权他们代管，语义上仍含房长本人。
_ROLE_TEXT = {1: "族人", 2: "理事会成员", 3: "族管理员", 4: "平台运营"}


def _role_text(role: int) -> str:
    return _ROLE_TEXT.get(role, "族人")


def _family_brief(family: Family) -> dict:
    return {
        "id": family.id,
        "name": family.name,
        "surname": family.surname,
        "tangHao": family.tang_hao,
        "junWang": family.jun_wang,
        "originPlace": family.origin_place,
        "settlePlace": family.settle_place,
        "founderName": family.founder_name,
        "founderEra": family.founder_era,
        "generationPoemText": family.generation_poem,
        "viewCode": family.view_code if family.view_code_enabled else None,
        "dataRev": family.data_rev,
    }
