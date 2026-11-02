"""家族树三视图组装与布局缓存。

三种视图的参数差异（设计文档 4.1）：

| view     | 节点范围                                    | 端上技术      | 典型节点数 |
|----------|---------------------------------------------|---------------|-----------|
| mine     | 锚点上下 3 代 + 配偶 + 兄弟姐妹 + 外孙 2 层 | WXML          | 25—40     |
| lineage  | 锚点所在房的全部成员                        | 虚拟列表      | 100—600   |
| overview | 全族 lineage_role IN (1,2,3)                | Canvas        | 1000—5000 |

缓存策略：
- 缓存只存**与访问者无关**的部分（坐标与结构）
- 姓名等字段每次按 viewer 实时投影，因为脱敏强度因人而异
- 缓存是否有效由 cache.family_rev 与 family.data_rev 比对决定
- 客户端可带上上次的 rev；一致则返回 notModified，一个字节都不传
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import (
    EdgeType,
    LineageRole,
    OVERVIEW_VISIBLE_ROLES,
    TreeView,
)
from app.core.errors import param_invalid
from app.models.family import Family
from app.models.system import TreeLayoutCache
from app.services.mask import Viewer, cn_number, project_name
from app.services.missing import build_missing_tasks
from app.services.scope_service import RelationGraph
from app.services.tree_layout import (
    ChildLink,
    LayoutParams,
    PersonLite,
    UnionLite,
    compute_layout,
)

log = logging.getLogger("raoxin.tree")

VIEW_TYPE = {TreeView.MINE: 1, TreeView.LINEAGE: 2, TreeView.OVERVIEW: 3}


# ============================================================
# 主入口
# ============================================================


def get_tree(
    db: Session,
    family: Family,
    graph: RelationGraph,
    view: TreeView,
    anchor_id: int | None,
    viewer: Viewer,
    *,
    client_rev: int | None = None,
    detail: str = "full",
    sibling_limit: int | None = None,
) -> dict[str, Any]:
    rev = family.data_rev

    if client_rev is not None and int(client_rev) == int(rev):
        return {"notModified": True, "rev": rev}

    if view in (TreeView.MINE, TreeView.LINEAGE) and anchor_id is None:
        raise param_invalid(f"view={view.value} 需要 anchor 参数")

    if anchor_id is not None and anchor_id not in graph.person_of:
        raise param_invalid("anchor 人物不存在或不属于该宗族")

    sib_limit = max(1, int(sibling_limit if sibling_limit is not None
                          else settings.mine_sibling_limit))

    member_ids, scope_meta = _resolve_scope(graph, view, anchor_id, sib_limit)
    view_type = VIEW_TYPE[view]

    view_key = f"{view_type}:{anchor_id or 'none'}"
    # 折叠数量影响布局，必须进缓存键，否则"展开全部"会命中折叠版缓存拿到旧图
    if view == TreeView.MINE:
        view_key += f":sib{sib_limit}"
    if detail == "lite":
        view_key += ":lite"
    # 布局算法改版要换 key，否则旧坐标会一直命中缓存——
    # 症状是"代码改了、界面完全没变"，比报错更难查。
    # L4：mine 主线全量（始祖→锚点全部成卡）+ truncated 锚点下发（2026-10-28）
    # L5：谱牌缩到 64×72 / 间距 16（2026-11-02，一屏横向放下 4 人）
    view_key += ":L5"

    layout, from_cache = _load_or_compute(db, family, graph, member_ids, view_key, view_type, anchor_id, rev)

    result = _assemble(db, graph, layout, viewer, view, anchor_id, rev, detail,
                       scope_ids=member_ids)
    if scope_meta:
        result["collapsed"] = scope_meta
    result["cached"] = from_cache
    return result


# ============================================================
# 范围计算
# ============================================================


def _resolve_scope(
    graph: RelationGraph,
    view: TreeView,
    anchor_id: int | None,
    sibling_limit: int,
) -> tuple[set[int], dict[str, Any] | None]:
    """返回（成员集合，折叠信息）。折叠信息只有 mine 视图会有。"""
    if view == TreeView.MINE:
        assert anchor_id is not None
        return _scope_mine(graph, anchor_id, sibling_limit)
    if view == TreeView.LINEAGE:
        return _scope_lineage(graph, anchor_id), None
    return _scope_overview(graph), None


def _scope_mine(
    graph: RelationGraph,
    anchor_id: int,
    sibling_limit: int,
) -> tuple[set[int], dict[str, Any] | None]:
    """我的一家：始祖→锚点整条父链 + 上下配偶 + 兄弟姐妹 + 向下 1 代 + 外孙 2 层。

    2026-10-28 再改（垂直卡片流）：mine 已经不再用横排画布渲染，
    「一屏塞不下」的约束消失了。用户对这一屏的预期变成——
    **从我往上的直系先祖全部卡片式展示，可以上下滑动**。
    所以向上不再限代数：沿父链一路走到始祖，每代都进图（含各代配偶），
    15 代也就 15 行，竖着滚刚刚好。向下仍取 1 代，再往下的世代
    靠「下面还有后代」逐代下看（服务端下发 downAnchor）。

    兄弟姐妹超过 sibling_limit 个男丁节点时折叠，否则光同代就能
    横向铺开 10 个夫妻单元。被折叠的人不删数据，只是这一屏不画。
    """
    down = settings.mine_down_generations
    ext = settings.mine_external_desc_generations

    ids: set[int] = {anchor_id}

    # 向上：沿父链一直到始祖（含每一代的配偶——那位"配某氏"也是先人）
    cur = anchor_id
    while True:
        f = graph.father_of.get(cur)
        if f is None or f in ids:
            break
        ids.add(f)
        for sp in graph.spouses_of.get(f, []):
            ids.add(sp)
        cur = f

    # 自己的配偶
    for sp in graph.spouses_of.get(anchor_id, []):
        ids.add(sp)

    # 兄弟姐妹（及配偶）——不展开他们的子女
    all_sibs = sorted(set(graph.siblings(anchor_id)))
    keep = _pick_siblings(anchor_id, all_sibs, sibling_limit)
    for s in keep:
        ids.add(s)
        for sp in graph.spouses_of.get(s, []):
            ids.add(sp)

    collapsed: dict[str, Any] | None = None
    hidden = [s for s in all_sibs if s not in keep]
    if hidden:
        collapsed = {
            "siblingsHidden": len(hidden),
            "siblingsTotal": len(all_sibs) + 1,  # 含锚点本人
        }

    # 向下：本族分支最多 down 层；外姓分支交给 ext 单独控制
    visited = {anchor_id}
    frontier = [anchor_id]
    for _ in range(down):
        nxt: list[int] = []
        for pid in frontier:
            for c in graph.children_of.get(pid, []):
                if c in visited or _is_external(graph, c):
                    continue
                visited.add(c)
                ids.add(c)
                for sp in graph.spouses_of.get(c, []):
                    ids.add(sp)
                nxt.append(c)
        frontier = nxt
        if not frontier:
            break

    # 外孙及外姓后代（决策第 2 条：只在"我的一家"显示）
    if ext > 0:
        from collections import deque

        queue: deque[tuple[int, int]] = deque()
        for pid in list(ids):
            for c in graph.children_of.get(pid, []):
                if _is_external(graph, c):
                    queue.append((c, 0))
        seen: set[int] = set()
        while queue:
            pid, d = queue.popleft()
            if pid in seen or d >= ext:
                continue
            seen.add(pid)
            ids.add(pid)
            for sp in graph.spouses_of.get(pid, []):
                ids.add(sp)
            for c in graph.children_of.get(pid, []):
                queue.append((c, d + 1))

    return ids, collapsed


def _pick_siblings(anchor_id: int, sibs: list[int], limit: int) -> set[int]:
    """从兄弟里挑最多 limit-1 个（limit 含锚点本人），挑离锚点最近的那几个。

    为什么按 id 距离而不是"长幼"：老谱导入时 id 基本就是录入顺序，
    近似长幼；而不管按什么排，**锚点左侧和右侧各留几个**都比"取前 N 个"
    更符合直觉——取前 N 个会把锚点本人挤出屏幕，用户一进来找不到自己。
    """
    keep: set[int] = {anchor_id}
    room = max(0, int(limit) - 1)
    if room <= 0 or not sibs:
        return keep

    ordered = sorted(sibs, key=lambda s: (abs(s - anchor_id), s))
    for s in ordered[:room]:
        keep.add(s)
    return keep


def _scope_lineage(graph: RelationGraph, anchor_id: int | None) -> set[int]:
    """我的支系：锚点所在房的全部成员（及配偶）。"""
    if anchor_id is None:
        return _scope_overview(graph)

    person = graph.person_of.get(anchor_id)
    if person is None or person.branch_id is None:
        return _scope_overview(graph)

    ids = {
        pid for pid, p in graph.person_of.items() if p.branch_id == person.branch_id
    }
    for pid in list(ids):
        for sp in graph.spouses_of.get(pid, []):
            if sp in graph.person_of:
                ids.add(sp)

    # 房长为空时至少保证锚点那一支是完整的
    ids.add(anchor_id)
    return ids


def _scope_overview(graph: RelationGraph) -> set[int]:
    """全族总览：只含本族男丁、本族女儿、媳妇。

    决策第 2 条：外孙与女婿（role 4/5）不进全族树，只在我的一家显示。
    这里不删数据、不建第二张表，只在范围上过滤——用户不需要理解规则，
    端上用虚线框就能表达清楚"这是外姓"。
    """
    return {
        pid
        for pid, p in graph.person_of.items()
        if p.lineage_role in OVERVIEW_VISIBLE_ROLES
    }


def _is_external(graph: RelationGraph, person_id: int) -> bool:
    return graph.role_of.get(person_id) in (
        int(LineageRole.SPOUSE_OUT),
        int(LineageRole.EXTERNAL_DESC),
    )


# ============================================================
# 布局与缓存
# ============================================================


def _params() -> LayoutParams:
    return LayoutParams(
        node_width=settings.tree_node_width,
        node_height=settings.tree_node_height,
        gen_gap=settings.tree_gen_gap,
        sibling_gap=settings.tree_sibling_gap,
    )


def _load_or_compute(
    db: Session,
    family: Family,
    graph: RelationGraph,
    member_ids: set[int],
    view_key: str,
    view_type: int,
    anchor_id: int | None,
    rev: int,
) -> tuple[dict[str, Any], bool]:
    cached = db.execute(
        select(TreeLayoutCache).where(
            TreeLayoutCache.family_id == family.id,
            TreeLayoutCache.view_key == view_key,
        )
    ).scalar_one_or_none()

    if cached is not None and int(cached.family_rev) == int(rev):
        try:
            return json.loads(cached.layout_json), True
        except (ValueError, TypeError):
            log.warning("布局缓存损坏，重算 view_key=%s", view_key)

    t0 = time.perf_counter()
    layout = _compute_layout(
        graph,
        member_ids,
        group_by_mother=(view_type == VIEW_TYPE[TreeView.MINE]),
    )
    calc_ms = int((time.perf_counter() - t0) * 1000)

    # 写缓存（覆盖）
    payload = json.dumps(layout, ensure_ascii=False, separators=(",", ":"))
    if cached is None:
        db.add(
            TreeLayoutCache(
                family_id=family.id,
                view_type=view_type,
                anchor_person_id=anchor_id,
                view_key=view_key,
                layout_json=payload,
                node_count=len(layout["nodes"]),
                family_rev=rev,
                calc_ms=calc_ms,
                created_at=datetime.now(),
            )
        )
    else:
        cached.layout_json = payload
        cached.node_count = len(layout["nodes"])
        cached.family_rev = rev
        cached.calc_ms = calc_ms
        cached.created_at = datetime.now()
    db.commit()

    if calc_ms > 500:
        log.warning("布局计算较慢 view_key=%s nodes=%s 耗时=%sms", view_key, len(layout["nodes"]), calc_ms)

    return layout, False


def _compute_layout(
    graph: RelationGraph,
    member_ids: set[int],
    *,
    group_by_mother: bool = False,
) -> dict[str, Any]:
    from app.models.person import FamilyUnion, ParentChild

    persons = [graph.person_of[i] for i in member_ids if i in graph.person_of]

    # 婚姻**只要承袭方在范围内就保留**。
    # 不能要求两端都在：全族总览现在只画直系丁口、不含媳妇，
    # 按"两端都在"过滤会把每一段婚姻都丢掉，亲子链接随之全没了
    # —— 症状是总览有节点、零条连线。
    # 配偶不在范围内时布局层会自然忽略她（只在两端都可见时才并成一个单元）。
    unions = [
        u for u in graph.unions.values() if u.partner_a_id in member_ids
    ]
    # 只保留父子都在范围内的亲子关系
    links = [
        lk
        for lk in graph.links
        if lk.child_id in member_ids
        and lk.union_id in {u.id for u in unions}
    ]

    return compute_layout(
        [
            PersonLite(
                id=p.id,
                name=p.name,
                surname=p.surname,
                gender=p.gender,
                generation=p.generation,
                lineage_role=p.lineage_role,
                living_status=p.living_status,
                privacy_level=p.privacy_level,
                is_lineage_carrier=p.is_lineage_carrier,
                branch_id=p.branch_id,
            )
            for p in persons
        ],
        [
            UnionLite(
                id=u.id,
                partner_a_id=u.partner_a_id,
                partner_b_id=u.partner_b_id,
                union_type=u.union_type,
                union_seq=u.union_seq,
                end_date=u.end_date,
                end_reason=u.end_reason,
            )
            for u in unions
        ],
        [
            ChildLink(
                union_id=lk.union_id,
                child_id=lk.child_id,
                relation=lk.relation,
                is_primary=lk.is_primary,
                seq=lk.seq,
            )
            for lk in links
        ],
        _params(),
        group_by_mother=group_by_mother,
    )


def invalidate_family_cache(db: Session, family: Family) -> None:
    """数据变更后使缓存失效，并把族的数据版本号 +1。

    调用点：revision 审核通过、批量导入、节点合并。
    """
    family.data_rev = int(family.data_rev) + 1
    db.query(TreeLayoutCache).filter(TreeLayoutCache.family_id == family.id).delete(
        synchronize_session=False
    )
    db.commit()


# ============================================================
# 输出组装（按 viewer 脱敏）
# ============================================================


def _assemble(
    db: Session,
    graph: RelationGraph,
    layout: dict[str, Any],
    viewer: Viewer,
    view: TreeView,
    anchor_id: int | None,
    rev: int,
    detail: str,
    scope_ids: set[int] | None = None,
) -> dict[str, Any]:
    nodes_out: list[dict[str, Any]] = []
    on_path = _path_ids(graph, anchor_id) if anchor_id else set()
    lite = detail == "lite"

    for n in layout["nodes"]:
        pid = n["id"]
        person = graph.person_of.get(pid)

        if person is None:
            continue

        d: dict[str, Any] = {
            "id": pid,
            "x": n["x"],
            "y": n["y"],
            "gen": n["gen"],
            # w/h 必须透出。端上靠它算连线端点（tree-lines.js）和把锚点滚到
            # 屏幕中间（home.js 的 centerOn）。布局层一直在算这两个值，
            # 投影时漏掉的话端上拿到 undefined，会静默算出 NaN 坐标。
            "w": n.get("w"),
            "h": n.get("h"),
            "gender": n.get("gender", 0),
            "role": n.get("role", 1),
            "living": n.get("living", 2),
        }
        if n.get("isSpouse"):
            d["isSpouse"] = True
            # 配偶卡片要能写「元配 / 继配 / 已离异」，靠的就是这几个字段；
            # 投影时漏掉，端上只能一律显示"媳妇"，多妻就分不出先后与现状。
            for k in ("spouseOf", "unionSeq", "unionType", "endReason", "unionEnded"):
                if k in n:
                    d[k] = n[k]

        if not lite:
            name, display_name = project_name(person, viewer)
            d["name"] = name
            d["displayName"] = display_name
            d["isSelf"] = viewer.claimed_person_id == pid
            d["onPath"] = pid in on_path
            d["isPending"] = False

            # 角标只给"能补的人"画。
            # 缺口清单的入口是 /person/{id}/missing，它按 viewer.can_edit 放行；
            # 如果这里无条件给每个人算角标，普通族人会看到满屏的「1」，
            # 点进去必然被拒——一个数字配一个注定失败的按钮，比不显示更糟。
            # 谓词必须与 /missing、「去补全」跳转三处**同一个人、同一个条件**，
            # 否则又会退化成"显示 A 的缺口、打开 B 的清单"（2026-10-21 实报）。
            if viewer.can_edit(pid):
                missing = _quick_missing(graph, pid)
                if missing:
                    d["hasPending"] = True
                    d["badge"] = {"type": "pending", "count": missing}

        nodes_out.append(d)

    # 按世代、横坐标排序，渲染顺序稳定
    nodes_out.sort(key=lambda x: (x["gen"], x["x"]))

    result: dict[str, Any] = {
        "notModified": False,
        "rev": rev,
        "view": view.value,
        "anchorPersonId": anchor_id,
        "viewerScope": viewer.scope.value,
        "canvas": layout["canvas"],
        "nodes": nodes_out,
        "edges": layout["edges"],
        "truncated": _truncation_hint(graph, view, anchor_id, scope_ids=scope_ids),
    }

    if not lite:
        result["pathFromFounder"] = [
            pid
            for pid in _root_to_anchor(graph, anchor_id)
            if pid in {n["id"] for n in nodes_out}
        ]

    if view == TreeView.MINE and anchor_id is not None and viewer.can_edit(anchor_id):
        # pending 必须自报"这是谁的缺口"。
        # 端上原来点「去补全」直接跳 claimedPersonId（自己），而数字来自锚点，
        # 于是"点吴敏显示还差一项，点进去说陈启华已补全"——两个人。
        result["pending"] = _pending_summary(graph, anchor_id, viewer)

    if view == TreeView.OVERVIEW:
        result.update(_generation_stats(nodes_out))

    return result


def _generation_stats(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """每一代的"丁口"人数，按男/女分开。

    **只数 lineage_role 1（男丁）和 2（女儿）**：媳妇、女婿、外孙是外姓，
    计进人数会让族谱的世系规模虚高——一个 12 代 200 人的族，媳妇能占一半，
    报出去"400 人"既不合谱例，也没法跟理事会的老谱对上。

    为什么还要分男女：老谱的世系表本来就是一格一男丁，女儿另记。
    理事会一对账，问的第一句就是"这一代几个男丁"，只给总数答不了。

    这个统计在服务端算而不是端上数：地方志/族谱报的口径必须唯一，
    端上一旦哪天为了性能少画了几代，数字就跟着错，而且没人会发现。
    """
    male: dict[int, int] = {}
    female: dict[int, int] = {}
    for n in nodes:
        role = int(n.get("role") or 1)
        if role not in (1, 2):
            continue
        gen = int(n.get("gen") or 0)
        if gen <= 0:
            continue
        bucket = male if role == 1 else female
        bucket[gen] = bucket.get(gen, 0) + 1

    stats = [
        {
            "gen": g,
            "count": male.get(g, 0) + female.get(g, 0),
            "male": male.get(g, 0),
            "female": female.get(g, 0),
        }
        for g in sorted(set(male) | set(female))
    ]
    return {
        "generationStats": stats,
        "lineageTotal": sum(s["count"] for s in stats),
        "maleTotal": sum(s["male"] for s in stats),
        "femaleTotal": sum(s["female"] for s in stats),
        "generationCount": len(stats),
    }


def _path_ids(graph: RelationGraph, anchor_id: int | None) -> set[int]:
    return set(_root_to_anchor(graph, anchor_id)) if anchor_id else set()


def _root_to_anchor(graph: RelationGraph, anchor_id: int | None) -> list[int]:
    """从最远的可考祖先一路到锚点。这条线解决用户面对大树的第一问题：我在哪。"""
    if anchor_id is None:
        return []
    path: list[int] = []
    seen: set[int] = set()
    cur: int | None = anchor_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        path.append(cur)
        cur = graph.father_of.get(cur)
    path.reverse()
    return path


def _quick_missing(graph: RelationGraph, person_id: int) -> int:
    """某个节点还缺几项，用于树上的角标。

    **必须走 build_missing_tasks，不能在这里自己再判一遍。**

    这里原本有一份手抄的简化判断（父/母/配偶）。它和清单是两套口径，
    于是"补全页说已完整、首页底部条还说还差一项"这种自相矛盾会反复复发：
    上次是完整度百分比（67%），这次是「未婚」——清单认出 marital_status 了，
    手抄版没认，底部条就不消失。这类"同一件事算两遍"的代码删一次算一次。
    """
    if person_id not in graph.person_of:
        return 0
    # max_items=0：只要计数，不要清单正文
    tasks = build_missing_tasks(graph, person_id, max_items=0)
    return int(tasks.get("missingCount") or 0)


def _pending_summary(graph: RelationGraph, anchor_id: int, viewer: Viewer) -> dict[str, Any]:
    """首页底部那条「还差 N 项」——与补全页同一份清单。

    `personId` / `displayName` 必须回给端上：数字属于锚点，不是"我"。
    少了这两个字段，端上只能猜「去补全」该跳谁，而猜错的默认值（自己）
    恰好是另一个人的清单——两个都"正常"的页面，说的是两件事。
    """
    if anchor_id not in graph.person_of:
        return {"missingCount": 0, "items": []}

    person = graph.person_of[anchor_id]
    tasks = build_missing_tasks(graph, anchor_id, max_items=99)
    _, display_name = project_name(person, viewer)
    return {
        "personId": anchor_id,
        "displayName": display_name,
        # 这条数字是「我」的，还是「当前聚焦的这位」的？
        # 端上文案和点击目标都靠它分流——少了它，就只能默认是自己，
        # 于是"点吴敏显示还差一项、点去补全打开的是陈启华"。
        "isSelf": viewer.claimed_person_id == anchor_id,
        "missingCount": int(tasks.get("missingCount") or 0),
        # 端上目前只用 missingCount；items 保留 key 列表，便于将来做"差哪几项"的提示
        "items": [it["key"] for it in (tasks.get("items") or [])],
    }


def _truncation_hint(
    graph: RelationGraph,
    view: TreeView,
    anchor_id: int | None,
    scope_ids: set[int] | None = None,
) -> dict[str, Any]:
    """流首尾的「更上一世 / 下面还有后代」提示，并直接下发**跳到谁**。

    2026-10-28 修一个真 bug：旧实现按固定代数（up/down limit）判断截断，
    但端上"下面还有后代"取的是流里最底下那张卡——mine 窗口下探 1 代时，
    锚点的孩子全成了父卡的「子女」小片，最底下的**卡**永远是锚点本人。
    于是点「下面还有后代」= 把锚点设为锚点，端上早退，点了没反应。

    现在改成两件事都按**实际下发的成员集合**判断：
      up/down        —— 父链 / 后代里还有没有不在这屏上的人
      upAnchor       —— 父链上第一个出屏的先人（点「更上一世」跳他）
      downAnchor     —— 离锚点最近的一个出屏后代（点「下面还有后代」跳他）
                        出屏候选有多个时取承袭世系的那支，一跳就是主线上下一位
    """
    if view == TreeView.OVERVIEW or anchor_id is None:
        return {"up": False, "down": False, "upAnchor": None, "downAnchor": None,
                "hint": None}

    scope = scope_ids if scope_ids is not None else set()

    # ---- 向上：沿父链找第一个不在屏上的先人 ----
    up_anchor = None
    cur = graph.father_of.get(anchor_id)
    while cur is not None:
        if cur not in scope:
            up_anchor = cur
            break
        cur = graph.father_of.get(cur)
    truncated_up = up_anchor is not None

    # ---- 向下：BFS 找最近一代的出屏后代（只走本族分支） ----
    down_anchor = None
    down_depth = None
    visited = {anchor_id}
    queue: deque[tuple[int, int]] = deque([(anchor_id, 0)])
    while queue:
        pid, depth = queue.popleft()
        if down_depth is not None and depth > down_depth:
            break
        for c in graph.children_of.get(pid, []):
            if c in visited or _is_external(graph, c):
                continue
            visited.add(c)
            if c not in scope:
                # 同一代里优先承袭世系的：跳下去就是主线上的下一位
                p = graph.person_of.get(c)
                carrier = bool(p is not None and p.is_lineage_carrier)
                if down_anchor is None or (
                    depth == down_depth and carrier
                    and not bool(graph.person_of.get(down_anchor)
                                 and graph.person_of[down_anchor].is_lineage_carrier)
                ):
                    down_anchor = c
                    down_depth = depth
                continue
            queue.append((c, depth + 1))
    truncated_down = down_anchor is not None

    hint = None
    if truncated_up:
        # 别再写「去我的支系看完整世系」：这个视图 2026-10-21 已退休。
        # 现在往上看的唯一动作是**换锚点**——点画面上最上面那位，焦点就上移一代。
        # 提示词必须教用户做得到的事，指向一个不存在的入口等于把人带到死路。
        hint = "上方还有更早的祖先，点最上面那位继续往上看"
    elif truncated_down:
        hint = "下方还有后代，点击节点可把他设为新的中心"

    return {"up": truncated_up, "down": truncated_down,
            "upAnchor": up_anchor, "downAnchor": down_anchor, "hint": hint}


def generation_label(gen: int | None) -> str | None:
    return f"第{cn_number(gen)}世" if gen else None
