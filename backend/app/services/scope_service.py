"""权限与关系图。

三件事：
1. RelationGraph —— 一次加载全族的婚姻/亲子关系到内存，供 BFS 使用。
   单族几千人，两张关系表加起来几千行，加载一次几十毫秒，比逐节点查库快得多。
2. 近亲集合计算（直系上下 3 代 + 兄弟姐妹 + 配偶）—— 脱敏规则的判定依据。
3. 邀请码授权范围解析（决策第 5 条方案 C）—— 按 scope_rule 算出节点快照。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import (
    DEFAULT_SCOPE_UP_DOWN,
    LineageRole,
    ScopeSource,
    UserRole,
    ViewerScope,
)
from app.core.errors import no_edit_scope, not_found
from app.models.person import FamilyUnion, ParentChild, Person
from app.models.user import AppUser, EditScope
from app.services.mask import Viewer


# ============================================================
# 关系图
# ============================================================


class RelationGraph:
    """内存中的家族关系图。

    father_of        子女 -> 承袭方父/母（is_primary=1 的那条）
    mother_of        子女 -> 另一位伴侣
    children_of      父/母 -> [子女]
    spouses_of       人 -> [配偶]
    relation_of      子女 -> 亲子关系类型（1亲生 2过继 …）
    role_of          人 -> lineage_role
    person_of        人 -> Person 对象
    """

    __slots__ = (
        "family_id", "person_of", "father_of", "mother_of",
        "children_of", "spouses_of", "relation_of", "role_of", "living_of",
        "unions", "links",
    )

    def __init__(self, family_id: int) -> None:
        self.family_id = family_id
        self.person_of: dict[int, Person] = {}
        self.father_of: dict[int, int] = {}
        self.mother_of: dict[int, int] = {}
        self.children_of: dict[int, list[int]] = {}
        self.spouses_of: dict[int, list[int]] = {}
        self.relation_of: dict[int, int] = {}
        self.role_of: dict[int, int] = {}
        self.living_of: dict[int, int] = {}
        # 原始关系数据，供布局算法使用
        self.unions: dict[int, FamilyUnion] = {}
        self.links: list[ParentChild] = []

    # ---------- 加载 ----------

    @classmethod
    def load(cls, db: Session, family_id: int) -> "RelationGraph":
        g = cls(family_id)

        persons = db.execute(
            select(Person).where(Person.family_id == family_id, Person.status == 1)
        ).scalars().all()
        for p in persons:
            g.person_of[p.id] = p
            g.role_of[p.id] = p.lineage_role
            g.living_of[p.id] = p.living_status

        unions = db.execute(
            select(FamilyUnion).where(
                FamilyUnion.family_id == family_id, FamilyUnion.status == 1
            )
        ).scalars().all()
        union_map: dict[int, FamilyUnion] = {}
        for u in unions:
            union_map[u.id] = u
            if u.partner_b_id is not None and u.partner_a_id != u.partner_b_id:
                g.spouses_of.setdefault(u.partner_a_id, []).append(u.partner_b_id)
                g.spouses_of.setdefault(u.partner_b_id, []).append(u.partner_a_id)
        # 必须挂到实例上：布局算法、邀请码范围解析都从这里取婚姻数据。
        # 漏了这行的话 spouses_of 有值、unions 却是空的，症状很隐蔽——
        # 夫妻在树上不合并、婚姻连线整条消失，但其余部分看起来完全正常。
        g.unions = union_map

        links = db.execute(
            select(ParentChild).where(
                ParentChild.family_id == family_id, ParentChild.status == 1
            )
        ).scalars().all()

        g.links = list(links)

        # 一条 child 可能有多条（过继）：is_primary=1 的那条决定它挂在谁下面
        for lk in sorted(links, key=lambda x: -x.is_primary):
            u = union_map.get(lk.union_id)
            if u is None:
                continue
            carrier = u.partner_a_id
            other = u.partner_b_id
            g.children_of.setdefault(carrier, []).append(lk.child_id)
            if other is not None:
                g.children_of.setdefault(other, []).append(lk.child_id)
            if lk.is_primary == 1:
                g.father_of.setdefault(lk.child_id, carrier)
                if other is not None:
                    g.mother_of.setdefault(lk.child_id, other)
                g.relation_of.setdefault(lk.child_id, lk.relation)

        for k in g.children_of:
            g.children_of[k] = sorted(set(g.children_of[k]))
        return g

    # ---------- 查询 ----------

    def primary_spouse(self, person_id: int) -> int | None:
        """现任配偶：未结束的最后一段；全都结束了就给最后一段。

        旧实现返回 `spouses_of[0]`，而那个列表的顺序来自数据库行序——
        既不是"元配"，也不是"现任"，是一场随机的排序。
        """
        unions = [
            u
            for u in self.unions.values()
            if u.status == 1 and person_id in (u.partner_a_id, u.partner_b_id)
        ]
        if not unions:
            return None

        live = [u for u in unions if u.end_date is None and u.end_reason is None]
        pool = live or unions
        u = max(pool, key=lambda x: (x.union_seq, x.id))
        other = u.partner_b_id if u.partner_a_id == person_id else u.partner_a_id
        return other if other != person_id else None

    def ancestors(self, person_id: int, depth: int) -> list[int]:
        out: list[int] = []
        cur = person_id
        for _ in range(max(depth, 0)):
            f = self.father_of.get(cur)
            if f is None:
                break
            out.append(f)
            cur = f
        return out

    def descendants(self, person_id: int, depth: int) -> list[int]:
        out: list[int] = []
        seen = {person_id}
        frontier = [person_id]
        for _ in range(max(depth, 0)):
            nxt: list[int] = []
            for pid in frontier:
                for c in self.children_of.get(pid, []):
                    if c in seen:
                        continue
                    seen.add(c)
                    out.append(c)
                    nxt.append(c)
            frontier = nxt
            if not frontier:
                break
        return out

    def siblings(self, person_id: int) -> list[int]:
        f = self.father_of.get(person_id) or self.mother_of.get(person_id)
        if f is None:
            return []
        return [c for c in self.children_of.get(f, []) if c != person_id]

    # ---------- 近亲集合 ----------

    def close_relatives(
        self,
        anchor_id: int,
        up: int = 3,
        down: int = 3,
        *,
        include_siblings: bool = True,
    ) -> set[int]:
        """直系上下 N 代 + 兄弟姐妹 + 配偶。与"我的一家"的范围保持一致——
        用户在同一屏里看到的人，就该是他能看全的人。"""
        result: set[int] = {anchor_id}

        # 向上
        for a in self.ancestors(anchor_id, up):
            result.add(a)
            # 祖先的**每一位**配偶都进范围，不只"主配偶"。
            # 视图里所有夫人都画出来了，只给一位授权就等于
            # "看得见、补不了"——而祖母那一格恰恰是最常缺名字的地方。
            for sp in self.spouses_of.get(a, []):
                result.add(sp)

        # 向下
        for d in self.descendants(anchor_id, down):
            result.add(d)
            for sp in self.spouses_of.get(d, []):
                result.add(sp)

        # 兄弟姐妹（及其配偶）
        if include_siblings:
            for s in self.siblings(anchor_id):
                result.add(s)
                for sp in self.spouses_of.get(s, []):
                    result.add(sp)

        # 自己的配偶
        for sp in self.spouses_of.get(anchor_id, []):
            result.add(sp)

        return result

    # ---------- 邀请码授权范围 ----------

    def resolve_scope(
        self,
        anchor_id: int,
        rule: dict[str, Any] | None = None,
    ) -> list[int]:
        """按 scope_rule 解析出可编辑的 person_id 列表（快照）。

        rule 字段：up / down / spouse / siblings / externalDesc
        """
        r = {**DEFAULT_SCOPE_UP_DOWN, **(rule or {})}
        up = int(r.get("up", 3))
        down = int(r.get("down", 2))
        include_spouse = bool(r.get("spouse", True))
        include_siblings = bool(r.get("siblings", False))
        ext_depth = int(r.get("externalDesc", 2))

        if anchor_id not in self.person_of:
            raise not_found("锚点人物")

        scope: set[int] = {anchor_id}

        def add_with_spouse(pid: int) -> None:
            scope.add(pid)
            if include_spouse:
                for sp in self.spouses_of.get(pid, []):
                    scope.add(sp)

        # 向上
        for a in self.ancestors(anchor_id, up):
            add_with_spouse(a)

        # 兄弟姐妹
        if include_siblings:
            for s in self.siblings(anchor_id):
                add_with_spouse(s)

        # 自己的配偶
        if include_spouse:
            for sp in self.spouses_of.get(anchor_id, []):
                scope.add(sp)

        # 向下（本族，最多 down 层；外姓分支不计入层数）
        visited = {anchor_id}
        frontier = [anchor_id]
        for _ in range(down):
            nxt: list[int] = []
            for pid in frontier:
                for c in self.children_of.get(pid, []):
                    if c in visited or self._is_external(c):
                        continue
                    visited.add(c)
                    add_with_spouse(c)
                    nxt.append(c)
            frontier = nxt
            if not frontier:
                break

        # 外姓后代（外孙及以下），最多 ext_depth 层
        if ext_depth > 0:
            ext_seen: set[int] = set()
            ext_frontier: list[tuple[int, int]] = []
            for pid in list(scope) + [anchor_id]:
                for c in self.children_of.get(pid, []):
                    if self._is_external(c):
                        ext_frontier.append((c, 0))
            while ext_frontier:
                pid, d = ext_frontier.pop(0)
                if pid in ext_seen or d >= ext_depth:
                    continue
                ext_seen.add(pid)
                add_with_spouse(pid)
                for c in self.children_of.get(pid, []):
                    ext_frontier.append((c, d + 1))

        return sorted(scope)

    def _is_external(self, person_id: int) -> bool:
        """是否外姓成员（女婿 / 外孙及其后代）。"""
        return self.role_of.get(person_id) in (
            int(LineageRole.SPOUSE_OUT),
            int(LineageRole.EXTERNAL_DESC),
        )


# ============================================================
# Viewer 构建与鉴权
# ============================================================


def load_edit_scope_ids(db: Session, user_id: int, family_id: int) -> set[int]:
    now = datetime.now()
    rows = db.execute(
        select(EditScope.person_id).where(
            EditScope.user_id == user_id,
            EditScope.family_id == family_id,
            EditScope.status == 1,
        )
    ).scalars().all()
    return set(rows)


def build_viewer(
    db: Session,
    user: AppUser | None,
    family_id: int | None = None,
    *,
    graph: RelationGraph | None = None,
    with_close: bool = True,
) -> Viewer:
    """构建访问者上下文。未登录用户得到一个 GUEST viewer。"""
    if user is None:
        return Viewer(scope=ViewerScope.GUEST)

    fid = family_id or user.family_id

    viewer = Viewer(
        user_id=user.id,
        role=user.role,
        family_id=fid,
        claimed_person_id=user.claimed_person_id,
    )

    if viewer.role >= int(UserRole.FAMILY_ADMIN):
        viewer.scope = ViewerScope.ADMIN
    elif user.claimed_person_id:
        viewer.scope = ViewerScope.CLOSE
    elif fid is not None:
        viewer.scope = ViewerScope.LINEAGE

    if fid is None:
        return viewer

    if user.claimed_person_id:
        g = graph or RelationGraph.load(db, fid)
        viewer.close_person_ids = g.close_relatives(user.claimed_person_id)
        viewer.edit_scope_ids = load_edit_scope_ids(db, user.id, fid)
        if not viewer.edit_scope_ids:
            # 认领通过但授权表为空时兜底（老数据/异常），至少能改自己
            viewer.edit_scope_ids = {user.claimed_person_id}
    else:
        viewer.edit_scope_ids = load_edit_scope_ids(db, user.id, fid)

    return viewer


def ensure_can_edit(db: Session, viewer: Viewer, person_ids: Iterable[int]) -> None:
    """写操作前的鉴权。不在授权范围内一律拒绝。

    设计原则：权限宁可偏紧不可偏松。
    偏紧的代价是用户多点一次"申请扩展"；偏松的代价是家谱被改坏且查不出是谁改的。
    """
    if viewer.is_admin:
        return
    if not viewer.is_logged_in:
        raise no_edit_scope("请先登录并认领您的节点")

    missing = [pid for pid in person_ids if pid not in viewer.edit_scope_ids]
    if missing:
        raise no_edit_scope()


def grant_edit_scope(
    db: Session,
    *,
    user_id: int,
    family_id: int,
    person_ids: Iterable[int],
    source: ScopeSource,
    invite_code_id: int | None = None,
    granted_by: int | None = None,
    expires_at: datetime | None = None,
) -> int:
    """写入可编辑范围（幂等：已存在的跳过）。返回新增条数。"""
    existing = {
        row for row in db.execute(
            select(EditScope.person_id).where(
                EditScope.user_id == user_id, EditScope.family_id == family_id
            )
        ).scalars().all()
    }
    now = datetime.now()
    added = 0
    for pid in set(person_ids):
        if pid in existing:
            # 已存在则续期/恢复
            row = db.execute(
                select(EditScope).where(
                    EditScope.user_id == user_id, EditScope.person_id == pid
                )
            ).scalar_one_or_none()
            if row is not None and row.status == 0:
                row.status = 1
                row.expires_at = expires_at
            continue
        db.add(
            EditScope(
                user_id=user_id,
                family_id=family_id,
                person_id=pid,
                source=int(source),
                invite_code_id=invite_code_id,
                granted_by=granted_by,
                granted_at=now,
                expires_at=expires_at,
                status=1,
            )
        )
        added += 1
    return added
