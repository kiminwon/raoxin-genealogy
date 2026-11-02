"""修订（待审队列）服务。

## 审核粒度已从「每条数据」改成「一个范围」

原设计是全部先审后发：所有写操作先落 revision 表，管理员通过后才写进业务表。

改动的理由（见《家谱小程序-身份认领改造方案》§4）：
    房长不审认领 → 认领变容易 → 有编辑权的人变多 → 队列变长
    → 而审核还挂在房长身上 → 房长不会用 → 队列永不消耗 → 产品还是死的

所以现在：
  - `edit_scope` **范围内** → 自动落库，仍写 revision 记录（`status=AUTO_MERGED`）
  - `edit_scope` **范围外** → 仍排队等房长，但不再 403，用户能继续用自己的那块
  - 冲突 / 申诉 / 撤销 → 房长裁（低频，打个电话就能解决）

为什么范围免审是合理的：认领已经用「父母姓名」证明了你在**这个家庭里的位置**，
那么在这个范围内，「你妈叫什么」这件事你比任何房长都更有资格判断。
房长的角色因此从守门人变成仲裁者：从"每单必审"变成"有事才出场"。

先审后发的原始理由仍然成立、仍然被保留：家谱错误会被后代当作"祖上记载"
沿用几十年，试错成本极高。所以自动合并**同样写 revision 记录**——
留痕不是为了审批，是为了可回滚、可追责。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.constants import (
    DatePrecision,
    LineageRole,
    LivingStatus,
    ParentRelation,
    RevisionOp,
    ReviewStatus,
    UnionType,
)
from app.core.errors import BizError, ErrCode, not_found
from app.models.person import FamilyUnion, ParentChild, Person
from app.models.system import Revision
from app.services.mask import Viewer

log = logging.getLogger("raoxin.revision")


# 前端 camelCase -> 数据库 snake_case
PERSON_FIELD_MAP: dict[str, str] = {
    "name": "name",
    "surname": "surname",
    "namePinyin": "name_pinyin",
    "gender": "gender",
    "generation": "generation",
    "generationName": "generation_name",
    "courtesyName": "courtesy_name",
    "artName": "art_name",
    "rankInFamily": "rank_in_family",
    "birthDate": "birth_date",
    "birthDatePrecision": "birth_date_precision",
    "deathDate": "death_date",
    "deathDatePrecision": "death_date_precision",
    "livingStatus": "living_status",
    "maritalStatus": "marital_status",
    "childless": "childless",
    "externalOptOut": "external_opt_out",
    "burialPlace": "burial_place",
    "burialNote": "burial_note",
    "education": "education",
    "occupation": "occupation",
    "biography": "biography",
    "nativePlace": "native_place",
    "privacyLevel": "privacy_level",
    "lineageRole": "lineage_role",
    "branchId": "branch_id",
    "altFamilyId": "alt_family_id",
}

FIELD_LABEL: dict[str, str] = {
    "name": "姓名",
    "surname": "姓氏",
    "gender": "性别",
    "generation": "世次",
    "generationName": "字辈用字",
    "courtesyName": "字",
    "artName": "号",
    "rankInFamily": "排行",
    "birthDate": "出生日期",
    "birthDatePrecision": "出生日期精度",
    "deathDate": "去世日期",
    "deathDatePrecision": "去世日期精度",
    "livingStatus": "在世状态",
    "maritalStatus": "婚姻状态",
    "childless": "暂无子女",
    "externalOptOut": "外孙不入谱",
    "burialPlace": "葬地",
    "burialNote": "墓葬说明",
    "education": "学历/功名",
    "occupation": "职业",
    "biography": "生平",
    "nativePlace": "籍贯",
    "privacyLevel": "隐私级别",
    "lineageRole": "谱系角色",
}

DATE_FIELDS = {"birthDate", "deathDate"}

# 这些字段改起来敏感，需要更高权限（V1 先只做标记，不做拦截）
SENSITIVE_FIELDS = {"lineageRole", "generation", "isLineageCarrier"}


# ============================================================
# 提交
# ============================================================


def submit_update(
    db: Session,
    *,
    viewer: Viewer,
    family_id: int,
    person: Person,
    fields: dict[str, Any],
    submit_note: str | None = None,
    auto_merge: bool = False,
) -> Revision:
    """提交人物修改。

    auto_merge=True 时（落在自己的 edit_scope 内）直接落库，
    修订记录仍写下来，状态标 AUTO_MERGED —— 留痕是为了可回滚、可追责。
    落库结果挂在返回值的 `applied_result` 上。
    """
    clean: dict[str, Any] = {}
    before: dict[str, Any] = {}

    for key, value in fields.items():
        if key not in PERSON_FIELD_MAP:
            continue
        col = PERSON_FIELD_MAP[key]
        old = getattr(person, col, None)
        new = _coerce(key, value)
        if _same(old, new):
            continue
        clean[key] = _jsonable(new)
        before[key] = _jsonable(old)

    if not clean:
        raise BizError(ErrCode.PARAM_INVALID, "没有需要提交的变更")

    # 同字段已有他人待审修订时，标记冲突，交给房长处理（平台不做裁判）
    conflict_id = _find_conflict(db, person.id, set(clean.keys()))

    rev = Revision(
        family_id=family_id,
        target_table="person",
        target_id=person.id,
        op=int(RevisionOp.UPDATE),
        payload=clean,
        before_snapshot=before,
        diff_summary=build_diff_summary(person.name or "该成员", before, clean),
        submitter_user_id=viewer.user_id or 0,
        submitter_person_id=viewer.claimed_person_id,
        submit_note=submit_note,
        status=int(ReviewStatus.PENDING),
        conflict_with_id=conflict_id,
        created_at=datetime.now(),
    )
    db.add(rev)
    db.flush()

    if conflict_id is not None:
        other = db.get(Revision, conflict_id)
        if other is not None and other.conflict_with_id is None:
            other.conflict_with_id = rev.id

    db.commit()
    db.refresh(rev)

    if auto_merge:
        # 有冲突时不自动合并：两个人的填写都还没定论，让房长裁。
        # 自动放行一个"和别人打架"的版本，等于用先到先得替平台做了裁判。
        if conflict_id is None:
            apply_and_mark(db, rev, actor_user_id=viewer.user_id, family_id=family_id)
        else:
            log.info("修订 %s 命中冲突 %s，改为排队等房长", rev.id, conflict_id)

    return rev


def submit_create(
    db: Session,
    *,
    viewer: Viewer,
    family_id: int,
    fields: dict[str, Any],
    relation: dict[str, Any] | None = None,
    submit_note: str | None = None,
    auto_merge: bool = False,
) -> Revision:
    """提交新增人物。relation 描述它要挂到哪里（child_of / spouse_of / father_of）。"""
    clean = {
        k: _jsonable(_coerce(k, v))
        for k, v in fields.items()
        if k in PERSON_FIELD_MAP and v is not None
    }
    if not clean.get("name") and not clean.get("surname"):
        raise BizError(ErrCode.PARAM_INVALID, "至少需要填写姓名或姓氏")

    payload: dict[str, Any] = {"fields": clean}
    if relation:
        payload["relation"] = relation

    rev = Revision(
        family_id=family_id,
        target_table="person",
        target_id=None,
        op=int(RevisionOp.CREATE),
        payload=payload,
        diff_summary=f"新增成员：{clean.get('name') or (clean.get('surname', '') + '氏')}",
        submitter_user_id=viewer.user_id or 0,
        submitter_person_id=viewer.claimed_person_id,
        submit_note=submit_note,
        status=int(ReviewStatus.PENDING),
        created_at=datetime.now(),
    )
    db.add(rev)
    db.commit()
    db.refresh(rev)

    if auto_merge:
        apply_and_mark(db, rev, actor_user_id=viewer.user_id, family_id=family_id)

    return rev


def apply_and_mark(
    db: Session,
    rev: Revision,
    *,
    actor_user_id: int | None,
    family_id: int,
) -> dict[str, Any]:
    """落库 + 标 AUTO_MERGED + 失效布局缓存。返回落库摘要。

    落库结果同时挂在 `rev.applied_result` 上——调用方需要拿到新节点 id，
    但 JSON 里存的那份 payload 不含它（那是"提交的内容"，不是"落库的结果"）。
    """
    result = apply_revision(db, rev)
    rev.status = int(ReviewStatus.AUTO_MERGED)
    rev.reviewer_user_id = actor_user_id
    rev.reviewed_at = datetime.now()
    rev.applied_result = {**result, "autoMerged": True}   # type: ignore[attr-defined]

    _invalidate_cache(db, family_id)
    db.commit()
    db.refresh(rev)
    return result


def _invalidate_cache(db: Session, family_id: int) -> None:
    """布局缓存必须失效，否则树还是旧的——而树是主屏，用户一眼就看出不对。"""
    from app.models.family import Family
    from app.services.tree_service import invalidate_family_cache

    family = db.get(Family, family_id)
    if family is not None:
        db.flush()
        invalidate_family_cache(db, family)


# ============================================================
# 审核通过后落库
# ============================================================


def apply_revision(db: Session, rev: Revision) -> dict[str, Any]:
    """把修订应用到业务表。返回操作摘要。"""
    if rev.target_table != "person":
        raise BizError(ErrCode.PARAM_INVALID, f"暂不支持的 target_table: {rev.target_table}")

    if rev.op == int(RevisionOp.UPDATE):
        return _apply_update(db, rev)
    if rev.op == int(RevisionOp.CREATE):
        return _apply_create(db, rev)

    raise BizError(ErrCode.PARAM_INVALID, "暂不支持该操作类型")


def _apply_update(db: Session, rev: Revision) -> dict[str, Any]:
    person = db.get(Person, rev.target_id) if rev.target_id else None
    if person is None:
        raise not_found("待修改的人物")

    payload: dict[str, Any] = rev.payload or {}
    changed: dict[str, Any] = {}

    for key, value in payload.items():
        col = PERSON_FIELD_MAP.get(key)
        if col is None:
            continue
        setattr(person, col, _coerce(key, value))
        changed[key] = value

    # 填了字辈用字就重算一次校验结果（仍然只是提示，不回滚数据）
    if "generationName" in payload:
        person.generation_hint = 1

    db.flush()
    return {"action": "update", "personId": person.id, "changed": changed}


def _apply_create(db: Session, rev: Revision) -> dict[str, Any]:
    payload: dict[str, Any] = rev.payload or {}
    fields: dict[str, Any] = payload.get("fields", {})
    relation: dict[str, Any] = payload.get("relation", {}) or {}

    person = Person(
        family_id=rev.family_id,
        status=1,
        lineage_role=int(fields.get("lineageRole") or LineageRole.MALE),
        source_type=2,           # 族人填报
        privacy_level=int(fields.get("privacyLevel") or 2),
        # 族人现场添的人默认**在世**（2026-10-29 实报：给陈国栋添陈老三，
        # 只填了名字，卡片却打「故」——模型的 living_status 默认值是 2，
        # 一个"没填"的人被静默说成已故，这是最伤人的那种错：
        # 人还活着，谱上先给他画了终点）。只有明确填了才按填的来。
        living_status=int(fields.get("livingStatus") or LivingStatus.LIVING),
        created_by=rev.submitter_user_id,
    )
    for key, value in fields.items():
        col = PERSON_FIELD_MAP.get(key)
        if col is None:
            continue
        setattr(person, col, _coerce(key, value))

    if not person.surname and person.name:
        person.surname = person.name[0]

    db.add(person)
    db.flush()

    linked = _link_relation(db, person, relation, rev.family_id)

    return {"action": "create", "personId": person.id, "linked": linked}


def _link_relation(
    db: Session,
    person: Person,
    relation: dict[str, Any],
    family_id: int,
) -> dict[str, Any] | None:
    """把新建的人挂进家族树。

    三种挂法：
      child_of   -> 作为某人的子女：找到（或创建）该人的婚姻单元，建 parent_child
      spouse_of  -> 作为某人的配偶：补齐该婚姻单元的 partner_b
      father_of  -> 作为某人的父亲：补齐对方婚姻单元的 partner_a
    """
    rel_type = relation.get("type")
    if not rel_type:
        return None

    if rel_type == "child_of":
        union: FamilyUnion | None = None
        # 答题自证走这条：答"我母亲是X"时，孩子必须挂到**母亲所在的那个婚姻单元**，
        # 否则同一对父母的孩子会被拆到两个 union 里，兄妹在树上就不在一支上了。
        union_id = relation.get("unionId")
        if union_id:
            union = db.get(FamilyUnion, int(union_id))

        if union is None:
            parent_id = relation.get("parentId")
            if not parent_id:
                return None
            union = _ensure_union_for(db, int(parent_id), family_id)

        db.add(
            ParentChild(
                family_id=family_id,
                union_id=union.id,
                child_id=person.id,
                relation=int(relation.get("relation") or ParentRelation.BIOLOGICAL),
                is_primary=1,
                is_biological=1,
                seq=int(relation.get("seq") or 0),
                status=1,
            )
        )
        db.flush()
        return {"type": "child_of", "parentId": union.partner_a_id, "unionId": union.id}

    if rel_type == "spouse_of":
        partner_id = relation.get("partnerId")
        union_id = relation.get("unionId")
        if union_id:
            union = db.get(FamilyUnion, int(union_id))
            if union is not None and union.partner_b_id is None:
                union.partner_b_id = person.id
                db.flush()
                return {"type": "spouse_of", "unionId": union.id}
        if not partner_id:
            return None
        union = FamilyUnion(
            family_id=family_id,
            partner_a_id=int(partner_id),
            partner_b_id=person.id,
            union_type=int(relation.get("unionType") or UnionType.FIRST_MARRIAGE),
            # 序号必须落库：旧实现一律写 1，"元配/继配/第几任"当场就丢了。
            # 端上要按它排「元配—继配—侧室」，房长也要靠它判断先后。
            union_seq=int(
                relation.get("unionSeq")
                or _next_union_seq(db, int(partner_id), family_id)
            ),
            start_date=_to_date(relation.get("startDate")),
            end_date=_to_date(relation.get("endDate")),
            # 离异只记原因，**不软删**：软删等于让子女在自己那一屏看不到母亲。
            end_reason=(
                int(relation["endReason"]) if relation.get("endReason") else None
            ),
            status=1,
        )
        db.add(union)
        db.flush()
        return {
            "type": "spouse_of",
            "partnerId": partner_id,
            "unionId": union.id,
            "unionSeq": union.union_seq,
        }

    # ---------- 作为某人的父/母 ----------
    #
    # 2026-10-21 补。此前 `_infer_relation` 会产出 {"type": "father_of"}（老谱里
    # "父名待补" 的补全任务就靠它），但这里没有对应分支 —— 于是人建出来了、
    # 关系一条没挂，界面上父子断开，而且**没有任何报错**。这类"静默孤儿"
    # 是家谱数据里最难发现的错：少了谁不会有人知道，只有对账时才发现对不上。
    #
    # 挂法：找到孩子所在的婚姻单元，把空着的那一位补上。
    #   father_of -> partner_a（男）
    #   mother_of -> partner_b（女）
    # 那一位已经有人时**不覆盖**，返回 linked=False —— 让调用方能看见实情。
    if rel_type in ("father_of", "mother_of"):
        child_id = relation.get("childId")
        if not child_id:
            return None

        link = db.execute(
            select(ParentChild)
            .where(ParentChild.child_id == int(child_id), ParentChild.status == 1)
            .order_by(ParentChild.is_primary.desc(), ParentChild.id)
        ).scalars().first()

        union = db.get(FamilyUnion, int(link.union_id)) if link else None

        # 没有婚姻单元时，只有"补父"能独立成立：partner_a 是承袭方（NOT NULL），
        # 新建时直接把新父亲放进去。补母则必须先有承袭方，否则她会被当成
        # 承袭方（father_of 会指到她身上），界面上"父亲"一栏就变成了母亲。
        if union is None:
            if rel_type == "mother_of":
                return {
                    "type": "mother_of",
                    "childId": int(child_id),
                    "linked": False,
                    "reason": "no_carrier",
                }
            union = FamilyUnion(
                family_id=family_id,
                partner_a_id=person.id,
                partner_b_id=None,
                union_type=int(UnionType.FIRST_MARRIAGE),
                union_seq=1,
                status=1,
            )
            db.add(union)
            db.flush()
            db.add(
                ParentChild(
                    family_id=family_id,
                    union_id=union.id,
                    child_id=int(child_id),
                    relation=int(relation.get("relation") or ParentRelation.BIOLOGICAL),
                    is_primary=1,
                    is_biological=1,
                    seq=0,
                    status=1,
                )
            )
            db.flush()
            return {"type": rel_type, "childId": int(child_id), "unionId": union.id}

        slot = "partner_a_id" if rel_type == "father_of" else "partner_b_id"
        current = getattr(union, slot)
        if current is None:
            setattr(union, slot, person.id)
            db.flush()
            return {"type": rel_type, "childId": int(child_id), "unionId": union.id}

        if int(current) == person.id:
            return {"type": rel_type, "childId": int(child_id), "unionId": union.id}

        return {
            "type": rel_type,
            "childId": int(child_id),
            "linked": False,
            "reason": "slot_occupied",
            "occupiedBy": int(current),
        }

    return None


def _next_union_seq(db: Session, person_id: int, family_id: int) -> int:
    """这个人已有的婚姻段数 + 1。

    既数他当 partner_a 的，也数她当 partner_b 的——女性再婚时，
    她的第二段在库里是"别人的 partner_a"，只看 partner_a 会永远返回 1。
    """
    n = db.execute(
        select(func.count())
        .select_from(FamilyUnion)
        .where(
            FamilyUnion.family_id == family_id,
            FamilyUnion.status == 1,
            or_(
                FamilyUnion.partner_a_id == person_id,
                FamilyUnion.partner_b_id == person_id,
            ),
        )
    ).scalar()
    return int(n or 0) + 1


def _ensure_union_for(db: Session, person_id: int, family_id: int) -> FamilyUnion:
    """取该人所在的婚姻单元；**多段婚姻时不猜**。

    旧实现按 union_seq 取第一条，而序号又一律写 1，于是永远取 id 最小的
    那一段——等于把二房的孩子判给元配。后果不是显示问题：孩子的
    `mother_of` 指到元配，认领自证（"我母亲是张氏"）反查不到，
    主通道直接失败，而且没有任何报错。

    现在的规则：
      1. 只有一段 → 就是它。
      2. 有多段、且存在 partner_b 为空的那段（配不详 / 母亲待补）→ 用它。
      3. 有多段且都完整 → 新建一段 partner_b 为空的，把"这位母亲是谁"
         留给后续补录（`mother_of` 会回填这段的 partner_b）。
         **宁可先空着，也不要把孩子判给某一位夫人。**
      4. 她只当过 partner_b（媳妇）→ 用她最近的那段，否则新单元会
         把她当 partner_a，孩子的"父亲"栏就变成了母亲。
    """
    unions = db.execute(
        select(FamilyUnion)
        .where(
            FamilyUnion.family_id == family_id,
            FamilyUnion.status == 1,
            or_(
                FamilyUnion.partner_a_id == person_id,
                FamilyUnion.partner_b_id == person_id,
            ),
        )
        .order_by(FamilyUnion.union_seq, FamilyUnion.id)
    ).scalars().all()

    if len(unions) == 1:
        return unions[0]

    if unions:
        for u in unions:
            if u.partner_b_id is None and u.partner_a_id == person_id:
                return u

        as_a = [u for u in unions if u.partner_a_id == person_id]
        if not as_a:
            # 她只以配偶身份出现过：孩子挂她最近的一段婚姻
            return unions[-1]

    union = FamilyUnion(
        family_id=family_id,
        partner_a_id=person_id,
        partner_b_id=None,
        union_type=int(UnionType.UNKNOWN),
        union_seq=_next_union_seq(db, person_id, family_id),
        status=1,
    )
    db.add(union)
    db.flush()
    return union


# ============================================================
# 差异摘要
# ============================================================


def build_diff_summary(name: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    """人话摘要，例如：修改 陈国栋 的 出生日期：1965年 → 1965年3月12日。"""
    parts: list[str] = []
    for key, new in after.items():
        label = FIELD_LABEL.get(key, key)
        old = before.get(key)
        parts.append(f"{label}：{_human(old)} → {_human(new)}")
    head = f"修改 {name} 的 " if name else "修改 "
    return (head + "；".join(parts))[:500]


def _human(value: Any) -> str:
    if value is None or value == "":
        return "空"
    return str(value)


# ============================================================
# 辅助
# ============================================================


def _find_conflict(db: Session, person_id: int, keys: set[str]) -> int | None:
    """找同一目标、同一字段的其他待审修订。"""
    rows = db.execute(
        select(Revision).where(
            Revision.target_table == "person",
            Revision.target_id == person_id,
            Revision.status == int(ReviewStatus.PENDING),
        )
    ).scalars().all()

    for r in rows:
        payload = r.payload or {}
        if keys & set(payload.keys()):
            return r.id
    return None


def _coerce(key: str, value: Any) -> Any:
    if key in DATE_FIELDS:
        return _to_date(value)
    if key in {
        "gender", "generation", "birthDatePrecision", "deathDatePrecision",
        "livingStatus", "privacyLevel", "lineageRole", "branchId",
    }:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return value


def _to_date(value: Any) -> date | None:
    """支持 '1965' / '1965-03' / '1965-03-12' 三种粒度，精度另行记录。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        if len(s) == 4:
            return date(int(s), 1, 1)
        if len(s) == 7:
            return date(int(s[:4]), int(s[5:7]), 1)
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, date):
        a = a.isoformat()
    return a == b
