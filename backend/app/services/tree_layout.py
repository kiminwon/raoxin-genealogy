"""家族树布局算法（Reingold-Tilford）与三视图范围裁剪。

## 为什么布局放服务端

几千人的树在手机上现算会直接卡死。服务端算好每个人的 (x, y) 坐标后返回，
小程序端只负责按坐标绝对定位——端上零布局计算。坐标算完缓存，靠
family.data_rev 判断失效。

## 为什么用"子树范围"变体而不是经典 contour 版本

家谱树的特点是**浅而宽**（几十代 × 每代几十上百人），不是深而窄。
经典 Reingold-Tilford 的 contour 优化主要解决窄而深树的空间压缩问题，
在这里收益有限；而"子树范围"版本实现更简单、更不容易出错，
对数据局部变更的响应也更稳定——这点对缓存策略很重要。

## 核心规则

- y 轴由**世代**决定（决策第 1 条：按世代排，不按出生年）
- x 轴由**树结构**决定：叶子从左到右依次排列，父节点居中于其子节点
- 同一个人与其配偶合并为一个"布局单元"，横向并排，单元宽度动态计算
- 支持多配偶（单元宽度随配偶数增加）
- 支持森林（范围内有多个无父节点时横向并列）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence


# ============================================================
# 输入数据结构（与 ORM 解耦，方便单测）
# ============================================================


@dataclass(slots=True)
class PersonLite:
    id: int
    name: str | None = None
    surname: str | None = None
    gender: int = 0
    generation: int | None = None
    lineage_role: int = 1
    living_status: int = 2
    privacy_level: int = 2
    is_lineage_carrier: int = 0
    branch_id: int | None = None


@dataclass(slots=True)
class UnionLite:
    id: int
    partner_a_id: int
    partner_b_id: int | None = None
    union_type: int = 1
    union_seq: int = 1
    end_date: date | None = None
    end_reason: int | None = None


@dataclass(slots=True)
class ChildLink:
    union_id: int
    child_id: int
    relation: int = 1
    is_primary: int = 1
    seq: int = 0


# ============================================================
# 内部布局节点
# ============================================================


class _Node:
    __slots__ = (
        "person_id", "spouse_ids", "unit_w", "row_w", "children", "parent",
        "subtree_w", "cx", "gen", "col_w", "col_x", "mother_col",
    )

    def __init__(self, person_id: int, unit_w: float) -> None:
        self.person_id = person_id
        self.spouse_ids: list[int] = []
        self.unit_w = unit_w
        # 卡片行宽度（本人 + 配偶紧贴排成一条），与 unit_w 分离：
        # unit_w 还要容纳下方的子女列，但**卡片不能跟着子女列一起被撑开**，
        # 否则妻子会被推到几十上百像素外——这是"夫妻离得很远"的根因。
        self.row_w: float = unit_w
        self.children: list[_Node] = []
        self.parent: _Node | None = None
        self.subtree_w: float = unit_w
        self.cx: float = 0.0
        self.gen: int = 0
        # 按生母分列时才有：col_w[i] 是第 i 列的宽度（0 = 本人列，i>=1 = 第 i 位配偶列；
        # 0 表示这一列没有子女，**不占宽度**），col_x[i] 是这一列的中心 x（布局后才填）。
        # mother_col 是"我挂在哪一列"。
        self.col_w: list[float] | None = None
        self.col_x: list[float] | None = None
        self.mother_col: int = 0


# ============================================================
# 布局参数
# ============================================================


@dataclass
class LayoutParams:
    node_width: int = 88
    node_height: int = 96
    gen_gap: int = 140          # 每代之间的纵向间距（= 节点高 + 间距）
    sibling_gap: int = 20       # 相邻单元之间的横向间距
    # 夫妻间距固定为 0：两张卡**贴着**，视觉上是一个整体。
    # 留 12px 那种"礼貌的距离"会被读成"这两位没什么关系"，
    # 而家谱里夫妻关系恰恰是这个单元唯一要表达的东西。
    # 想区分开合状态不用间距，用虚框（离异）。
    spouse_gap: int = 0
    forest_gap: int = 80        # 森林中各棵树之间的间距
    padding: int = 40           # 画布四周留白


# ============================================================
# 主入口
# ============================================================


def compute_layout(
    persons: Sequence[PersonLite],
    unions: Sequence[UnionLite],
    links: Sequence[ChildLink],
    params: LayoutParams | None = None,
    *,
    group_by_mother: bool = False,
) -> dict[str, Any]:
    """计算布局。

    `group_by_mother=True`（焦点视图用）：子女挂到**生母所在的列**下方，
    而不是整体居中在父母单元之下。多妻时这是唯一能"一眼看出谁是谁所出"
    的排法，也顺带解决了婚姻/亲子连线横穿别人卡片的问题。
    总览是静态骨架，不需要分列，保持 False 以省宽度。


    返回：
        {
          "nodes": [{"id", "x", "y", "gen", "w", "h", "spouseIds", "isSpouseOf"}],
          "edges": [{"from", "to", "type"}],
          "canvas": {"width", "height", "nodeWidth", "nodeHeight", "genGap",
                     "siblingGap", "minGeneration", "maxGeneration"},
        }

    nodes[].x / y 是**节点左上角**坐标，端上直接 left/top 定位。
    """
    p = params or LayoutParams()
    if not persons:
        return _empty_layout(p)

    person_by_id: dict[int, PersonLite] = {x.id: x for x in persons}

    # ---------- 1. 推断世代（缺失的用父代 +1） ----------
    gen_of = _resolve_generations(persons, unions, links)

    # ---------- 2. 建立配偶关系 ----------
    # person_id -> [spouse_id]（按 union_seq 排序，保证布局稳定）
    spouses_of: dict[int, list[int]] = {}
    un_tmp: dict[int, UnionLite] = {u.id: u for u in unions}
    # (伴侣A, 伴侣B) -> union，供 _build_edges 用，避免在全族总览时退化成 O(n*m)
    pair_index: dict[frozenset[int], UnionLite] = {}
    for u in sorted(unions, key=lambda x: (x.union_seq, x.id)):
        a, b = u.partner_a_id, u.partner_b_id
        if b is None or a == b:
            continue
        if a in person_by_id and b in person_by_id:
            spouses_of.setdefault(a, []).append(b)
            pair_index[frozenset((a, b))] = u

    # 被并入他人单元的配偶，不再作为独立树节点参与层级
    merged_spouse_ids: set[int] = set()
    for _a, sps in spouses_of.items():
        merged_spouse_ids.update(sps)
    # 但一个人不能既是别人的配偶又是别人的"本人"（族内通婚的边界情况）：
    # 如果某人自己也承袭世系（is_lineage_carrier=1），优先让他当独立节点
    for pid in list(merged_spouse_ids):
        person = person_by_id.get(pid)
        if person and person.is_lineage_carrier == 1:
            merged_spouse_ids.discard(pid)
            for a, sps in spouses_of.items():
                if pid in sps:
                    sps.remove(pid)

    # ---------- 3. 建立布局节点 ----------
    nodes: dict[int, _Node] = {}
    for person in persons:
        if person.id in merged_spouse_ids:
            continue
        sp_list = [s for s in spouses_of.get(person.id, []) if s in person_by_id]
        unit_w = p.node_width * (1 + len(sp_list)) + p.spouse_gap * len(sp_list)
        node = _Node(person.id, unit_w)
        node.spouse_ids = sp_list
        node.gen = gen_of.get(person.id, 1)
        nodes[person.id] = node

    # ---------- 4. 建立父子边（主谱关系优先） ----------
    # child_id -> 承袭方 parent_id
    child_parent: dict[int, int] = {}
    for lk in sorted(links, key=lambda x: (x.is_primary, x.seq), reverse=True):
        if lk.is_primary != 1:
            continue
        if lk.child_id in child_parent:
            continue
        u = un_tmp.get(lk.union_id)
        if u is None:
            continue
        carrier = u.partner_a_id
        if carrier in nodes and lk.child_id in nodes:
            child_parent[lk.child_id] = carrier

    for child_id, parent_id in child_parent.items():
        parent_node = nodes[parent_id]
        child_node = nodes[child_id]
        if parent_node is child_node:
            continue
        # 防环：child 不能是 parent 的祖先
        if _is_ancestor(nodes, parent_node, child_node):
            continue
        child_node.parent = parent_node
        parent_node.children.append(child_node)

    # 主谱链接给出"这个孩子的生母是谁"，供按生母分列使用。
    # 只认 is_primary=1：过继的孩子在树上跟着嗣父母，本生母不该抢位置。
    child_mother: dict[int, int | None] = {}
    for lk in links:
        if lk.is_primary != 1 or lk.child_id in child_mother:
            continue
        u = un_tmp.get(lk.union_id)
        child_mother[lk.child_id] = u.partner_b_id if u is not None else None

    # 子节点排序：先按世代，再按 seq 排行，最后按 id 保证稳定
    for node in nodes.values():
        if len(node.children) > 1:
            node.children.sort(
                key=lambda c: (
                    gen_of.get(c.person_id, 1),
                    _child_seq(links, c.parent.person_id if c.parent else 0, c.person_id),
                    c.person_id,
                )
            )

    # ---------- 5. 找根并布局 ----------
    roots = [n for n in nodes.values() if n.parent is None]
    roots.sort(key=lambda n: (n.gen, n.person_id))

    cursor = float(p.padding)
    for root in roots:
        _measure(root, p, group_by_mother, child_mother)
        _place(root, cursor, p, group_by_mother)
        cursor += root.subtree_w + p.forest_gap

    # ---------- 6. 输出坐标 ----------
    all_gens = [gen_of.get(x.id, 1) for x in persons]
    min_gen, max_gen = min(all_gens), max(all_gens)
    gen_gap = p.gen_gap

    out_nodes: list[dict[str, Any]] = []
    for node in nodes.values():
        person = person_by_id[node.person_id]
        y = (node.gen - min_gen) * gen_gap + p.padding

        # 卡片行：本人 + 配偶**零间距**依次紧贴，整行居中于单元。
        # 位置只由 row_w 决定，与下方的子女列无关——
        # 这是"夫妻必须贴在一起"这条视觉规则唯一的落点。
        row_left = node.cx - node.row_w / 2.0

        out_nodes.append(_node_dict(person, row_left, y, p, gen=node.gen, is_spouse=False))

        for i, sid in enumerate(node.spouse_ids):
            sp = person_by_id[sid]
            sx = row_left + (i + 1) * (p.node_width + p.spouse_gap)
            d = _node_dict(sp, sx, y, p, gen=node.gen, is_spouse=True, spouse_of=node.person_id)
            _tag_union(d, pair_index, node.person_id, sid)
            out_nodes.append(d)

    # 独立出来的配偶（族内通婚等）单独补坐标，避免丢节点
    placed = {n["id"] for n in out_nodes}
    for person in persons:
        if person.id in placed:
            continue
        fallback_x = p.padding + (person.id % 10) * (p.node_width + p.sibling_gap)
        out_nodes.append(
            _node_dict(
                person, fallback_x,
                (gen_of.get(person.id, 1) - min_gen) * gen_gap + p.padding,
                p, gen=gen_of.get(person.id, 1),
            )
        )

    # 边型要靠 lineage_role 判断"外姓分支"（女婿/外孙 → 淡虚线）
    role_of: dict[int, int] = {x.id: x.lineage_role for x in persons}
    edges = _build_edges(
        nodes, child_parent, links, pair_index, role_of, child_mother,
        visible_ids=set(person_by_id),
    )

    width = max((n["x"] + n["w"] for n in out_nodes), default=p.padding) + p.padding
    height = (max_gen - min_gen) * gen_gap + p.node_height + p.padding * 2

    return {
        "nodes": out_nodes,
        "edges": edges,
        "canvas": {
            "width": round(width),
            "height": round(height),
            "nodeWidth": p.node_width,
            "nodeHeight": p.node_height,
            "genGap": gen_gap,
            "siblingGap": p.sibling_gap,
            "spouseGap": p.spouse_gap,
            "minGeneration": min_gen,
            "maxGeneration": max_gen,
        },
    }


# ============================================================
# 算法内部
# ============================================================


def _measure(
    node: _Node,
    p: LayoutParams,
    group_by_mother: bool = False,
    child_mother: dict[int, int | None] | None = None,
) -> float:
    """后序：算出每棵子树占据的横向宽度。

    开了 `group_by_mother` 时，一个人的子女**按生母分组**：
    列数 = 1（本人）+ 配偶数，每列宽度取「这一列子女的总宽」。
    分组是为了让同母的兄弟姐妹待在一起，不跟别的母亲的孩子混排。

    关键：**列宽不影响卡片位置**。
    卡片行宽度单独记在 `row_w`（夫妻紧贴），单元总宽取两者较大值。
    以前是"卡片放在列中心、列被子女撑多宽卡片就退多远"，
    于是妻子被推到 220px 外——同一位母亲的三个孩子，把她的卡片推走了。
    """
    for child in node.children:
        _measure(child, p, group_by_mother, child_mother)

    if not node.children:
        node.subtree_w = node.unit_w
        return node.subtree_w

    if not (group_by_mother and node.spouse_ids):
        total = 0.0
        for i, child in enumerate(node.children):
            total += child.subtree_w
            if i > 0:
                total += p.sibling_gap
        node.unit_w = max(node.row_w, total)
        node.subtree_w = node.unit_w
        return node.subtree_w

    ncol = 1 + len(node.spouse_ids)
    groups: list[list[_Node]] = [[] for _ in range(ncol)]
    mothers = child_mother or {}
    for child in node.children:
        mid = mothers.get(child.person_id)
        idx = node.spouse_ids.index(mid) + 1 if mid in node.spouse_ids else 0
        child.mother_col = idx
        groups[idx].append(child)

    # 空列宽 0：没有子女的那一列不该占位。
    # 亲生子女的生母都是配偶，所以「本人列」通常整列为空——
    # 若给它留一个 node_width，唯一有孩子的那列就被顶到右边，
    # 孩子看着不像挂在父母下面，而是偏在一侧。
    col_w: list[float] = []
    for g in groups:
        if not g:
            col_w.append(0.0)
            continue
        w = sum(c.subtree_w for c in g) + p.sibling_gap * (len(g) - 1)
        col_w.append(w)

    active = [w for w in col_w if w > 0]
    # 列间距用 sibling_gap（不是 spouse_gap）：这是"不同母亲的子女组"之间的距离，
    # 跟夫妻卡片贴不贴没有关系。曾经两者共用一个参数，
    # 把夫妻间距调成 0 就会让两个母亲的子女组也粘在一起分不清。
    cols_total = sum(active) + p.sibling_gap * max(0, len(active) - 1)

    node.col_w = col_w
    node.unit_w = max(node.row_w, cols_total)
    node.subtree_w = node.unit_w
    return node.subtree_w


def _place(
    node: _Node,
    left: float,
    p: LayoutParams,
    group_by_mother: bool = False,
) -> None:
    """前序：把子节点放到父节点下方。

    默认是把所有子女整体居中在父母单元之下；
    开了 `group_by_mother` 后，改为**每一列各自居中**——
    各列依次排开后再整体居中于单元，同母的兄弟姐妹自然成一组。
    """
    node.cx = left + node.subtree_w / 2.0

    if not node.children:
        return

    if group_by_mother and node.col_w:
        active = [w for w in node.col_w if w > 0]
        cols_total = sum(active) + p.sibling_gap * max(0, len(active) - 1)

        # 列整体居中于单元，列内内容居中于列
        node.col_x = []
        x = left + (node.unit_w - cols_total) / 2.0
        for w in node.col_w:
            if w <= 0:
                node.col_x.append(x)
                continue
            node.col_x.append(x + w / 2.0)
            x += w + p.sibling_gap

        buckets: list[list[_Node]] = [[] for _ in node.col_w]
        for child in node.children:
            buckets[child.mother_col].append(child)

        for idx, kids in enumerate(buckets):
            if not kids:
                continue
            total = sum(c.subtree_w for c in kids) + p.sibling_gap * (len(kids) - 1)
            cursor = node.col_x[idx] - total / 2.0
            for child in kids:
                _place(child, cursor, p, group_by_mother)
                cursor += child.subtree_w + p.sibling_gap
        return

    total = sum(c.subtree_w for c in node.children) + p.sibling_gap * (len(node.children) - 1)
    cursor = left + (node.subtree_w - total) / 2.0
    for child in node.children:
        _place(child, cursor, p, group_by_mother)
        cursor += child.subtree_w + p.sibling_gap


def _resolve_generations(
    persons: Sequence[PersonLite],
    unions: Sequence[UnionLite],
    links: Sequence[ChildLink],
) -> dict[int, int]:
    """补齐缺失的世代：没有 generation 的，取其父的 +1。"""
    gen: dict[int, int] = {x.id: x.generation for x in persons if x.generation}  # type: ignore[misc]

    un_tmp = {u.id: u for u in unions}
    parent_of: dict[int, int] = {}
    for lk in sorted(links, key=lambda x: -x.is_primary):
        if lk.is_primary != 1 or lk.child_id in parent_of:
            continue
        u = un_tmp.get(lk.union_id)
        if u is not None:
            parent_of[lk.child_id] = u.partner_a_id

    ids = {x.id for x in persons}
    changing = True
    guard = 0
    while changing and guard < 64:
        changing = False
        guard += 1
        for pid in ids:
            if pid in gen:
                continue
            father = parent_of.get(pid)
            if father is not None and father in gen:
                gen[pid] = gen[father] + 1
                changing = True

    for pid in ids:
        gen.setdefault(pid, 1)
    return gen


def _child_seq(links: Sequence[ChildLink], parent_id: int, child_id: int) -> int:
    for lk in links:
        if lk.child_id == child_id:
            return lk.seq
    return 9999


def _is_ancestor(nodes: dict[int, _Node], suspect: _Node, target: _Node) -> bool:
    """suspect 是否是 target 的祖先（用于数据有环时防死循环）。"""
    seen: set[int] = set()
    cur: _Node | None = target
    while cur is not None and cur.person_id not in seen:
        if cur is suspect:
            return True
        seen.add(cur.person_id)
        cur = cur.parent
    return False


def _tag_union(
    d: dict[str, Any],
    pair_index: dict[frozenset[int], UnionLite],
    a: int,
    b: int,
) -> None:
    """给配偶节点挂上「第几任 / 什么类型 / 有没有结束」。

    端上据此把卡片副标题写成「元配」「继配」「已离异」——
    不需要它自己再发一次请求，也不需要在端上复刻婚姻规则。
    """
    u = pair_index.get(frozenset((a, b)))
    if u is None:
        return
    d["unionSeq"] = u.union_seq
    d["unionType"] = u.union_type
    d["endReason"] = u.end_reason
    d["unionEnded"] = u.end_date is not None or u.end_reason is not None


def _node_dict(
    person: PersonLite,
    x: float,
    y: float,
    p: LayoutParams,
    *,
    gen: int,
    is_spouse: bool,
    spouse_of: int | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": person.id,
        "x": round(x),
        "y": round(y),
        "gen": gen,
        "w": p.node_width,
        "h": p.node_height,
        "gender": person.gender,
        "role": person.lineage_role,
        "living": person.living_status,
        "isSpouse": is_spouse,
    }
    if spouse_of is not None:
        d["spouseOf"] = spouse_of
    return d


def _build_edges(
    nodes: dict[int, _Node],
    child_parent: dict[int, int],
    links: Sequence[ChildLink],
    pair_index: dict[frozenset[int], UnionLite],
    role_of: dict[int, int],
    child_mother: dict[int, int | None] | None = None,
    visible_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    from app.core.constants import EdgeType, LineageRole

    edges: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    mothers = child_mother or {}

    external_roles = (int(LineageRole.SPOUSE_OUT), int(LineageRole.EXTERNAL_DESC))

    # 父子边：过继（relation=2）用虚线；外姓分支（女婿/外孙）用淡虚线
    rel_of: dict[int, int] = {}
    for lk in links:
        if lk.is_primary == 1:
            rel_of.setdefault(lk.child_id, lk.relation)

    for child_id, parent_id in child_parent.items():
        relation = rel_of.get(child_id, 1)
        if role_of.get(child_id) in external_roles:
            etype = int(EdgeType.EXTERNAL)
        elif relation == 2:
            etype = int(EdgeType.ADOPTED)
        else:
            etype = int(EdgeType.PARENT_CHILD)
        key = (parent_id, child_id, etype)
        if key in seen:
            continue
        seen.add(key)
        edge: dict[str, Any] = {"from": parent_id, "to": child_id, "type": etype}
        # 生母在同一屏里时带上她：端上把亲子线画到母亲正下方，
        # 而不是从父亲/父母中点斜拉过去——多妻时那条线会横穿别人的卡片。
        # 注意不能查 `nodes`：配偶被并进了丈夫的布局单元，本身不是一个 node。
        mid = mothers.get(child_id)
        if mid is not None and (visible_ids is None or mid in visible_ids):
            edge["motherId"] = mid
        edges.append(edge)

    # 婚姻边
    for node in nodes.values():
        for sid in node.spouse_ids:
            u = pair_index.get(frozenset((node.person_id, sid)))
            etype = int(EdgeType.MARRIAGE)
            key = (node.person_id, sid, etype)
            if key in seen:
                continue
            seen.add(key)
            edge = {
                "from": node.person_id,
                "to": sid,
                "type": etype,
                "unionType": u.union_type if u else 1,
            }
            if u is not None:
                edge["unionId"] = u.id
                edge["unionSeq"] = u.union_seq
                # 已结束的婚姻（离异/丧偶）端上画虚线。**人还在图上**——
                # 隐藏离异配偶等于让子女在自己那一屏看不到母亲。
                edge["ended"] = u.end_date is not None or u.end_reason is not None
                edge["endReason"] = u.end_reason
            edges.append(edge)

    return edges


def _empty_layout(p: LayoutParams) -> dict[str, Any]:
    return {
        "nodes": [],
        "edges": [],
        "canvas": {
            "width": p.padding * 2,
            "height": p.padding * 2,
            "nodeWidth": p.node_width,
            "nodeHeight": p.node_height,
            "genGap": p.gen_gap,
            "siblingGap": p.sibling_gap,
            "spouseGap": p.spouse_gap,
            "minGeneration": 1,
            "maxGeneration": 1,
        },
    }
