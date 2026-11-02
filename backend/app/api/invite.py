"""邀请码（决策第 5 条，2026-10-22 修订）。

**现在只有一种码：族级浏览码（code_type=1）。**

| code_type | 名称   | 谁生成   | 能力                   | 状态     |
|-----------|--------|----------|------------------------|----------|
| 1         | 浏览码 | 族管理员 | 看脱敏的全族树         | 在用     |
| 2         | 编辑码 | （原）任意已认领 | 看 + 编辑指定范围 | **已下线** |

为什么下线编辑码（启明 2026-10-22 拍板）：

1. **权限不该由链接携带。** 写权限本来就绑在 `viewer.can_edit(pid)`（`edit_scope`），
   而 `edit_scope` 的唯一正常来源是**认领**。编辑码试图让"链接带权限"，等于在
   身份系统之外开了一条查不到人的后路：谁拿码谁都能写。
2. **代录需求不需要它。** 理事会成员（role=2）本来就是全族可写，志愿者分房录入
   走的是角色 + `POST /admin/grant-scope`（可追溯、可撤销），不是匿名码。
3. **它一次都没被用过。** 生成它的前提（已认领）本身就自动带来了更宽的直系范围
   （`close_relatives(up=2, down=2)`，含兄弟姐妹及其配偶），而它上 3 代却不含兄弟
   姐妹 —— 增量只有曾祖辈，那一辈通常由理事会统一录。实测：本地 0 次、云端 0 次
   被接受，`edit_scope` 里 source=2 的授权行一条都没有。

**个人分享仍然保留，但它带的是「锚点」不是「权限」**：分享路径里带
`?code=<族浏览码>&anchor=<分享人 personId>`，被分享人未认领时，「我的一家」
就聚焦在分享人身上——他第一眼看到的是自己熟悉的名字，而不是从始祖开始找自己。

历史编辑码的 `accept` 保持兼容：老链接不至于一夜失效，但不再签发新的。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OptionalUser, resolve_context
from app.core.constants import (
    InviteCodeType,
    ScopeSource,
    UserRole,
    ViewerScope,
)
from app.core.errors import BizError, ErrCode, forbidden, not_found
from app.models.family import Family
from app.models.person import Person
from app.models.user import InviteCode
from app.services.family_member import join_and_activate
from app.services.invite_service import load_invite_or_raise
from app.services.mask import Viewer, cn_number, project_person_brief
from app.services.scope_service import RelationGraph, grant_edit_scope

router = APIRouter(prefix="/invite", tags=["invite"])

# 去掉 I / O / 0 / 1 —— 长辈们在家族群里手输或念出来时不会搞混
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code(db, length: int = 8) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        exists = db.execute(select(InviteCode.id).where(InviteCode.code == code)).first()
        if exists is None:
            return code
    raise BizError(ErrCode.SERVER_ERROR, "邀请码生成失败，请重试")


# ============================================================
# 我生成的邀请码
# 注意：必须注册在 /{code} 之前，否则 GET /invite/mine 会被当成 code="mine"
# ============================================================


@router.get("/mine", summary="我生成过的邀请码")
async def my_invites(user: CurrentUser, db: DbSession) -> dict:
    rows = db.execute(
        select(InviteCode)
        .where(InviteCode.inviter_user_id == user.id)
        .order_by(InviteCode.created_at.desc())
        .limit(50)
    ).scalars().all()

    graph_cache: dict[int, RelationGraph] = {}
    items = []
    for inv in rows:
        if inv.family_id not in graph_cache:
            graph_cache[inv.family_id] = RelationGraph.load(db, inv.family_id)
        g = graph_cache[inv.family_id]
        anchor = g.person_of.get(inv.anchor_person_id) if inv.anchor_person_id else None
        items.append(
            {
                "inviteId": inv.id,
                "code": inv.code,
                "codeType": inv.code_type,
                "anchorPersonId": inv.anchor_person_id,
                "anchorName": _display(anchor),
                "inviteNote": inv.invite_note,
                "scopeNodeCount": inv.scope_node_count,
                "usedCount": inv.used_count,
                "maxUses": inv.max_uses,
                "expiresAt": inv.expires_at.isoformat() if inv.expires_at else None,
                "status": inv.status,
                "isExpired": bool(inv.expires_at and inv.expires_at < datetime.now()),
            }
        )

    return {"code": 0, "msg": "ok", "data": {"total": len(items), "items": items}}


# ============================================================
# 落地页预览（无需登录）
# ============================================================


@router.get("/{code}", summary="邀请码落地页预览（无需登录）")
async def preview(
    code: str,
    user: OptionalUser,
    db: DbSession,
    previewPersonId: int | None = Query(None),
) -> dict:
    inv = _load_code(db, code)
    family = db.get(Family, inv.family_id)

    # 分享链接带 ?anchor=<personId>（分享人）时不改变权限，只改变"落地页/我的一家
    # 先看谁"。浏览码本身没有 anchor，所以这里以 query 优先；编辑码的历史记录
    # 仍按签发时的 anchor_person_id 兜底。
    anchor_id = previewPersonId or inv.anchor_person_id
    graph = RelationGraph.load(db, inv.family_id)
    if anchor_id is not None and anchor_id not in graph.person_of:
        anchor_id = None

    # 未登录访客只给三代脱敏预览。
    # 一是隐私边界，二是转化设计——"这是你家，但只给你看三代"比"什么都没有"更能促使人认领。
    guest = Viewer(
        scope=ViewerScope.LINEAGE if user is not None else ViewerScope.GUEST,
        user_id=user.id if user else None,
        role=user.role if user else 1,
        family_id=inv.family_id,
        claimed_person_id=user.claimed_person_id if user else None,
    )

    preview_block = _three_generation_preview(graph, anchor_id, guest)
    anchor_brief = _anchor_brief(graph, anchor_id, guest)

    scope_preview = None
    if inv.code_type == int(InviteCodeType.EDIT):
        scope_preview = {
            "nodeCount": inv.scope_node_count or len(inv.scope_person_ids or []),
            "canEdit": user is not None,
            "summary": _scope_summary(inv.scope_rule or {}, inv.scope_node_count or 0),
        }

    stats = _family_stats(graph)

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "valid": True,
            "codeType": inv.code_type,
            "code": inv.code,
            "family": {
                "id": family.id,
                "name": family.name,
                "surname": family.surname,
                "tangHao": family.tang_hao,
                "settlePlace": family.settle_place,
                "founderName": family.founder_name,
                "founderEra": family.founder_era,
                "personCount": stats["personCount"],
                "generationCount": stats["generationCount"],
            },
            "inviter": {
                "nickname": _inviter_nickname(db, inv),
                "personId": inv.anchor_person_id,
                "note": inv.invite_note,
            },
            "anchor": anchor_brief,
            "scopePreview": scope_preview,
            "preview": preview_block,
        },
    }


class AcceptIn(BaseModel):
    """接受邀请时一起带上来的东西。

    `anchorPersonId` 是分享链接里的锚点（分享人）。放 body 而不是 query：
    POST 的参数就该在 body 里，也免得 query 里的拼串在端上被归一化器误当成路径。
    """

    anchorPersonId: int | None = None


@router.post("/{code}/accept", summary="接受邀请")
async def accept(
    code: str,
    user: CurrentUser,
    db: DbSession,
    body: AcceptIn | None = None,
) -> dict:
    inv = _load_code(db, code)
    anchor_person_id = body.anchorPersonId if body else None

    if inv.max_uses and inv.used_count >= inv.max_uses:
        raise BizError(ErrCode.INVITE_INVALID, "该邀请链接已被使用完")

    graph = RelationGraph.load(db, inv.family_id)

    granted: list[int] = []
    if inv.code_type == int(InviteCodeType.EDIT):
        # 鉴权以签发时快照为准，不用规则重算 —— 防止权限随树变动漂移
        snapshot = [int(x) for x in (inv.scope_person_ids or [])]
        valid = [pid for pid in snapshot if pid in graph.person_of]
        if valid:
            grant_edit_scope(
                db,
                user_id=user.id,
                family_id=inv.family_id,
                person_ids=valid,
                source=ScopeSource.INVITE_CODE,
                invite_code_id=inv.id,
                granted_by=inv.inviter_user_id,
                expires_at=inv.expires_at,
            )
            granted = valid

    inv.used_count = int(inv.used_count) + 1
    # 一个账号可以同属多个族（父系谱 + 母系谱），user_family 记"我加入过哪些族"。
    #
    # 这里顺手把"当前族"切到这个族：他是点着这个族的码进来的，要看的就是这个族。
    # 已经在这个族时这一步幂等；之前是 `if user.family_id is None` 才绑，
    # 那行是"已入族就永远绑不上第二个族"的元凶，也是母系谱一直没法做的原因。
    join_and_activate(db, user, inv.family_id)
    db.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "codeType": inv.code_type,
            "familyId": inv.family_id,
            # 切族会连"我在这个族是谁、认领了谁"一起换掉（那三列是当前族快照）。
            # 三个值必须一起回给端上：只回 familyId 的话，端上还拿着旧族的
            # claimedPersonId，会以为"我已经认领过"，于是跳过认领、
            # 然后第一次写操作被 40301 挡回来 —— 一个很难自查的死角。
            "role": int(user.role),
            "claimedPersonId": user.claimed_person_id,
            "needClaim": user.claimed_person_id is None,
            "grantedScopes": {
                "personIds": granted,
                "nodeCount": len(granted),
            },
            # 进来先看谁：分享链接带的锚点优先，其次历史编辑码签发时的锚点。
            # 它只决定「我的一家」初始聚焦到谁，**不带任何写权限**。
            "recommendAnchor": _recommend_anchor(graph, inv, user, anchor_person_id),
        },
    }


# ============================================================
# 生成与管理
# ============================================================


class GenerateIn(BaseModel):
    familyId: int
    codeType: int = Field(int(InviteCodeType.VIEW), description="1 浏览码（唯一在用的类型）")
    anchorPersonId: int | None = None
    scopeRule: dict | None = None
    inviteNote: str | None = None
    maxUses: int = 0
    expiresInDays: int | None = None


@router.post("/generate", summary="生成邀请码")
async def generate(body: GenerateIn, user: CurrentUser, db: DbSession) -> dict:
    # resolve_context 负责"族存在 + 我确实是这个族的成员"，
    # 返回值里的 graph/viewer 这里用不上（浏览码不需要范围计算）
    family, _graph, _viewer = resolve_context(db, user, body.familyId)

    if body.codeType != int(InviteCodeType.VIEW):
        # 线上旧版端还在调 codeType=2。这里必须报错，不能静默降级成浏览码：
        # 旧端拿到返回值里的 scopeNodeCount 会当成"已经授权了 N 个节点"显示给用户。
        # 明确拒绝，旧端才会走进 catch 并提示用户改用分享。
        raise BizError(
            ErrCode.PARAM_INVALID,
            "个人编辑码已下线：分享家族链接即可，家人认领自己之后就能填自己那一支",
        )

    if user.role < int(UserRole.FAMILY_ADMIN):
        raise forbidden("只有族管理员可以生成全族浏览码")
    return _make_view_code(db, family)



@router.post("/{invite_id}/revoke", summary="吊销邀请码")
async def revoke(invite_id: int, user: CurrentUser, db: DbSession) -> dict:
    inv = db.get(InviteCode, invite_id)
    if inv is None:
        raise not_found("邀请码")
    if inv.inviter_user_id != user.id and user.role < int(UserRole.FAMILY_ADMIN):
        raise forbidden("只能吊销自己生成的邀请码")

    inv.status = 0
    # 同时回收由它产生的授权
    from app.models.user import EditScope

    db.query(EditScope).filter(
        EditScope.invite_code_id == invite_id, EditScope.status == 1
    ).update({"status": 0}, synchronize_session=False)
    db.commit()

    return {"code": 0, "msg": "ok", "data": {"inviteId": invite_id, "status": 0}}


@router.get("/{code}/scope", summary="查看编辑码的授权范围（给生成前的确认页用）")
async def scope_preview(
    code: str,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    inv = _load_code(db, code)
    graph = RelationGraph.load(db, inv.family_id)
    ids = [int(x) for x in (inv.scope_person_ids or []) if int(x) in graph.person_of]

    nodes = []
    for pid in ids:
        person = graph.person_of.get(pid)
        if person is None:
            continue
        nodes.append(
            {
                "id": pid,
                "displayName": _display(person),
                "generation": person.generation,
                "generationLabel": f"第{cn_number(person.generation)}世" if person.generation else None,
                "lineageRole": person.lineage_role,
                "isDirect": pid == inv.anchor_person_id,
            }
        )

    nodes.sort(key=lambda x: (x["generation"] or 0, x["id"]))
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "scopeRule": inv.scope_rule,
            "nodeCount": len(nodes),
            "nodes": nodes,
            "summary": _scope_summary(inv.scope_rule or {}, len(nodes)),
        },
    }


# ============================================================
# 辅助
# ============================================================


def _load_code(db, code: str) -> InviteCode:
    """委托给 invite_service：两种码存储位置不同，必须统一入口解析。"""
    return load_invite_or_raise(db, code)


def _make_view_code(db, family: Family) -> dict:
    if not family.view_code:
        family.view_code = _gen_code(db)
        db.commit()
    family.view_code_enabled = 1
    db.commit()
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "code": family.view_code,
            "codeType": int(InviteCodeType.VIEW),
            "scopeNodeCount": None,
            # 分享卡片直接用这句：点开就是预览态，不用户名再输一遍码
            "shareText": f"{family.name}的电子家谱 · 点开直接进，看看您在族里的位置",
        },
    }


def _three_generation_preview(graph: RelationGraph, anchor_id: int | None, viewer) -> dict:
    """落地页只给三代。缺口本身就是动机——"这是你家，但只给你看三代"。"""
    if anchor_id is None or anchor_id not in graph.person_of:
        # 没有锚点时给始祖往下三代
        root_id = None
        best_gen = 10**9
        for pid, p in graph.person_of.items():
            if p.generation and p.generation < best_gen and not graph.father_of.get(pid):
                best_gen = p.generation
                root_id = pid
        anchor_id = root_id

    if anchor_id is None:
        return {"generations": [], "note": "该宗族尚未录入人物数据"}

    anchor = graph.person_of[anchor_id]
    anchor_gen = anchor.generation or 1
    lo, hi = anchor_gen - 1, anchor_gen + 1

    buckets: dict[int, list] = {}
    for pid, person in graph.person_of.items():
        if person.generation is None or not (lo <= person.generation <= hi):
            continue
        if person.lineage_role not in (1, 2, 3):
            continue
        brief = project_person_brief(person, viewer)
        if brief is None:
            continue
        brief["isAnchorPath"] = pid == anchor_id
        buckets.setdefault(person.generation, []).append(brief)

    generations = [
        {
            "generation": g,
            "label": f"第{cn_number(g)}世",
            "persons": sorted(buckets[g], key=lambda x: x["id"])[:12],
            "total": len(buckets[g]),
        }
        for g in sorted(buckets)
    ]

    return {
        "generations": generations,
        "note": "仅展示三代脱敏信息，加入后可查看完整世系",
    }


def _anchor_brief(graph: RelationGraph, anchor_id: int | None, viewer) -> dict | None:
    if anchor_id is None:
        return None
    person = graph.person_of.get(anchor_id)
    if person is None:
        return None
    brief = project_person_brief(person, viewer)
    if brief is None:
        return None
    brief["generationLabel"] = (
        f"第{cn_number(person.generation)}世" if person.generation else None
    )
    return brief


def _inviter_nickname(db, inv: InviteCode) -> str | None:
    if not inv.inviter_user_id:
        return None
    from app.models.user import AppUser

    u = db.get(AppUser, inv.inviter_user_id)
    return u.nickname if u else None


def _family_stats(graph: RelationGraph) -> dict:
    gens = [p.generation for p in graph.person_of.values() if p.generation]
    return {
        "personCount": len(graph.person_of),
        "generationCount": (max(gens) - min(gens) + 1) if gens else 0,
    }


def _scope_summary(rule: dict, node_count: int) -> str:
    parts = []
    up = rule.get("up", 3)
    down = rule.get("down", 2)
    if up:
        parts.append(f"上{up}代")
    if down:
        parts.append(f"下{down}代")
    if rule.get("spouse"):
        parts.append("含配偶")
    if rule.get("siblings"):
        parts.append("含兄弟姐妹")
    if rule.get("externalDesc"):
        parts.append("含外孙")
    return f"可填写：{'、'.join(parts)}（共 {node_count} 个节点）"


def _display(person: Person | None) -> str | None:
    if person is None:
        return None
    if person.name:
        return person.name
    if person.surname:
        return f"{person.surname}氏"
    return "配某氏"


def _recommend_anchor(
    graph: RelationGraph, inv: InviteCode, user, anchor_id: int | None = None
) -> int | None:
    """进来先看谁。

    分享链接带 `?anchor=<分享人>` 时用它；否则退回历史编辑码签发时的锚点；
    都没有就返回 None（端上从始祖那一屏开始）。

    注意这不是"你可能是我"的判定——认领仍然靠答题自证。它只是视角起点。
    """
    for pid in (anchor_id, inv.anchor_person_id):
        if pid and pid in graph.person_of:
            return pid
    return None
