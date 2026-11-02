"""家族动态（tabBar 第三格）。

口径三句话：
1. **只收已经落地的动作**——待审的修订不进动态。还没成事实就写"谁补了什么"，
   族里的人会以为已经改了（审核驳回后再看就更乱）。
2. **只有本族成员能看**。动态里有"谁认了哪一位"，那是族内的事，与谱文同门。
3. 三类事件：补全/新增（revision 通过或自动合并）、谱文发布、认领通过。
   不再细分字段级 diff——那是修订审核页的事，动态只回答"最近谁在动"。

数据量：一个族几十到几百人，三类各取最近 30 条内存合并即可，
不建 activity 表。真要分页到几千条再上（那时也该有真正的消息流模型了）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, resolve_context
from app.core.errors import forbidden
from app.models.article import FamilyArticle
from app.models.system import Revision
from app.models.user import AppUser, Claim, UserFamily
from app.services.mask import project_name

router = APIRouter(tags=["feed"])

# 每类事件先取这么多，再合并排序截断。取小了会把"某一类特别活跃"的族裁掉。
SOURCE_TAKE = 30
MAX_LIMIT = 50

# revision.status：1待审 2通过 3驳回 4自动合并 5撤回。
# 只认 2/4 —— 写进动态的必须是已经改在谱上的。
_REV_LIVE = (2, 4)
# revision.op
_OP_CREATE = 1
_OP_DELETE = 3


def _person_name(graph, viewer, pid) -> str | None:
    """谱上那个人的展示名。取不到就是 None，调用方退回"某位族人"。"""
    if not pid:
        return None
    person = graph.person_of.get(int(pid))
    if person is None:
        return None
    _, display = project_name(person, viewer)
    return display or None


def _stamp(at) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S") if at else ""


def _day(at) -> str:
    return at.strftime("%Y-%m-%d") if at else ""


@router.get("/family/{family_id}/feed", summary="家族动态（本族成员）")
async def family_feed(
    family_id: int,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(30, ge=1, le=MAX_LIMIT, description="返回条数，最多 50"),
) -> dict:
    family, graph, viewer = resolve_context(db, user, family_id, with_close=False)

    # 成员资格按"这个族"查 user_family。app_user.family_id 是当前族快照，
    # 路径里的 family_id 可能是另一个族——用快照判断会放错人进来。
    member = db.execute(
        select(UserFamily).where(
            UserFamily.user_id == user.id,
            UserFamily.family_id == family_id,
        )
    ).scalar_one_or_none()
    if member is None:
        forbidden("只有本族成员能看家族动态")

    events: list[dict] = []

    # ---------- 1. 修订：补全 / 新增 ----------
    revs = db.execute(
        select(Revision)
        .where(Revision.family_id == family_id, Revision.status.in_(_REV_LIVE))
        .order_by(Revision.id.desc())
        .limit(SOURCE_TAKE)
    ).scalars().all()

    if revs:
        # 提交者昵称：一次查完，别在循环里逐条打库
        uids = {int(r.submitter_user_id) for r in revs}
        nick = {
            u.id: (u.nickname or "")
            for u in db.execute(select(AppUser).where(AppUser.id.in_(uids))).scalars().all()
        }
        for r in revs:
            payload = r.payload or {}
            name = _person_name(graph, viewer, r.target_id) or payload.get("name") or "一位族人"
            actor = _person_name(graph, viewer, r.submitter_person_id) or nick.get(
                int(r.submitter_user_id)
            ) or "族人"

            if int(r.op) == _OP_CREATE:
                text = f"新增了「{name}」"
                kind = "create"
            elif int(r.op) == _OP_DELETE:
                text = f"删除了「{name}」"
                kind = "delete"
            else:
                summary = (r.diff_summary or "").strip()
                text = f"补全了「{name}」的{summary}" if summary else f"更新了「{name}」的资料"
                kind = "edit"

            events.append(
                {
                    "key": f"rev-{r.id}",
                    "type": kind,
                    "actor": actor,
                    "text": text,
                    "personId": r.target_id,
                    "articleId": None,
                    "at": _stamp(r.reviewed_at or r.created_at),
                    "day": _day(r.reviewed_at or r.created_at),
                }
            )

    # ---------- 2. 谱文：写了 / 更新了 ----------
    arts = db.execute(
        select(FamilyArticle)
        .where(FamilyArticle.family_id == family_id, FamilyArticle.status == 1)
        .order_by(FamilyArticle.updated_at.desc(), FamilyArticle.id.desc())
        .limit(SOURCE_TAKE)
    ).scalars().all()

    if arts:
        uids = {int(a.created_by) for a in arts if a.created_by}
        nick = {
            u.id: (u.nickname or "")
            for u in db.execute(select(AppUser).where(AppUser.id.in_(uids))).scalars().all()
        }
        for a in arts:
            # 创建时间就是更新时间 = 新写的；否则是改过
            fresh = a.updated_at and a.created_at and a.updated_at <= a.created_at
            events.append(
                {
                    "key": f"art-{a.id}",
                    "type": "article",
                    "actor": nick.get(int(a.created_by), "") if a.created_by else "族人",
                    "text": f"{'写了' if fresh else '更新了'}《{a.title}》",
                    "personId": None,
                    "articleId": a.id,
                    "at": _stamp(a.updated_at or a.created_at),
                    "day": _day(a.updated_at or a.created_at),
                }
            )

    # ---------- 3. 认领：是谁上了谱 ----------
    claims = db.execute(
        select(Claim)
        .where(Claim.family_id == family_id, Claim.status == 2)
        .order_by(Claim.id.desc())
        .limit(SOURCE_TAKE)
    ).scalars().all()

    if claims:
        uids = {int(c.user_id) for c in claims}
        nick = {
            u.id: (u.nickname or "")
            for u in db.execute(select(AppUser).where(AppUser.id.in_(uids))).scalars().all()
        }
        for c in claims:
            name = _person_name(graph, viewer, c.person_id) or "一位祖先"
            events.append(
                {
                    "key": f"claim-{c.id}",
                    "type": "claim",
                    "actor": nick.get(int(c.user_id)) or "族人",
                    "text": f"认领了「{name}」",
                    "personId": c.person_id,
                    "articleId": None,
                    "at": _stamp(c.reviewed_at or c.created_at),
                    "day": _day(c.reviewed_at or c.created_at),
                }
            )

    events.sort(key=lambda e: e["at"], reverse=True)
    events = events[:limit]

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "familyName": family.name,
            "items": events,
            "total": len(events),
        },
    }
