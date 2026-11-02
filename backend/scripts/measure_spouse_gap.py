"""量一下真实数据里"夫妻之间到底隔多远"（只读，不改任何数据）。

    cd backend
    .venv\\Scripts\\python.exe scripts/measure_spouse_gap.py

输出的 gap = 配偶卡片左边 - 本人卡片右边，单位 px。
布局参数 node_width=88 时，12 就是 spouse_gap，>12 说明被"按生母分列"撑开了。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.family import Family
from app.models.person import Person
from app.services.scope_service import RelationGraph
from app.services.tree_layout import LayoutParams, compute_layout
from app.services.tree_service import _compute_layout, _scope_mine, _scope_overview


def dump(layout: dict, label: str) -> None:
    by_id = {n["id"]: n for n in layout["nodes"]}
    print(f"\n=== {label} === 画布 {layout['canvas']['width']}px  节点 {len(layout['nodes'])}")

    pairs = []
    for n in layout["nodes"]:
        sp_of = n.get("spouseOf")
        if sp_of is None or sp_of not in by_id:
            continue
        a = by_id[sp_of]
        gap = n["x"] - (a["x"] + a["w"])
        pairs.append((gap, sp_of, n["id"]))

    pairs.sort(reverse=True)
    for gap, a, b in pairs[:12]:
        print(f"  间距 {gap:>4}px  {a:>3} ↔ {b:>3}   (x {by_id[a]['x']:>4} / {by_id[b]['x']:>4})")
    if pairs:
        gaps = [g for g, _, _ in pairs]
        print(f"  —— 共 {len(gaps)} 对，最小 {min(gaps)}，最大 {max(gaps)}，"
              f"平均 {sum(gaps) / len(gaps):.1f}")


def main() -> None:
    db = SessionLocal()
    try:
        family = db.execute(select(Family).limit(1)).scalar_one()
        graph = RelationGraph.load(db, family.id)
        print(f"家族 {family.name} 人数 {len(graph.person_of)}")

        ids = _scope_overview(graph)
        dump(_compute_layout(graph, ids, group_by_mother=False), "总览 overview")

        for anchor in (6, 14, 30):
            if anchor not in graph.person_of:
                continue
            ids, _ = _scope_mine(graph, anchor, 99)
            p = graph.person_of[anchor]
            dump(_compute_layout(graph, ids, group_by_mother=True),
                 f"我的一家 mine anchor={anchor} {p.name}  ({len(ids)} 人)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
