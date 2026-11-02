"""树布局算法演示（不需要数据库）。

直接运行就能看到布局结果，用来验证算法并与前端确认视觉效果：

    cd backend
    .venv\\Scripts\\python.exe scripts/demo_layout.py

造的数据刻意覆盖了几个难缠的场景：
  - 多子女的房（长房三子）
  - "配某氏"——只知道姓，没有名字（老谱常态）
  - 过继（同一人挂两条 parent_child）
  - 外孙（女儿的孩子，决策第 2 条：只在"我的一家"显示）
  - 多配偶
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.constants import (
    EdgeType,
    LineageRole,
    LivingStatus,
    ParentRelation,
    UnionType,
)
from app.services.mask import Viewer, cn_number, mask_name, project_person
from app.services.tree_layout import (
    ChildLink,
    LayoutParams,
    PersonLite,
    UnionLite,
    compute_layout,
)

# ============================================================
# 演示数据：广信陈氏（六代，约 20 人）
# ============================================================

P = PersonLite

PERSONS = [
    # ---- 第 1 世：始迁祖 ----
    P(id=1, name="陈德昌", surname="陈", gender=1, generation=1, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.DECEASED, is_lineage_carrier=1),
    P(id=2, name="王秀英", surname="王", gender=2, generation=1, lineage_role=LineageRole.SPOUSE_IN,
      living_status=LivingStatus.DECEASED),

    # ---- 第 2 世：长房 / 二房 ----
    P(id=10, name="陈文山", surname="陈", gender=1, generation=2, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.DECEASED, is_lineage_carrier=1),
    P(id=11, name="李桂芳", surname="李", gender=2, generation=2, lineage_role=LineageRole.SPOUSE_IN,
      living_status=LivingStatus.DECEASED),
    P(id=12, name="陈文海", surname="陈", gender=1, generation=2, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.DECEASED, is_lineage_carrier=1),
    # 这一位就是老谱里的"配某氏"：只知道姓，不知道名字
    P(id=13, name=None, surname="张", gender=2, generation=2, lineage_role=LineageRole.SPOUSE_IN,
      living_status=LivingStatus.DECEASED),

    # ---- 第 3 世 ----
    P(id=20, name="陈国栋", surname="陈", gender=1, generation=3, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.LIVING, is_lineage_carrier=1),
    P(id=21, name="李秀英", surname="李", gender=2, generation=3, lineage_role=LineageRole.SPOUSE_IN,
      living_status=LivingStatus.LIVING),
    P(id=22, name="陈国梁", surname="陈", gender=1, generation=3, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.LIVING, is_lineage_carrier=1),
    P(id=23, name="周小燕", surname="周", gender=2, generation=3, lineage_role=LineageRole.SPOUSE_IN,
      living_status=LivingStatus.LIVING),
    # 陈国华：文海之子，同时也是文山的嗣子（过继场景）
    P(id=24, name="陈国华", surname="陈", gender=1, generation=3, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.LIVING, is_lineage_carrier=1),

    # ---- 第 4 世 ----
    P(id=30, name="陈启明", surname="陈", gender=1, generation=4, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.LIVING, is_lineage_carrier=1),
    P(id=31, name="吴敏", surname="吴", gender=2, generation=4, lineage_role=LineageRole.SPOUSE_IN,
      living_status=LivingStatus.LIVING),
    P(id=32, name="陈启华", surname="陈", gender=1, generation=4, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.LIVING, is_lineage_carrier=1),
    P(id=33, name="陈雨欣", surname="陈", gender=2, generation=4, lineage_role=LineageRole.FEMALE,
      living_status=LivingStatus.LIVING),
    # 女婿（外姓，虚线框）
    P(id=34, name="林志强", surname="林", gender=1, generation=4, lineage_role=LineageRole.SPOUSE_OUT,
      living_status=LivingStatus.LIVING),

    # ---- 第 5 世 ----
    P(id=40, name="陈思远", surname="陈", gender=1, generation=5, lineage_role=LineageRole.MALE,
      living_status=LivingStatus.LIVING, is_lineage_carrier=1),
    P(id=41, name="陈思彤", surname="陈", gender=2, generation=5, lineage_role=LineageRole.FEMALE,
      living_status=LivingStatus.LIVING),
    # 外孙（决策第 2 条：只在"我的一家"显示，不进全族树）
    P(id=42, name="林子轩", surname="林", gender=1, generation=5,
      lineage_role=LineageRole.EXTERNAL_DESC, living_status=LivingStatus.LIVING),
]

UNIONS = [
    UnionLite(id=100, partner_a_id=1, partner_b_id=2, union_type=UnionType.FIRST_MARRIAGE),
    UnionLite(id=101, partner_a_id=10, partner_b_id=11, union_type=UnionType.FIRST_MARRIAGE),
    UnionLite(id=102, partner_a_id=12, partner_b_id=13, union_type=UnionType.FIRST_MARRIAGE),
    UnionLite(id=103, partner_a_id=20, partner_b_id=21, union_type=UnionType.FIRST_MARRIAGE),
    UnionLite(id=104, partner_a_id=22, partner_b_id=23, union_type=UnionType.FIRST_MARRIAGE),
    # 招赘：女儿陈雨欣与林志强，孩子随母入本族谱（这里林志强是 partner_b）
    UnionLite(id=105, partner_a_id=33, partner_b_id=34, union_type=UnionType.UXORILOCAL_OUT),
    UnionLite(id=106, partner_a_id=30, partner_b_id=31, union_type=UnionType.FIRST_MARRIAGE),
]

LINKS = [
    ChildLink(union_id=100, child_id=10, relation=ParentRelation.BIOLOGICAL, seq=1),
    ChildLink(union_id=100, child_id=12, relation=ParentRelation.BIOLOGICAL, seq=2),
    ChildLink(union_id=101, child_id=20, relation=ParentRelation.BIOLOGICAL, seq=1),
    ChildLink(union_id=102, child_id=24, relation=ParentRelation.BIOLOGICAL, seq=1, is_primary=0),
    # 过继：国华同时是文山的嗣子（is_primary=1，决定他显示在谁下面）
    ChildLink(union_id=101, child_id=24, relation=ParentRelation.ADOPTED, seq=2, is_primary=1),
    ChildLink(union_id=103, child_id=30, relation=ParentRelation.BIOLOGICAL, seq=1),
    ChildLink(union_id=103, child_id=32, relation=ParentRelation.BIOLOGICAL, seq=2),
    ChildLink(union_id=103, child_id=33, relation=ParentRelation.BIOLOGICAL, seq=3),
    ChildLink(union_id=104, child_id=40, relation=ParentRelation.BIOLOGICAL, seq=1),
    ChildLink(union_id=105, child_id=42, relation=ParentRelation.BIOLOGICAL, seq=1),
    ChildLink(union_id=106, child_id=41, relation=ParentRelation.BIOLOGICAL, seq=1),
]

NAMES = {p.id: (p.name or (f"{p.surname}氏" if p.surname else "配某氏")) for p in PERSONS}


# ============================================================
# 渲染
# ============================================================


def render(layout: dict, width: int = 104) -> None:
    canvas = layout["canvas"]
    nodes = layout["nodes"]
    scale = (width - 8) / max(canvas["width"], 1)

    by_gen: dict[int, list[tuple[int, int]]] = {}
    for n in nodes:
        col = int(n["x"] * scale)
        by_gen.setdefault(n["gen"], []).append((col, n["id"]))

    print()
    for gen in sorted(by_gen):
        line = [" "] * (width + 12)
        for col, pid in sorted(by_gen[gen]):
            label = NAMES.get(pid, str(pid))
            if label == "陈启明":
                label = "【启明】"
            pos = col
            for ch in label:
                if pos < len(line):
                    line[pos] = ch
                    pos += 1
        print(f"{gen:>2}世 │" + "".join(line).rstrip())
        print("    │")


def main() -> None:
    print("=" * 108)
    print("  饶信谱脉 · 树布局算法验证")
    print("=" * 108)

    params = LayoutParams()
    layout = compute_layout(PERSONS, UNIONS, LINKS, params)

    c = layout["canvas"]
    print(f"\n画布 {c['width']} x {c['height']} px ｜ 节点 {c['nodeWidth']}x{c['nodeHeight']}"
          f" ｜ 世代 {c['minGeneration']}—{c['maxGeneration']}"
          f" ｜ 节点数 {len(layout['nodes'])} ｜ 边数 {len(layout['edges'])}")

    render(layout)

    print("\n" + "-" * 108)
    print("节点坐标（端上直接按 x/y 绝对定位，不做任何布局计算）")
    print("-" * 108)
    for n in sorted(layout["nodes"], key=lambda x: (x["gen"], x["x"])):
        name = NAMES.get(n["id"], "?")
        kind = "配偶" if n.get("isSpouse") else "本族"
        role_tag = {
            int(LineageRole.MALE): "男丁",
            int(LineageRole.FEMALE): "女儿",
            int(LineageRole.SPOUSE_IN): "媳妇",
            int(LineageRole.SPOUSE_OUT): "女婿",
            int(LineageRole.EXTERNAL_DESC): "外孙",
        }.get(n.get("role", 1), "?")
        print(f"  gen={n['gen']}  x={n['x']:>4}  y={n['y']:>4}  {name:<8} {role_tag:<4} {kind}")

    print("\n" + "-" * 108)
    print("边（1父子实线 2婚姻双线 3过继虚线 4外姓淡虚线）")
    print("-" * 108)
    for e in layout["edges"]:
        t = {
            int(EdgeType.PARENT_CHILD): "父子",
            int(EdgeType.MARRIAGE): "婚姻",
            int(EdgeType.ADOPTED): "过继",
            int(EdgeType.EXTERNAL): "外姓",
        }.get(e["type"], "?")
        print(f"  {NAMES.get(e['from'], e['from']):<8} → {NAMES.get(e['to'], e['to']):<8} [{t}]")

    # ---------- 脱敏验证 ----------
    print("\n" + "-" * 108)
    print("脱敏投影验证（决策第 4 条）")
    print("-" * 108)

    from types import SimpleNamespace

    class Row(SimpleNamespace):
        pass

    living = Row(
        id=20, family_id=1, branch_id=1, name="陈国栋", surname="陈", gender=1,
        generation=3, generation_hint=0, generation_name=None, lineage_role=1,
        is_lineage_carrier=1, courtesy_name=None, art_name=None, rank_in_family="长子",
        birth_date=__import__("datetime").date(1965, 3, 12), birth_date_precision=3,
        death_date=None, death_date_precision=0, living_status=1,
        burial_place=None, burial_note=None, native_place="广信区茶亭镇",
        education=None, occupation="教师", biography="一生务农，后任教于村小。",
        privacy_level=2, status=1, alt_family_id=None, source_type=2,
        source_note="本人填报", confidence=1, verify_status=1,
    )
    deceased = Row(**{**living.__dict__, "id": 10, "name": "陈文山",
                      "living_status": 2, "birth_date": __import__("datetime").date(1940, 5, 1),
                      "occupation": "农民", "biography": "生于民国二十九年，卒于二零一一年。"})

    from app.core.constants import ViewerScope

    # 姓名放开的分界线是「有没有认领」，不是「是不是近亲」（2026-10-16 口径）。
    # 所以第 3、4 行的区别只在**目标是不是近亲**：认领之后姓名两行都给全名，
    # 差别退到生日精度和职业/生平上。
    scenarios = [
        ("访客（未登录，从家族群链接进来）", Viewer(scope=ViewerScope.GUEST)),
        ("已入族但未认领（不知道自己是谁）",
         Viewer(scope=ViewerScope.LINEAGE, user_id=9, claimed_person_id=None)),
        ("已认领·目标在近亲圈内", Viewer(
            scope=ViewerScope.CLOSE, user_id=30, claimed_person_id=30,
            close_person_ids={20, 21, 30},
        )),
        ("已认领·目标是旁系（伯父那一支）", Viewer(
            scope=ViewerScope.CLOSE, user_id=31, claimed_person_id=31,
            close_person_ids={31, 32},
        )),
        ("族管理员", Viewer(scope=ViewerScope.ADMIN, user_id=1, role=3)),
    ]

    for scope_name, viewer in scenarios:
        a = project_person(living, viewer)
        b = project_person(deceased, viewer)
        print(f"\n  【{scope_name}】")
        print(f"    在世者 陈国栋 → 姓名 {a['name'] or a['displayName']}"
              f" ｜ 出生 {a['birth']['display'] if a['birth'] else '—'}"
              f" ｜ 职业 {a.get('occupation') or '（隐藏）'}")
        print(f"    已故者 陈文山 → 姓名 {b['name'] or b['displayName']}"
              f" ｜ 出生 {b['birth']['display'] if b['birth'] else '—'}"
              f" ｜ 职业 {b.get('occupation') or '（隐藏）'}")

    print("\n  姓名脱敏函数：", ", ".join(f"{x}→{mask_name(x)}" for x in ["陈启明", "陈明", "陈", "欧阳修"]))

    print("\n" + "=" * 108)
    print("  ✅ 布局算法与脱敏投影均可运行")
    print("=" * 108)


if __name__ == "__main__":
    main()
