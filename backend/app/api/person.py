"""人物详情、认领、待补全清单。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OptionalUser, resolve_context
from app.core.constants import (
    ClaimStatus,
    ClaimType,
    LineageRole,
    LivingStatus,
    MaritalStatus,
    ParentRelation,
    RevisionOp,
    ReviewStatus,
    UnionEndReason,
    UserRole,
    VerifyMethod,
)
from app.core.errors import BizError, ErrCode, forbidden, no_edit_scope, not_found
from app.models.person import FamilyUnion, ParentChild, Person
from app.models.system import Revision
from app.models.user import AppUser, Claim, UserFamily
from app.services.family_member import join
from app.services.mask import cn_number, project_person, project_person_brief
from app.services.missing import build_missing_tasks
from app.services.revision_service import submit_create, submit_update
from app.services.scope_service import RelationGraph, ensure_can_edit

router = APIRouter(prefix="/person", tags=["person"])


# ============================================================
# 详情
# ============================================================


@router.get("/{person_id}", summary="人物详情（按权限脱敏）")
async def person_detail(person_id: int, user: OptionalUser, db: DbSession) -> dict:
    person = _load_person(db, person_id)
    family, graph, viewer = resolve_context(db, user, person.family_id)

    data = project_person(person, viewer)
    if data is None:
        raise not_found("人物")

    data["relatives"] = _build_relatives(db, graph, viewer, person)
    data["biological"] = _build_biological(db, graph, viewer, person)
    # 缺口清单只给"能补的人"看。判据与 /missing、树上的角标、底部条
    # 必须是**同一个谓词** viewer.can_edit()：
    #   - 只能是本人？太窄。配偶/子女/被签发节点编辑码的人本来就能改，
    #     给一个"能改但看不到缺什么"的状态，等于让他在详情页盲改。
    #   - 任何登录族人？太宽。普通族人看到满屏别人的缺口，点进去必然被拒。
    # 2026-10-21 实报「点吴敏提示还差一项、点去补全说陈启华已补全」就是
    # 四个出口各用一套判据、还各自换了个人名。
    if viewer.can_edit(person_id):
        data["pending"] = _pending_brief(graph, person_id)
    else:
        data["pending"] = None

    return {"code": 0, "msg": "ok", "data": data}


@router.get("/{person_id}/relatives", summary="直系亲属（用于关系链）")
async def person_relatives(person_id: int, user: OptionalUser, db: DbSession) -> dict:
    person = _load_person(db, person_id)
    family, graph, viewer = resolve_context(db, user, person.family_id)
    return {"code": 0, "msg": "ok", "data": _build_relatives(db, graph, viewer, person)}


# ============================================================
# 待补全清单（接力修谱引擎）
# ============================================================


@router.get("/{person_id}/missing", summary="待补全清单")
async def person_missing(person_id: int, user: OptionalUser, db: DbSession) -> dict:
    person = _load_person(db, person_id)
    family, graph, viewer = resolve_context(db, user, person.family_id)

    # 清单与详情页的 pending、树上的角标、底部条**同一判据**：能编辑就能看。
    # 原来卡在"本人或管理员"，于是给直系亲属发了编辑权却不让他看缺什么，
    # 页面上还照样显示缺口数字——点进来就是 40300。
    if not viewer.can_edit(person_id):
        raise forbidden("这一位不在您的修订范围内，只有本人或房长能看这份清单")

    data = build_missing_tasks(graph, person_id, max_items=3)
    data["canSubmit"] = viewer.can_edit(person_id) or viewer.is_admin
    data["completeness"] = _person_completeness(graph, person_id)
    return {"code": 0, "msg": "ok", "data": data}


class MissingSubmitIn(BaseModel):
    key: str = Field(..., description="任务 key，如 mother_name / father / spouse")
    values: dict = Field(default_factory=dict, description="用户填写的内容")
    createRelation: dict | None = Field(
        None,
        description="新增人物时的挂载方式，如 {'type':'child_of','parentId':10020}",
    )
    skip: bool = Field(
        False,
        description="回答「没有」（如未婚 / 暂无子女）。只有任务标了 skippable 才允许，"
                    "落库为事实字段（maritalStatus / childless），不是「已跳过」标记",
    )


@router.post("/{person_id}/missing/submit", summary="提交待补全任务")
async def submit_missing(
    person_id: int,
    body: MissingSubmitIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    person = _load_person(db, person_id)
    family, graph, viewer = resolve_context(db, user, person.family_id)

    # 与 person_missing 同一个门禁：看得见清单的人才填得了。
    # 收得比 _resolve_write_mode 紧是有意的——这里提交的是"缺口答案"，
    # 前端根本没给他看过这一问；范围外的改动走详情页的修订入口。
    if not viewer.can_edit(person_id):
        raise forbidden("这一位不在您的修订范围内，只有本人或房长能填这份清单")

    # 重新算一次清单，确认这个任务确实存在（防止前端传脏 key）
    tasks = build_missing_tasks(graph, person_id, max_items=50)
    task = next((t for t in tasks["items"] if t["key"] == body.key), None)
    if task is None:
        raise BizError(ErrCode.PARAM_INVALID, "该任务不存在或已完成")

    task_type = task.get("type")

    # ---- 「没有」也是一种回答 ----
    #
    # 未婚、暂无子女都是常态。原来配偶这一问没有出口，用户只能编一个名字，
    # 或者永远卡在 96%——而"编一个"比"空着"更糟：谱里会多出一个不存在的人。
    # 所以这两项允许直接答"没有"，落的是**事实字段**（未婚 / 确认无子女），
    # 不是"用户不想填"的标记。将来查谱，"未婚"本身就是一句话。
    if body.skip:
        if not task.get("skippable"):
            raise BizError(ErrCode.PARAM_INVALID, "这一项需要填写，不能跳过")
        skip_fields = _SKIP_FIELDS.get(body.key)
        if not skip_fields:
            raise BizError(ErrCode.PARAM_INVALID, "这一项还不能回答「没有」")

        auto_merge = _resolve_write_mode(viewer, [person_id])
        rev = submit_update(
            db,
            viewer=viewer,
            family_id=family.id,
            person=person,
            fields=dict(skip_fields),
            submit_note=(
                f"待补全清单：{task['question']} → "
                f"{task.get('skipLabel') or '没有'}"
            ),
            auto_merge=auto_merge,
        )
        return _submit_out(rev)

    if task_type == "update_field":
        target_id = int(task["targetId"])
        auto_merge = _resolve_write_mode(viewer, [target_id])
        target = _load_person(db, target_id)
        rev = submit_update(
            db,
            viewer=viewer,
            family_id=family.id,
            person=target,
            fields={task["field"]: body.values.get(task["field"])},
            submit_note=f"待补全清单：{task['question']}",
            auto_merge=auto_merge,
        )
    elif task_type in ("create_person", "create_union"):
        parent_id = int(person_id)
        prefill = dict(task.get("prefill") or {})
        relation = body.createRelation or _infer_relation(prefill, person_id)

        auto_merge = _resolve_write_mode(viewer, [parent_id])

        fields = dict(body.values)
        # 清单里"配偶"这一问的答案是 partnerName，但落库时它就是新人的姓名。
        # 不转的话 submit_create 会把 partnerName 过滤掉，然后报"至少需要填写姓名或姓氏"。
        if "partnerName" in fields and "name" not in fields:
            fields["name"] = fields.pop("partnerName")
        for k in ("surname", "gender", "generation"):
            if k not in fields and prefill.get(k) is not None:
                fields[k] = prefill[k]
        if task_type == "create_union":
            fields.setdefault("surname", None)

        rev = submit_create(
            db,
            viewer=viewer,
            family_id=family.id,
            fields=fields,
            relation=relation,
            submit_note=f"待补全清单：{task['question']}",
            auto_merge=auto_merge,
        )
    else:
        raise BizError(ErrCode.PARAM_INVALID, "未知的任务类型")

    return _submit_out(rev)


# ============================================================
# 修改 / 新增（走修订队列）
# ============================================================


class ReviseIn(BaseModel):
    fields: dict = Field(default_factory=dict)
    submitNote: str | None = None
    idempotencyKey: str | None = Field(None, description="幂等键，防网络抖动重复提交")


@router.patch("/{person_id}", summary="修改人物（提交为待审修订）")
async def revise_person(
    person_id: int,
    body: ReviseIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    person = _load_person(db, person_id)
    family, graph, viewer = resolve_context(db, user, person.family_id)

    # 幂等：同一个 idempotencyKey 直接返回已有修订
    if body.idempotencyKey:
        dup = db.execute(
            select(Revision).where(
                Revision.submitter_user_id == user.id,
                Revision.diff_summary.like(f"%{body.idempotencyKey}%"),
            )
        ).scalars().first()
        if dup is not None:
            return {"code": 0, "msg": "ok", "data": _revision_out(dup)}

    auto_merge = _resolve_write_mode(viewer, [person_id])
    rev = submit_update(
        db,
        viewer=viewer,
        family_id=family.id,
        person=person,
        fields=body.fields,
        submit_note=body.submitNote,
        auto_merge=auto_merge,
    )

    result = _revision_out(rev)

    # 字辈校验：只提示，不阻断（决策第 1 条）
    from app.services.generation import check_generation

    hint = check_generation(
        family,
        generation_name=body.fields.get("generationName"),
        claimed_generation=body.fields.get("generation"),
        lineage_role=person.lineage_role,
    )
    result["generationHint"] = hint

    return {"code": 0, "msg": "ok", "data": result}


class CreateIn(BaseModel):
    fields: dict = Field(default_factory=dict)
    relation: dict | None = None
    submitNote: str | None = None


@router.post("", summary="新增人物（提交为待审修订）")
async def create_person(body: CreateIn, user: CurrentUser, db: DbSession) -> dict:
    fid = user.family_id
    if fid is None:
        raise BizError(ErrCode.PARAM_INVALID, "请先加入宗族")

    family, graph, viewer = resolve_context(db, user, fid)

    # 新增必须挂到一个已有节点下，且你对那个节点有权限
    #
    # 锚点的取法跟着关系类型走，三种都要覆盖：
    #   child_of  -> parentId  （给某人添子女）
    #   spouse_of -> partnerId （给某人添配偶）
    #   father_of / mother_of -> childId（给某人添父母，锚点是**那个孩子**）
    # 漏掉 childId 时，"在树上给爷爷补父亲"会报"需要指定挂载位置"——
    # 权限其实完全够，纯粹是这里没认。
    anchor = None
    rel = body.relation or {}
    for k in ("parentId", "partnerId", "childId"):
        if rel.get(k):
            anchor = int(rel[k])
            break
    if anchor is None:
        raise BizError(ErrCode.PARAM_INVALID, "新增成员需要指定挂载位置（parentId / partnerId / childId）")

    auto_merge = _resolve_write_mode(viewer, [anchor])

    rev = submit_create(
        db,
        viewer=viewer,
        family_id=fid,
        fields=body.fields,
        relation=rel,
        submit_note=body.submitNote,
        auto_merge=auto_merge,
    )
    return {"code": 0, "msg": "ok", "data": _revision_out(rev)}


# ============================================================
# 认领
# ============================================================


class ClaimIn(BaseModel):
    claimType: int = int(ClaimType.CLAIM_EXISTING)
    evidence: str | None = None
    evidenceFiles: list[str] | None = None


@router.post("/{person_id}/claim", summary="提交认领申请")
async def claim_person(
    person_id: int,
    body: ClaimIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    person = _load_person(db, person_id)
    family, graph, viewer = resolve_context(db, user, person.family_id)

    if user.claimed_person_id == person_id:
        return {
            "code": 0,
            "msg": "ok",
            "data": {"claimId": None, "status": int(ClaimStatus.APPROVED), "statusText": "您已认领该节点"},
        }

    if user.claimed_person_id:
        raise BizError(ErrCode.PARAM_INVALID, "您已经认领了其他节点，如需更换请先联系房长")

    # 已被他人认领 -> 冲突，不先到先得
    taken = db.execute(
        select(Claim).where(
            Claim.person_id == person_id,
            Claim.status == int(ClaimStatus.APPROVED),
        )
    ).scalars().first()
    if taken is not None and taken.user_id != user.id:
        holder = db.get(Person, person_id)
        raise BizError(
            ErrCode.CLAIM_CONFLICT,
            "该节点已被他人认领",
            {
                "conflictClaimId": taken.id,
                "hint": "如果您确认这是您本人，可以提交申诉，由房长核实",
                "personName": _display(holder),
            },
        )

    my_pending = db.execute(
        select(Claim).where(
            Claim.user_id == user.id,
            Claim.person_id == person_id,
            Claim.status == int(ClaimStatus.PENDING),
        )
    ).scalars().first()
    if my_pending is not None:
        return {
            "code": 0,
            "msg": "ok",
            "data": {
                "claimId": my_pending.id,
                "status": my_pending.status,
                "statusText": "已提交，等待审核",
            },
        }

    claim = Claim(
        family_id=person.family_id,
        user_id=user.id,
        person_id=person_id,
        claim_type=body.claimType,
        evidence=body.evidence,
        evidence_files=body.evidenceFiles,
        # 这是兜底通道，不是主路径：主路径是答题自证（/auth/identify/verify）。
        # 走到这里说明答题没对上，或同名有争议，才需要人来判断。
        verify_method=int(VerifyMethod.REVIEW),
        status=int(ClaimStatus.PENDING),
        created_at=datetime.now(),
    )
    db.add(claim)

    # 只登记"他属于这个族"，**不切当前族**：人工审核还没通过，
    # 他还看不到这个族的树，此刻把当前族改掉没意义。
    join(db, user, person.family_id)
    db.commit()
    db.refresh(claim)

    # 找一位真正的审核人（房长及以上且有本人节点）。
    # 原来是取族里第一个人，结果第一个人永远是始祖 —— 提示里会出现
    # "已提交，等待 陈XX 审核"，而那位祖宗早就不在了。
    #
    # 走 user_family 而不是 app_user：一个账号可以同属多个族，他在本族是房长、
    # 此刻却在看另一个族时 app_user.family_id 并不指向这里，用 app_user 筛会
    # 把本族的管理员整个漏掉，提示里就成了"等待（无）审核"。
    reviewer_row = db.execute(
        select(UserFamily.claimed_person_id)
        .join(AppUser, AppUser.id == UserFamily.user_id)
        .where(
            UserFamily.family_id == person.family_id,
            UserFamily.status == 1,
            UserFamily.role >= int(UserRole.BRANCH_HEAD),
            UserFamily.claimed_person_id.isnot(None),
            AppUser.status == 1,
        )
        .order_by(UserFamily.role.desc())
    ).first()
    reviewer_person = db.get(Person, reviewer_row[0]) if reviewer_row else None

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "claimId": claim.id,
            "status": claim.status,
            "statusText": "已提交，等待房长核实",
            # 显示一个具体的名字比显示"管理员"更容易获得信任与提交
            "reviewerName": _display(reviewer_person) if reviewer_person else None,
            "nextHint": "答题自证能立刻通过；人工核实会慢一些，可以先用族浏览码浏览全族",
        },
    }


# ============================================================
# 内部辅助
# ============================================================


def _load_person(db, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None or person.status != 1:
        raise not_found("人物")
    return person


def _display(person: Person | None) -> str | None:
    if person is None:
        return None
    if person.name:
        return person.name
    if person.surname:
        return f"{person.surname}氏"
    return "配某氏"


def _brief(graph: RelationGraph, viewer, person_id: int, **extra) -> dict | None:
    person = graph.person_of.get(person_id)
    if person is None:
        return None
    d = project_person_brief(person, viewer)
    if d is None:
        return None
    d.update(extra)
    return d


def _union_between(graph: RelationGraph, a: int, b: int):
    """两人之间的婚姻单元。多妻时挑序号最小的一段（元配优先）。

    为什么需要它：`spouses_of` 只给"谁跟谁是配偶"，不给"第几段、有没有离"。
    详情页要写「元配」「继配 · 已离异」，光靠人 id 拼不出来。
    """
    best = None
    for u in graph.unions.values():
        if {u.partner_a_id, u.partner_b_id} != {a, b}:
            continue
        if best is None or (u.union_seq, u.id) < (best.union_seq, best.id):
            best = u
    return best


def _current_union_id(graph: RelationGraph, person_id: int) -> int | None:
    """现任 = 没有结束日期、也没有结束原因的最后一段。

    不建 `is_current` 字段：双写迟早和事实不一致。
    """
    live = [
        u
        for u in graph.unions.values()
        if u.status == 1
        and person_id in (u.partner_a_id, u.partner_b_id)
        and u.end_date is None
        and u.end_reason is None
    ]
    if not live:
        return None
    return max(live, key=lambda u: (u.union_seq, u.id)).id


def _build_relatives(db, graph: RelationGraph, viewer, person: Person) -> dict:
    pid = person.id

    father = _brief(graph, viewer, graph.father_of.get(pid), relation="father") if graph.father_of.get(pid) else None
    mother = _brief(graph, viewer, graph.mother_of.get(pid), relation="mother") if graph.mother_of.get(pid) else None

    current_uid = _current_union_id(graph, pid)
    spouses = []
    # 按序号排，否则"元配/继配"在页面上顺序随机（顺序来自数据库行序）
    for sid in sorted(
        graph.spouses_of.get(pid, []),
        key=lambda s: (
            (_union_between(graph, pid, s).union_seq if _union_between(graph, pid, s) else 99),
            s,
        ),
    ):
        u = _union_between(graph, pid, sid)
        extra = {"relation": "spouse"}
        if u is not None:
            extra["union"] = {
                "id": u.id,
                "seq": u.union_seq,
                "type": u.union_type,
                "startDate": u.start_date.isoformat() if u.start_date else None,
                "endDate": u.end_date.isoformat() if u.end_date else None,
                "endReason": u.end_reason,
                # 离异 ≠ 消失：端上照常显示这个人，只把婚姻边画成虚线
                "divorced": u.end_reason == int(UnionEndReason.DIVORCED),
                "ended": u.end_date is not None or u.end_reason is not None,
                "current": u.id == current_uid,
            }
        b = _brief(graph, viewer, sid, **extra)
        if b:
            spouses.append(b)

    children = []
    for cid in graph.children_of.get(pid, []):
        rel = graph.relation_of.get(cid, int(ParentRelation.BIOLOGICAL))
        # 生母 id 一并给端上：焦点视图要按"谁所出"分列，端上不该自己再去猜
        b = _brief(
            graph, viewer, cid,
            relation="child",
            parentRelation=rel,
            isPrimary=True,
            motherId=graph.mother_of.get(cid),
        )
        if b:
            children.append(b)

    siblings = [_brief(graph, viewer, sid, relation="sibling") for sid in graph.siblings(pid)]
    siblings = [s for s in siblings if s]

    return {
        "father": father,
        "mother": mother,
        "spouses": spouses,
        "children": children,
        "siblings": siblings,
    }


def _build_biological(db, graph: RelationGraph, viewer, person: Person) -> dict | None:
    """过继场景：树里按嗣父排，但详情页要明确标出本生父是谁。信息不丢，位置不冲突。"""
    links = db.execute(
        select(ParentChild).where(
            ParentChild.child_id == person.id, ParentChild.status == 1
        )
    ).scalars().all()

    if len(links) <= 1:
        return None

    out: dict = {"note": "本生父母与嗣父母不同"}
    for lk in links:
        union = db.get(FamilyUnion, lk.union_id)
        if union is None:
            continue
        bio_parent = _brief(graph, viewer, union.partner_a_id)
        if lk.is_primary == 1:
            out["adoptiveFather"] = bio_parent
            out["adoptiveRelation"] = lk.relation
        else:
            out["father"] = bio_parent

    return out


def _pending_brief(graph: RelationGraph, person_id: int) -> dict:
    # max_items 与 /person/{id}/missing 保持一致（3）：
    # 两处参数不同时 missingCount 虽然一样，但 items 长度会不同，
    # 迟早有人拿 items 去"判断还有没有"，然后得出两个结论。
    tasks = build_missing_tasks(graph, person_id, max_items=3)
    return {
        "missingCount": tasks["missingCount"],
        "items": [t["key"] for t in tasks["items"]],
    }


def _person_completeness(graph: RelationGraph, person_id: int) -> float:
    """完整度——**必须与 build_missing_tasks 同一套口径**。

    教训：这里曾自算 6 项（姓名/出生/父/母/配偶/生平），与待补全清单
    各算各的，出现「您这一格已经完整了 67%」的自相矛盾——
    未成年不问配偶（_check_spouse 有成年判断）、生平根本没有补录入口，
    这两项却永远压着分，且没有任何任务能把它们补上。

    现在的规则：**适用项才计分**，适用性逐条对齐 _check_* 的触发条件；
    缺口数直接取清单的 missingCount。于是
    missingCount == 0 ⟺ 完整度 100%，端上两个显示永远不会打架。
    """
    person = graph.person_of.get(person_id)
    if person is None:
        return 0.0

    tasks = build_missing_tasks(graph, person_id, max_items=999)
    holes = int(tasks.get("missingCount") or 0)

    living = person.living_status == int(LivingStatus.LIVING)
    father_id = graph.father_of.get(person_id)
    mid = graph.mother_of.get(person_id)

    applicable = 1  # 姓名（_check_self_name 永远适用）
    # 媳妇 / 女婿 / 外姓后代的父母不在本族谱上（_check_parents 有同样的守卫）。
    # 分母不同步的话，李秀英这种媳妇会永远停在 67%——缺口是 0 项，
    # 分母里却还压着"父亲"一格。
    is_inlaw = person.lineage_role in (
        int(LineageRole.SPOUSE_IN),
        int(LineageRole.SPOUSE_OUT),
        int(LineageRole.EXTERNAL_DESC),
    )
    if not is_inlaw and person.generation != 1:
        applicable += 1  # 父亲（_check_parents：第 1 世不问）
        if mid is not None or father_id is not None:
            applicable += 1  # 母亲（有母查其名，有父才问母）
    if living and (person.birth_date is None or person.birth_date.year <= 2008):
        # 配偶（_check_spouse：在世、成年、还没有婚姻记录、且没答过"未婚"）
        if not graph.spouses_of.get(person_id) and (
            person.marital_status != int(MaritalStatus.SINGLE)
        ):
            applicable += 1
    if living and graph.spouses_of.get(person_id):
        # 子女（_check_children：在世、有婚姻、还没有子女记录、且没答过"暂无子女"）
        if not graph.children_of.get(person_id) and not person.childless:
            applicable += 1
    # 外孙（_check_external：每个没记子女的女儿算一项，最多 2 项）
    applicable += sum(1 for t in tasks["items"] if t["key"] == "external")
    if living:
        applicable += 1  # 出生时间（_check_dates）
    else:
        applicable += 2  # 去世时间 + 葬地

    if applicable <= 0:
        return 1.0
    return round(max(0.0, 1.0 - holes / applicable), 2)


def _resolve_write_mode(viewer, person_ids: list[int]) -> bool:
    """决定这次写操作是「直接生效」还是「排队等房长」。返回值 = auto_merge。

    | 用户状态 | 目标在 edit_scope 内？ | 结果 |
    |---|---|---|
    | 房长及以上 | — | 直接生效 |
    | 已认领 | 是 | 直接生效（写 AUTO_MERGED 修订留痕） |
    | 已认领 | 否 | 进待审队列，**不阻塞**（用户自己那块照常可用） |
    | 未认领 | — | 拒绝（40301），走答题自证 |

    为什么"已认领"是分界线：认领已经用父母姓名证明了他在这个家庭里的位置，
    范围内的事他最有资格判断；但一个连身份都没确认的账号不该往别人家门上贴字。
    范围外排队而不是拒绝，是因为"拒了就不会再有人改"——
    家谱缺的那一块会永远空着，那才是真正的损失。
    """
    if viewer.is_admin:
        return True
    if not viewer.is_logged_in or viewer.claimed_person_id is None:
        raise no_edit_scope("请先通过答题确认您的身份")
    return all(pid in viewer.edit_scope_ids for pid in person_ids)


def _revision_out(rev: Revision) -> dict:
    auto = rev.status == int(ReviewStatus.AUTO_MERGED)
    return {
        "revisionId": rev.id,
        "status": rev.status,
        "statusText": {
            int(ReviewStatus.PENDING): "待审核",
            int(ReviewStatus.APPROVED): "已通过",
            int(ReviewStatus.REJECTED): "已驳回",
            int(ReviewStatus.AUTO_MERGED): "已直接生效",
        }.get(rev.status, "未知"),
        "autoMerged": auto,
        "diffSummary": rev.diff_summary,
        "conflictWithId": rev.conflict_with_id,
    }


# 「没有」也是一种回答。这两项答"没有"时落到哪个字段——
# 注意落的是**事实字段**而不是一个"已跳过"的标记：谱里写的是"未婚"，
# 不是"用户暂时不想填"，这两件事将来查谱时完全不一样。
_SKIP_FIELDS: dict[str, dict] = {
    "spouse": {"maritalStatus": int(MaritalStatus.SINGLE)},
    "children": {"childless": 1},
    # 外孙：答「暂不记」落的是一个决定（这一支外孙不入谱），不是一个跳过标记。
    # 不落库的话这一问永远复发，完成度永远上不去（陈启明卡在 0.8 就是这个）。
    "external": {"externalOptOut": 1},
}


def _submit_out(rev: Revision) -> dict:
    """写操作的统一回包。autoMerged 必须显式给（端上据此说"已生效"还是"等房长"）。"""
    out = _revision_out(rev)
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "revisionId": rev.id,
            "status": rev.status,
            "statusText": out["statusText"],
            "autoMerged": out["autoMerged"],
            "diffSummary": rev.diff_summary,
            "nextHint": (
                "已直接生效，家族树上马上就能看到"
                if out["autoMerged"]
                else "已进入待审队列；您自己这一块不受影响，仍可继续填写"
            ),
        },
    }


def _infer_relation(prefill: dict, person_id: int) -> dict:
    link_as = prefill.get("linkAs")
    if link_as == "child_of":
        return {"type": "child_of", "parentId": prefill.get("parentId") or person_id}
    if link_as == "spouse_of":
        return {"type": "spouse_of", "partnerId": prefill.get("partnerId") or person_id}
    if link_as == "father_of":
        return {"type": "father_of", "childId": prefill.get("childId")}

    # "配偶"这一问的 prefill 用的是 partnerAId（表示"和谁结婚"）。
    # 少了这一支就会退化成 child_of，把配偶挂成子女——
    # 这是数据里最难发现、也最难修的错，所以宁可在这里显式兜住。
    if prefill.get("partnerAId"):
        return {"type": "spouse_of", "partnerId": int(prefill["partnerAId"])}

    return {"type": "child_of", "parentId": person_id}
