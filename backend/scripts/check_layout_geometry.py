"""布局几何自检：卡片之间不许重叠，夫妻必须零间距（只读）。

    cd backend
    .venv\\Scripts\\python.exe scripts/check_layout_geometry.py

检查项：
  1. 同一世代的卡片矩形两两不重叠（夫妻贴边算 0，不算重叠）
  2. 每对夫妻的间距 == spouse_gap（当前 0）
  3. 每个孩子都落在父母单元横向范围内（不偏出）
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.family import Family
from app.services.scope_service import RelationGraph
from app.services.tree_service import (
    _compute_layout,
    _scope_mine,
    _scope_overview,
)

SPOUSE_GAP = 0


def check(layout: dict, label: str) -> int:
    problems = 0
    by_id = {n["id"]: n for n in layout["nodes"]}

    # 1. 同代重叠
    gens: dict[int, list[dict]] = {}
    for n in layout["nodes"]:
        gens.setdefault(n["gen"], []).append(n)
    for gen, row in gens.items():
        row.sort(key=lambda n: n["x"])
        for a, b in zip(row, row[1:]):
            if b["x"] < a["x"] + a["w"] - 0.5:
                problems += 1
                print(f"  ✗ {label} {gen}世 卡片重叠 {a['id']}@{a['x']} 与 {b['id']}@{b['x']}")

    # 2. 夫妻间距
    for n in layout["nodes"]:
        sp = n.get("spouseOf")
        if sp is None or sp not in by_id:
            continue
        a = by_id[sp]
        gap = n["x"] - (a["x"] + a["w"])
        if abs(gap - SPOUSE_GAP) > 0.5:
            problems += 1
            print(f"  ✗ {label} 夫妻 {sp}↔{n['id']} 间距 {gap}（应为 {SPOUSE_GAP}）")

    # 3. 孩子横向离父母单元太远（超过一屏宽的一半）说明排歪了
    for e in layout["edges"]:
        if e["type"] == 2:
            continue
        a = by_id.get(e["from"])
        b = by_id.get(e["to"])
        mo = by_id.get(e.get("motherId"))
        if not a or not b:
            continue
        lo = min(a["x"], mo["x"] if mo else a["x"])
        hi = max(a["x"] + a["w"], (mo["x"] + mo["w"]) if mo else a["x"] + a["w"])
        cx = b["x"] + b["w"] / 2
        off = 0 if lo <= cx <= hi else min(abs(cx - lo), abs(cx - hi))
        if off > 400:
            problems += 1
            print(f"  ✗ {label} 孩子 {b['id']} 偏离父母单元 {off:.0f}px")

    n_nodes = len(layout["nodes"])
    print(f"  {'✓' if problems == 0 else '✗'} {label}: {n_nodes} 节点，{problems} 处问题")
    return problems


def main() -> None:
    db = SessionLocal()
    total = 0
    try:
        family = db.execute(select(Family).limit(1)).scalar_one()
        graph = RelationGraph.load(db, family.id)

        total += check(_compute_layout(graph, _scope_overview(graph)), "总览")

        from app.models.person import Person

        anchors = [
            p.id
            for p in db.execute(
                select(Person).where(Person.family_id == family.id, Person.status == 1)
            ).scalars()
        ]
        for aid in anchors:
            ids, _ = _scope_mine(graph, aid, 99)
            p = graph.person_of[aid]
            total += check(_compute_layout(graph, ids, group_by_mother=True),
                           f"mine/{aid} {p.name or '配某氏'}")
    finally:
        db.close()

    print("\n" + ("✅ 几何自检全部通过" if total == 0 else f"❌ 共 {total} 处问题"))
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
