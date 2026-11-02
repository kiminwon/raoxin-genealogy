"""管理端：认领审核、修订审核、看板。

这里有两个设计底线：

1. **平台不做辈分裁判。** 两人对同一字段填写不一致时，只标"待确认"并通知双方，
   是否采信由房长决定且必须填理由。宗族内部的辈分争议，产品绝对不能替他们决定。

2. **绝不提供"全部通过"。** 家谱数据一旦批量放行，错误会扩散到全族。
   首期导入期会有几百条待审，所以提供批量勾选（用户主动选择），但不提供一键全通过。
"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, resolve_context
from app.core.constants import (
    ClaimStatus,
    NotificationType,
    ReviewStatus,
    ScopeSource,
    UserRole,
    VerifyMethod,
)
from app.core.errors import BizError, ErrCode, forbidden, not_found
from app.models.family import Branch, Family
from app.models.person import Person
from app.models.system import AuditLog, Notification, Revision
from app.models.user import AppUser, Claim, EditScope, UserFamily
from app.services.mask import cn_number
from app.services.identify_service import (
    list_locked_user_ids,
    lock_status,
    reset_rate_limit,
)
from app.services.missing import count_missing_for_family
from app.services.revision_service import apply_revision
from app.services.scope_service import RelationGraph, grant_edit_scope
from app.services.tree_service import invalidate_family_cache

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_reviewer(user: AppUser) -> None:
    if user.role < int(UserRole.BRANCH_HEAD):
        raise forbidden("需要理事会成员及以上权限")


def _require_admin(user: AppUser) -> None:
    if user.role < int(UserRole.FAMILY_ADMIN):
        raise forbidden("需要族管理员权限")


# ============================================================
# 看板
# ============================================================


@router.get("/dashboard", summary="管理看板")
async def dashboard(
    user: CurrentUser,
    db: DbSession,
    familyId: int = Query(...),
) -> dict:
    _require_reviewer(user)
    family, graph, viewer = resolve_context(db, user, familyId)

    stats = count_missing_for_family(graph)
    stats["livingCount"] = sum(1 for p in graph.person_of.values() if p.living_status == 1)

    # 走 user_family：同一个账号可以属于多个族，按 app_user.family_id 数
    # 只会数到"当前正好停在这个族"的人，切去母系谱看一眼就会从这个族的人数里消失。
    claimed_users = db.execute(
        select(func.count(UserFamily.id)).where(
            UserFamily.family_id == familyId,
            UserFamily.status == 1,
            UserFamily.claimed_person_id.isnot(None),
        )
    ).scalar() or 0

    pending_revisions = db.execute(
        select(func.count(Revision.id)).where(
            Revision.family_id == familyId,
            Revision.status == int(ReviewStatus.PENDING),
        )
    ).scalar() or 0

    pending_claims = db.execute(
        select(func.count(Claim.id)).where(
            Claim.family_id == familyId,
            Claim.status == int(ClaimStatus.PENDING),
        )
    ).scalar() or 0

    conflict_count = db.execute(
        select(func.count(Revision.id)).where(
            Revision.family_id == familyId,
            Revision.conflict_with_id.isnot(None),
        )
    ).scalar() or 0

    total = stats["personCount"] or 1
    # 认领率是这个项目唯一需要盯的核心指标：它等于"有多少人真正参与进来了"
    stats["claimedUserCount"] = int(claimed_users)
    stats["claimRate"] = round(claimed_users / total, 4)
    stats["pendingRevisionCount"] = int(pending_revisions)
    stats["pendingClaimCount"] = int(pending_claims)
    stats["conflictCount"] = int(conflict_count)

    branches = db.execute(
        select(Branch).where(Branch.family_id == familyId, Branch.status == 1)
    ).scalars().all()
    branch_stats = [
        {
            "branchId": b.id,
            "name": b.name,
            "personCount": sum(1 for p in graph.person_of.values() if p.branch_id == b.id),
        }
        for b in branches
    ]

    recent = []
    for r in db.execute(
        select(Revision)
        .where(Revision.family_id == familyId, Revision.status != int(ReviewStatus.PENDING))
        .order_by(Revision.reviewed_at.desc())
        .limit(8)
    ).scalars().all():
        recent.append(
            {
                "type": "revision.reviewed",
                "summary": r.diff_summary,
                "status": r.status,
                "at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            }
        )

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "familyId": familyId,
            "familyName": family.name,
            "stats": stats,
            "branchStats": branch_stats,
            "recentActivity": recent,
        },
    }


# ============================================================
# 答题限速（解禁）
# ============================================================

# 为什么要有这两个接口：
# 限速本来是防暴力枚举的，但它会误伤——一家人围着手机替长辈试答案，
# 试到第五次就被冷却一小时，而屏幕上只写"请稍后再试"。
# 让理事会成员能查"谁被卡住了、还剩多久"并一键解开，
# 比让用户干等一小时、以为小程序坏了，代价小得多。


@router.get("/identify/locks", summary="答题限速名单")
async def list_identify_locks(
    user: CurrentUser,
    db: DbSession,
    familyId: int = Query(...),
) -> dict:
    _require_reviewer(user)
    _family, graph, _viewer = resolve_context(db, user, familyId)

    ids = list_locked_user_ids()
    items: list[dict] = []
    if ids:
        rows = db.execute(select(AppUser).where(AppUser.id.in_(ids))).scalars().all()
        now = time.time()
        for u in rows:
            person = graph.person_of.get(u.claimed_person_id or 0)
            # 只列与本族有关的人：认领了本族节点，或当前就停在这个族
            if person is None and u.family_id != familyId:
                continue
            st = lock_status(u.id, now)
            items.append(
                {
                    "userId": u.id,
                    "nickname": u.nickname,
                    "personId": person.id if person else None,
                    "personName": _display(person),
                    "generationLabel": (
                        f"第{cn_number(person.generation)}世"
                        if person is not None and person.generation
                        else None
                    ),
                    "attempts": st["attempts"],
                    "failCount": st["failCount"],
                    "reason": st["reason"],
                    "reasonText": {
                        "too_many_attempts": "一小时内的答题次数用完了",
                        "trailing_fails": "连续答错达到上限",
                    }.get(st["reason"] or "", "已限速"),
                    "retryAfterSeconds": st["retryAfterSeconds"],
                    "retryAfterText": _wait_text(st["retryAfterSeconds"]),
                    "lastAttemptAt": st["lastAttemptAt"],
                }
            )
    items.sort(key=lambda x: -x["retryAfterSeconds"])

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "total": len(items),
            "items": items,
            # 面板上要如实写这句，否则理事会以为"解禁"是个慎重的大动作
            "note": "计数存在服务内存里，重启后端即全部清零；解禁不改动任何家谱数据。",
        },
    }


class IdentifyUnlockIn(BaseModel):
    familyId: int
    userId: int
    note: str | None = None


@router.post("/identify/unlock", summary="解除答题限速")
async def unlock_identify(
    body: IdentifyUnlockIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    _require_reviewer(user)

    target = db.get(AppUser, body.userId)
    if target is None:
        raise not_found("成员")

    # 只能解本族的人：跨族解禁等于把手伸进别人的谱
    member = db.execute(
        select(UserFamily).where(
            UserFamily.user_id == body.userId,
            UserFamily.family_id == body.familyId,
            UserFamily.status == 1,
        )
    ).scalars().first()
    in_family = False
    if member is not None:
        in_family = True
    elif target.claimed_person_id:
        in_family = db.execute(
            select(Person.id).where(
                Person.id == target.claimed_person_id,
                Person.family_id == body.familyId,
            )
        ).first() is not None
    if not in_family:
        raise forbidden("该成员不属于本族")

    reset_rate_limit(body.userId)

    db.add(
        AuditLog(
            family_id=body.familyId,
            user_id=user.id,
            action="identify.unlock",
            target_table="app_user",
            target_id=body.userId,
            detail={"note": body.note, "nickname": target.nickname},
            created_at=datetime.now(),
        )
    )
    db.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {"userId": body.userId, "unlocked": True},
    }


def _wait_text(seconds: int) -> str:
    """剩余时间的人话版本。别让理事会成员自己换算 2731 秒是多久。"""
    if seconds <= 0:
        return "已可重试"
    if seconds < 60:
        return f"还需 {seconds} 秒"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"还需约 {minutes} 分钟"
    return f"还需约 {minutes // 60} 小时"


# ============================================================
# 认领审核
# ============================================================


@router.get("/claims", summary="认领申请队列")
async def list_claims(
    user: CurrentUser,
    db: DbSession,
    familyId: int = Query(...),
    status: int = Query(int(ClaimStatus.PENDING)),
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    _require_reviewer(user)

    rows = db.execute(
        select(Claim)
        .where(Claim.family_id == familyId, Claim.status == status)
        .order_by(Claim.created_at)
        .limit(limit)
    ).scalars().all()

    graph = RelationGraph.load(db, familyId)
    items = []
    for c in rows:
        person = graph.person_of.get(c.person_id)
        applicant = db.get(AppUser, c.user_id)
        answers = c.verify_answers or {}
        items.append(
            {
                "claimId": c.id,
                "personId": c.person_id,
                "personName": _display(person),
                "personGeneration": person.generation if person else None,
                "personGenerationLabel": (
                    f"第{cn_number(person.generation)}世" if person and person.generation else None
                ),
                "fatherName": _display(graph.person_of.get(graph.father_of.get(c.person_id, 0))),
                "evidence": c.evidence,
                "claimType": c.claim_type,
                # 抽查用：答题自证的申请要能一眼看出"证据硬不硬"
                "verifyMethod": c.verify_method,
                "verifyMethodText": {
                    int(VerifyMethod.ANSWER): "答题自证",
                    int(VerifyMethod.REVIEW): "人工核实",
                    int(VerifyMethod.INVITE): "邀请码",
                }.get(c.verify_method, "未知"),
                "evidenceStrength": answers.get("evidenceStrength"),
                "verifyAnswers": answers or None,
                "applicantNickname": applicant.nickname if applicant else None,
                "createdAt": c.created_at.isoformat() if c.created_at else None,
                # 已处理的申请要能回看"谁审的、为什么这么定"——
                # 撤销认领是有后果的动作，事后追问必须有据可查
                "reviewNote": c.review_note,
                "reviewerNickname": (
                    db.get(AppUser, c.reviewer_user_id).nickname
                    if c.reviewer_user_id else None
                ),
                "reviewedAt": c.reviewed_at.isoformat() if c.reviewed_at else None,
                "resultText": {
                    int(ClaimStatus.PENDING): "待审",
                    int(ClaimStatus.APPROVED): "已通过",
                    int(ClaimStatus.REJECTED): "已驳回",
                    int(ClaimStatus.WITHDRAWN): "已撤销",
                    int(ClaimStatus.CONFLICT): "有冲突",
                }.get(c.status, "未知"),
            }
        )

    return {"code": 0, "msg": "ok", "data": {"total": len(items), "items": items}}


class ClaimReviewIn(BaseModel):
    approve: bool
    reviewNote: str | None = None
    # 通过时是否把"认领人直系亲属"一起授权（默认开，这是接力修谱能转起来的前提）
    grantCloseRelatives: bool = True


@router.post("/claim/{claim_id}/review", summary="审核认领申请")
async def review_claim(
    claim_id: int,
    body: ClaimReviewIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    _require_reviewer(user)

    claim = db.get(Claim, claim_id)
    if claim is None:
        raise not_found("认领申请")
    if claim.status != int(ClaimStatus.PENDING):
        raise BizError(ErrCode.PARAM_INVALID, "该申请已处理")

    graph = RelationGraph.load(db, claim.family_id)
    applicant = db.get(AppUser, claim.user_id)

    claim.reviewer_user_id = user.id
    claim.review_note = body.reviewNote
    claim.reviewed_at = datetime.now()

    if not body.approve:
        claim.status = int(ClaimStatus.REJECTED)
        db.add(
            Notification(
                user_id=claim.user_id,
                family_id=claim.family_id,
                type=int(NotificationType.CLAIM_RESULT),
                title="认领未通过",
                body=body.reviewNote or "请补充佐证后重新提交",
                payload={"claimId": claim.id},
                created_at=datetime.now(),
            )
        )
        db.commit()
        return {"code": 0, "msg": "ok", "data": {"claimId": claim.id, "status": claim.status}}

    # ---- 通过 ----
    conflict = db.execute(
        select(Claim).where(
            Claim.person_id == claim.person_id,
            Claim.id != claim.id,
            Claim.status == int(ClaimStatus.APPROVED),
        )
    ).scalars().first()
    if conflict is not None:
        raise BizError(
            ErrCode.CLAIM_CONFLICT,
            "该节点已被他人认领，请先处理冲突",
            {"conflictClaimId": conflict.id},
        )

    claim.status = int(ClaimStatus.APPROVED)

    if applicant is not None:
        applicant.claimed_person_id = claim.person_id
        if applicant.family_id is None:
            applicant.family_id = claim.family_id

        scope_ids = {claim.person_id}
        if body.grantCloseRelatives:
            # 认领通过 = 你能改自己 + 直系上下两代。
            # 这是"每个人只填自己那一小块"的权限落地。
            scope_ids |= graph.close_relatives(claim.person_id, up=2, down=2)

        grant_edit_scope(
            db,
            user_id=applicant.id,
            family_id=claim.family_id,
            person_ids=scope_ids,
            source=ScopeSource.CLAIM_SELF,
            granted_by=user.id,
        )

    db.add(
        Notification(
            user_id=claim.user_id,
            family_id=claim.family_id,
            type=int(NotificationType.CLAIM_RESULT),
            title="认领已通过",
            body="现在可以补全您和直系亲属的信息了",
            payload={"claimId": claim.id, "personId": claim.person_id},
            created_at=datetime.now(),
        )
    )
    db.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "claimId": claim.id,
            "status": claim.status,
            "grantedNodeCount": len(scope_ids) if applicant else 0,
        },
    }


class ClaimRevertIn(BaseModel):
    reviewNote: str = Field(..., min_length=2, description="撤销理由，必填")
    revokeScope: bool = Field(True, description="是否同时收回编辑授权")


@router.post("/claim/{claim_id}/revert", summary="撤销认领")
async def revert_claim(
    claim_id: int,
    body: ClaimRevertIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """房长撤销一次认领。

    为什么必须有这个接口：答题自证把审核从「每单必审」降成了「抽查」。
    抽查要成立，前提是**发现错了能回滚**——否则一次误判就要靠手工改库。

    注意作用范围：只收回这次认领产生的授权（source=CLAIM_SELF），
    不动通过邀请码拿到的授权，也不删他在树上的提交记录。
    撤销是"这个人不能代表这个节点了"，不是"把他做过的事抹掉"——
    家谱里的每次改动都要能追溯到人。
    """
    _require_reviewer(user)

    claim = db.get(Claim, claim_id)
    if claim is None:
        raise not_found("认领申请")
    if claim.status != int(ClaimStatus.APPROVED):
        raise BizError(ErrCode.PARAM_INVALID, "只有已通过的认领才能撤销")

    applicant = db.get(AppUser, claim.user_id)

    revoked = 0
    if body.revokeScope:
        rows = db.execute(
            select(EditScope).where(
                EditScope.user_id == claim.user_id,
                EditScope.family_id == claim.family_id,
                EditScope.source == int(ScopeSource.CLAIM_SELF),
                EditScope.status == 1,
            )
        ).scalars().all()
        for row in rows:
            row.status = 0
        revoked = len(rows)

    if applicant is not None and applicant.claimed_person_id == claim.person_id:
        applicant.claimed_person_id = None

    claim.status = int(ClaimStatus.WITHDRAWN)
    claim.reviewer_user_id = user.id
    claim.review_note = f"[撤销认领] {body.reviewNote}"
    claim.reviewed_at = datetime.now()

    db.add(
        Notification(
            user_id=claim.user_id,
            family_id=claim.family_id,
            type=int(NotificationType.CLAIM_RESULT),
            title="认领已被撤销",
            body=f"房长说明：{body.reviewNote}",
            payload={"claimId": claim.id, "personId": claim.person_id},
            created_at=datetime.now(),
        )
    )
    db.add(
        AuditLog(
            family_id=claim.family_id,
            user_id=user.id,
            action="claim.revert",
            target_table="claim",
            target_id=claim.id,
            detail={
                "reason": body.reviewNote,
                "personId": claim.person_id,
                "applicantUserId": claim.user_id,
                "revokedScopeRows": revoked,
            },
            created_at=datetime.now(),
        )
    )
    db.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "claimId": claim.id,
            "status": claim.status,
            "revokedScopeRows": revoked,
            "unboundUserId": claim.user_id,
        },
    }


# ============================================================
# 修订审核
# ============================================================


@router.get("/revisions", summary="修订待审队列")
async def list_revisions(
    user: CurrentUser,
    db: DbSession,
    familyId: int = Query(...),
    scope: str = Query("review", description="review 待我审 / conflicts 仅冲突 / all 全部"),
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    _require_reviewer(user)

    stmt = select(Revision).where(Revision.family_id == familyId)
    if scope == "review":
        stmt = stmt.where(Revision.status == int(ReviewStatus.PENDING))
    elif scope == "conflicts":
        stmt = stmt.where(Revision.conflict_with_id.isnot(None))
    stmt = stmt.order_by(Revision.created_at).limit(limit)

    rows = db.execute(stmt).scalars().all()
    graph = RelationGraph.load(db, familyId)

    items = []
    for r in rows:
        submitter = graph.person_of.get(r.submitter_person_id) if r.submitter_person_id else None
        target = graph.person_of.get(r.target_id) if r.target_id else None
        items.append(
            {
                "id": r.id,
                "targetTable": r.target_table,
                "targetId": r.target_id,
                "targetName": _display(target),
                "op": r.op,
                "diffSummary": r.diff_summary,
                "diffDetail": _diff_detail(r),
                "submitterPersonId": r.submitter_person_id,
                "submitterName": _display(submitter),
                # 提交人是被改对象的什么人 —— 房长看一眼"之子"基本就能判断可不可信
                "submitterRelation": _relation_label(graph, r.submitter_person_id, r.target_id),
                "conflictWithId": r.conflict_with_id,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
        )

    return {"code": 0, "msg": "ok", "data": {"total": len(items), "items": items}}


class RevisionReviewIn(BaseModel):
    reviewNote: str | None = None


@router.post("/revision/{revision_id}/approve", summary="通过修订")
async def approve_revision(
    revision_id: int,
    body: RevisionReviewIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    _require_reviewer(user)

    rev = db.get(Revision, revision_id)
    if rev is None:
        raise not_found("修订")
    if rev.status != int(ReviewStatus.PENDING):
        raise BizError(ErrCode.PARAM_INVALID, "该修订已处理")

    result = apply_revision(db, rev)

    rev.status = int(ReviewStatus.APPROVED)
    rev.reviewer_user_id = user.id
    rev.review_note = body.reviewNote
    rev.reviewed_at = datetime.now()

    family = db.get(Family, rev.family_id)
    if family is not None:
        db.flush()
        invalidate_family_cache(db, family)

    db.add(
        Notification(
            user_id=rev.submitter_user_id,
            family_id=rev.family_id,
            type=int(NotificationType.REVISION_REVIEWED),
            title="您提交的修改已通过",
            body=rev.diff_summary,
            payload={"revisionId": rev.id},
            created_at=datetime.now(),
        )
    )
    db.commit()

    return {"code": 0, "msg": "ok", "data": {"revisionId": rev.id, "status": rev.status, **result}}


@router.post("/revision/{revision_id}/reject", summary="驳回修订")
async def reject_revision(
    revision_id: int,
    body: RevisionReviewIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    _require_reviewer(user)

    rev = db.get(Revision, revision_id)
    if rev is None:
        raise not_found("修订")

    rev.status = int(ReviewStatus.REJECTED)
    rev.reviewer_user_id = user.id
    rev.review_note = body.reviewNote
    rev.reviewed_at = datetime.now()

    db.add(
        Notification(
            user_id=rev.submitter_user_id,
            family_id=rev.family_id,
            type=int(NotificationType.REVISION_REVIEWED),
            title="您提交的修改未通过",
            body=body.reviewNote or "请与家中长辈核实后重新提交",
            payload={"revisionId": rev.id},
            created_at=datetime.now(),
        )
    )
    db.commit()
    return {"code": 0, "msg": "ok", "data": {"revisionId": rev.id, "status": rev.status}}


class BatchApproveIn(BaseModel):
    revisionIds: list[int] = Field(..., max_length=50)


@router.post("/revision/batch-approve", summary="批量通过（需手动勾选）")
async def batch_approve(
    body: BatchApproveIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """首期导入期会有几百条待审，单条点会崩溃。
    但只提供"勾选多条"，绝不提供"全部通过"——错误一旦批量放行会扩散到全族。
    """
    _require_reviewer(user)

    ok_count = 0
    failed: list[dict] = []
    family_id: int | None = None

    for rid in body.revisionIds:
        rev = db.get(Revision, rid)
        if rev is None or rev.status != int(ReviewStatus.PENDING):
            failed.append({"revisionId": rid, "reason": "已处理或不存在"})
            continue
        try:
            apply_revision(db, rev)
            rev.status = int(ReviewStatus.APPROVED)
            rev.reviewer_user_id = user.id
            rev.reviewed_at = datetime.now()
            family_id = rev.family_id
            ok_count += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            failed.append({"revisionId": rid, "reason": str(exc)})

    if family_id:
        family = db.get(Family, family_id)
        if family is not None:
            db.flush()
            invalidate_family_cache(db, family)

    db.commit()
    return {
        "code": 0,
        "msg": "ok",
        "data": {"approved": ok_count, "failed": failed},
    }


class ResolveConflictIn(BaseModel):
    keepRevisionId: int
    rejectRevisionId: int
    note: str = Field(..., min_length=2, description="采信理由，必填")


@router.post("/revision/resolve-conflict", summary="解决填写冲突")
async def resolve_conflict(
    body: ResolveConflictIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """两人对同一字段填写不一致时由房长裁决。

    note 必填 —— 家谱中的记载会传给后代，必须留下采信理由。
    平台不做裁判，只提供工具和留痕。
    """
    _require_reviewer(user)

    keep = db.get(Revision, body.keepRevisionId)
    reject = db.get(Revision, body.rejectRevisionId)
    if keep is None or reject is None:
        raise not_found("修订")

    apply_revision(db, keep)
    keep.status = int(ReviewStatus.APPROVED)
    keep.reviewer_user_id = user.id
    keep.review_note = f"[冲突裁决] {body.note}"
    keep.reviewed_at = datetime.now()

    reject.status = int(ReviewStatus.REJECTED)
    reject.reviewer_user_id = user.id
    reject.review_note = f"[冲突裁决] 未采信，理由：{body.note}"
    reject.reviewed_at = datetime.now()

    family = db.get(Family, keep.family_id)
    if family is not None:
        db.flush()
        invalidate_family_cache(db, family)

    for uid, title in (
        (keep.submitter_user_id, "您填写的版本已采信"),
        (reject.submitter_user_id, "您填写的版本未被采信"),
    ):
        db.add(
            Notification(
                user_id=uid,
                family_id=keep.family_id,
                type=int(NotificationType.FIELD_CONFLICT),
                title=title,
                body=f"房长说明：{body.note}",
                payload={"revisionId": keep.id},
                created_at=datetime.now(),
            )
        )

    db.commit()
    return {
        "code": 0,
        "msg": "ok",
        "data": {"kept": keep.id, "rejected": reject.id, "note": body.note},
    }


# ============================================================
# 工具
# ============================================================


class GrantScopeIn(BaseModel):
    userId: int
    personIds: list[int]
    expiresInDays: int | None = None


@router.post("/grant-scope", summary="给用户授权指定节点（房长）")
async def grant_scope(body: GrantScopeIn, user: CurrentUser, db: DbSession) -> dict:
    _require_reviewer(user)

    target = db.get(AppUser, body.userId)
    if target is None or target.family_id is None:
        raise not_found("用户")

    from datetime import timedelta

    expires = (
        datetime.now() + timedelta(days=body.expiresInDays)
        if body.expiresInDays
        else None
    )
    added = grant_edit_scope(
        db,
        user_id=target.id,
        family_id=target.family_id,
        person_ids=body.personIds,
        source=ScopeSource.BRANCH_HEAD,
        granted_by=user.id,
        expires_at=expires,
    )
    db.commit()
    return {"code": 0, "msg": "ok", "data": {"added": added}}


class RoleSetIn(BaseModel):
    userId: int
    role: int = Field(..., description="1族人 2理事会成员（含房长） 3族管理员")


@router.post("/role", summary="授予 / 调整成员角色（族管理员）")
async def set_role(body: RoleSetIn, user: CurrentUser, db: DbSession) -> dict:
    """角色不是认领来的，是指定的——因为真正的房长年纪大、不操作智能机。

    实际被授权操作后台的是理事会里会用手机的年轻人。三条防线：
    1. 只有族管理员及以上能调（房长不能互相提权）；
    2. 不能授予比自己高的角色（族管理员也造不出平台运营）；
    3. 不能降低自己的角色（防止手滑把自己降成族人，全族锁死在后台外面）。
    双写规则与 family_member 同源：user_family.role 是真相，
    目标用户若正停在本族，app_user.role 快照同步回填。
    """
    _require_admin(user)

    if body.role not in (int(UserRole.NORMAL), int(UserRole.BRANCH_HEAD), int(UserRole.FAMILY_ADMIN)):
        raise BizError(ErrCode.PARAM_INVALID, "角色取值：1族人 2理事会成员 3族管理员")
    if body.userId == user.id and body.role < int(user.role):
        raise forbidden("不能降低自己的角色")

    target = db.get(AppUser, body.userId)
    if target is None:
        raise not_found("用户")

    fid = user.family_id
    if fid is None:
        raise forbidden("您尚未加入任何宗族")

    m = db.execute(
        select(UserFamily).where(
            UserFamily.user_id == body.userId,
            UserFamily.family_id == fid,
        )
    ).scalar_one_or_none()
    if m is None or m.status != 1:
        raise not_found("对方还不是本族成员，无法授予角色")

    if body.role > int(user.role):
        raise forbidden("不能授予比自己更高的角色")

    old = int(m.role)
    m.role = body.role
    # 快照回填：目标用户当前就停在这个族时才写 app_user.role，
    # 否则只动真相表，等他下次 activate() 自然对齐。
    if target.family_id == fid:
        target.role = body.role
    db.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {"userId": target.id, "role": int(body.role), "oldRole": old},
    }


@router.post("/tree/rebuild", summary="强制重算布局缓存")
async def rebuild_tree(
    user: CurrentUser,
    db: DbSession,
    familyId: int = Query(...),
) -> dict:
    _require_reviewer(user)
    family = db.get(Family, familyId)
    if family is None:
        raise not_found("宗族")
    invalidate_family_cache(db, family)
    return {"code": 0, "msg": "ok", "data": {"familyId": familyId, "dataRev": family.data_rev}}


def _display(person: Person | None) -> str | None:
    if person is None:
        return None
    if person.name:
        return person.name
    if person.surname:
        return f"{person.surname}氏"
    return "配某氏"


def _relation_label(graph: RelationGraph, submitter_id: int | None, target_id: int | None) -> str | None:
    if not submitter_id or not target_id:
        return None
    if submitter_id == target_id:
        return "本人"
    if graph.father_of.get(submitter_id) == target_id:
        return "之子/女"
    if graph.father_of.get(target_id) == submitter_id:
        return "之父/母"
    if target_id in graph.spouses_of.get(submitter_id, []):
        return "配偶"
    if graph.father_of.get(submitter_id) == graph.father_of.get(target_id):
        return "兄弟姐妹"
    return "族人"


def _diff_detail(rev: Revision) -> list[dict]:
    before = rev.before_snapshot or {}
    after = rev.payload or {}
    if rev.op == 1 and isinstance(after.get("fields"), dict):
        after = after["fields"]
        before = {}

    from app.services.revision_service import FIELD_LABEL

    out = []
    for key, new in after.items():
        out.append(
            {
                "field": key,
                "label": FIELD_LABEL.get(key, key),
                "before": before.get(key),
                "after": new,
            }
        )
    return out
