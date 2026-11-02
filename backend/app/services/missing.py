"""待补全清单——「接力修谱」的引擎。

## 为什么这是整个产品最关键的接口

行业公认的失败模式是：录入的永远只有两三个人，其他人看一眼就走。
根因是**让用户面对一张空白表单**。

这个模块换一个角度：不问"请完善您的信息"，而是先算出"这个人周围还缺什么"，
然后一次只问一个具体的问题——"您母亲的名字是？"

配套的两个设计要点：
1. `displayBefore` —— 让用户看到"配李氏"即将变成"李秀英"。
   这个前后对比是产品最强的情绪点：不是数据录入，是把一个被历史省略的人写回来。
2. `reason` —— 说明为什么问这个。用户不反感被问，反感的是莫名其妙被问。

老谱里每一处"配某氏"都会在这里变成一条待办任务——
导入之后系统会自动产出几十上百条具体任务，这是冷启动最有力的第一批弹药。
"""

from __future__ import annotations

from typing import Any

from app.core.constants import (
    Gender,
    LineageRole,
    LivingStatus,
    MaritalStatus,
    MissingTaskType,
    ParentRelation,
)
from app.services.scope_service import RelationGraph


def build_missing_tasks(
    graph: RelationGraph,
    person_id: int,
    *,
    include_external: bool = True,
    max_items: int = 3,
) -> dict[str, Any]:
    """生成待补全清单。

    一次最多返回 max_items 条（默认 3）。
    清单不能一次给 20 条——那是待办列表，不是引导。
    """
    person = graph.person_of.get(person_id)
    if person is None:
        return {"personId": person_id, "missingCount": 0, "items": []}

    all_items: list[dict[str, Any]] = []

    all_items.extend(_check_self_name(person))
    all_items.extend(_check_parents(graph, person))
    all_items.extend(_check_spouse(graph, person))
    all_items.extend(_check_children(graph, person))
    if include_external:
        all_items.extend(_check_external(graph, person))
    all_items.extend(_check_dates(person))

    # 排序：先补最直接的缺口（这个人自己还没名字），再问最近的亲属，最后问更远的
    order = {
        "self_name": 0,
        "father": 0,
        "mother_name": 1,
        "spouse": 2,
        "children": 3,
        "external": 4,
    }
    all_items.sort(key=lambda x: (order.get(x["key"], 9), x.get("_sort", 0)))

    for item in all_items:
        item.pop("_sort", None)

    return {
        "personId": person_id,
        "displayName": _display(person),
        "missingCount": len(all_items),
        "items": all_items[:max_items],
        "remainingCount": max(0, len(all_items) - max_items),
        "nextAfterComplete": {
            "hint": "填完后，系统会继续检查其他亲属的信息"
        } if len(all_items) > max_items else None,
    }


# ============================================================
# 各项检查
# ============================================================


def _check_self_name(person: Any) -> list[dict[str, Any]]:
    """本人姓名缺失——老谱里成片存在的「配某氏」。

    为什么必须有这一条，而不只靠 _check_parents 里的 mother_name：
        mother_name 是从**子女视角**触发的，而 graph.mother_of 只认
        is_primary=1 的那条亲子关系。过继场景下本生母会被嗣母挤掉，
        于是「配某氏」恰恰在最该出现的地方消失了。没有子女的无名者
        更是永远不会被问到。
    所以补一条按节点自身触发的检查：只要 name 为空，它自己就是一条待办。
    族管理员拿着老谱逐页核对时，走的就是这条路径。
    """
    if person.name:
        return []

    shown = _display(person)
    if person.gender == int(Gender.FEMALE):
        pronoun = "她"
    elif person.gender == int(Gender.MALE):
        pronoun = "他"
    else:
        pronoun = "这位先人"

    return [
        {
            "key": "self_name",
            "label": "姓名",
            "question": f"家谱上只写了「{shown}」，{pronoun}的名字是？",
            "type": MissingTaskType.UPDATE_FIELD.value,
            "targetTable": "person",
            "targetId": person.id,
            "field": "name",
            "prefill": {"surname": person.surname},
            "displayBefore": shown,
            "hint": f"补上名字后，{pronoun}就不再是「{shown}」",
            "reason": "家谱仅有姓氏，缺姓名",
            "_sort": -1,
        }
    ]


def _check_parents(graph: RelationGraph, person: Any) -> list[dict[str, Any]]:
    """父母——**只在本人是本族世系节点时才问**。

    媳妇（3）/ 女婿（4）/ 外姓后代（5）的父母记在别人家的谱上，
    本族对他们的写法就是「配某氏」四个字。没有这道守卫，每一位嫁入的
    媳妇都会永久挂一条「您父亲叫什么名字？」：

    - 答了 → 把外姓父亲挂进本族世系，全族树里凭空长出一支外姓人；
    - 跳过 → 任务不落库，下次进来照旧问。

    两条路都走不通，于是"还差一项"永远消不掉——启明点李秀英（媳妇，
    第 3 世）撞上的就是这个：卡片说还差一项，点进去问的是她父亲。
    """
    if person.lineage_role in (
        int(LineageRole.SPOUSE_IN),
        int(LineageRole.SPOUSE_OUT),
        int(LineageRole.EXTERNAL_DESC),
    ):
        return []

    items: list[dict[str, Any]] = []
    pid = person.id

    father_id = graph.father_of.get(pid)
    mother_id = graph.mother_of.get(pid)

    # 父亲缺失
    if father_id is None and graph.person_of.get(pid) is not None and person.generation != 1:
        items.append(
            {
                "key": "father",
                "label": "您的父亲",
                "question": "您父亲叫什么名字？",
                "type": MissingTaskType.CREATE_PERSON.value,
                "prefill": {
                    "surname": person.surname,
                    "generation": (person.generation - 1) if person.generation else None,
                    "linkAs": "father_of",
                    "childId": pid,
                },
                "required": ["name"],
                "optional": ["birthDate", "livingStatus", "burialPlace"],
                "reason": "家谱中尚未记录您父亲",
                "_sort": 0,
            }
        )

    # 母亲姓名缺失（老谱最常见：只记"配李氏"）
    if mother_id is not None:
        mother = graph.person_of.get(mother_id)
        if mother is not None and not mother.name:
            items.append(
                {
                    "key": "mother_name",
                    "label": "您母亲的名字",
                    "question": "您母亲的名字是？",
                    "type": MissingTaskType.UPDATE_FIELD.value,
                    "targetTable": "person",
                    "targetId": mother_id,
                    "field": "name",
                    "prefill": {"surname": mother.surname},
                    "displayBefore": _display(mother),
                    "hint": "老谱上只记了姓，补上名字后她就不再是「"
                            + _display(mother)
                            + "」",
                    "reason": "家谱中仅有姓氏，缺姓名",
                    "_sort": 0,
                }
            )
    elif father_id is not None and person.generation and person.generation > 1:
        # 父在但完全没有母亲的记录
        items.append(
            {
                "key": "mother",
                "label": "您的母亲",
                "question": "您母亲叫什么名字？",
                "type": MissingTaskType.CREATE_PERSON.value,
                "prefill": {
                    "gender": 2,
                    "generation": person.generation - 1,
                    "linkAs": "spouse_of",
                    "partnerId": father_id,
                },
                "required": ["name"],
                "optional": ["birthDate", "livingStatus"],
                "reason": "家谱中尚未记录您母亲",
                "_sort": 1,
            }
        )

    return items


def _check_spouse(graph: RelationGraph, person: Any) -> list[dict[str, Any]]:
    """在世成年但没有任何婚姻记录。

    已婚是常态、未婚也是常态。所以这一项必须能回答"没有"——
    原来它不可跳过，未婚的人被永久卡住（进度条停在 96% 下不来），
    而家谱里"未婚"本来就该是一句正经记载，不是一条填不完的待办。
    """
    if person.living_status != int(LivingStatus.LIVING):
        return []
    if graph.spouses_of.get(person.id):
        return []
    # 本人已经明确答过"未婚"——不再问，也不再算缺口
    if person.marital_status == int(MaritalStatus.SINGLE):
        return []

    # 用出生年做一次粗糙的成年判断，避免问未成年的孩子要不要填配偶
    if person.birth_date is not None and person.birth_date.year > 2008:
        return []

    return [
        {
            "key": "spouse",
            "label": "您的配偶",
            "question": "您的配偶叫什么名字？",
            "type": MissingTaskType.CREATE_UNION.value,
            "prefill": {"partnerAId": person.id, "unionType": 1},
            "required": ["partnerName"],
            "optional": ["marriageDate"],
            "reason": "家谱中尚未记录您的婚姻",
            # 跳过 = 明确回答"没有"，不是"以后再说"。
            # 两者的差别在于落不落库：不落库的跳过，下次进来照旧问，
            # 用户会觉得"我说了没有，它还在问"，比不给按钮更烦。
            "skippable": True,
            "skipLabel": "未婚 / 暂无配偶",
            "skipHint": "记为「未婚」，之后不再追问",
            "_sort": 0,
        }
    ]


def _check_children(graph: RelationGraph, person: Any) -> list[dict[str, Any]]:
    """有婚姻但没登记子女。

    这一项语气要留有余地——没孩子是常态，不能让人感觉是在催生。
    """
    if person.living_status != int(LivingStatus.LIVING):
        return []
    if not graph.spouses_of.get(person.id):
        return []
    if graph.children_of.get(person.id):
        return []
    # 本人已经明确答过"没有子女"
    if person.childless:
        return []

    return [
        {
            "key": "children",
            "label": "您的子女",
            "question": "您有子女吗？",
            "type": MissingTaskType.CREATE_PERSON.value,
            "prefill": {
                "surname": person.surname,
                "generation": (person.generation + 1) if person.generation else None,
                "linkAs": "child_of",
                "parentId": person.id,
            },
            "required": [],
            "optional": ["name", "gender", "birthDate"],
            "skippable": True,
            "skipLabel": "暂无子女",
            "skipHint": "记为「暂无子女」，之后不再追问",
            "reason": "家谱中尚未记录您的子女",
            "_sort": 0,
        }
    ]


def _check_external(graph: RelationGraph, person: Any) -> list[dict[str, Any]]:
    """外孙信息（决策第 2 条：是否入谱由用户决定，我们只提供功能支持）。

    **这是唯一一类"回答没有"却没地方落库的任务**，原来因此成了永久缺口：
    跳过只是本地跳过，下次进来照旧问，陈启明的完成度永远卡在 0.8。
    现在答「暂不记外孙」会落 `person.external_opt_out=1`——记的是
    **一个决定**（"我这一支的外孙不入谱"），不是一个"跳过"标记。
    这和「未婚」是同一个道理：老谱上"外孙不列"本身就是一句记载。

    粒度是**提问人（父/母）**，不是逐个女儿：家谱里外孙入不入谱
    向来讲的是一支的规矩，不按"这个女儿记、那个女儿不记"来定。

    与女儿本人答案的联动：她名下已有子女记录、或她本人声明过
    「暂无子女」（childless=1），父母这边就不再问——事实已经确认。
    「未婚」不算：未婚的人也可能有子女，老谱里"未娶有子"并不罕见。
    """
    if person.external_opt_out:
        return []

    items: list[dict[str, Any]] = []

    # 找出该人已出嫁/在外的女儿
    for child_id in graph.children_of.get(person.id, []):
        child = graph.person_of.get(child_id)
        if child is None:
            continue
        if child.lineage_role != int(LineageRole.FEMALE):
            continue
        if graph.children_of.get(child_id):
            continue   # 已经有外孙记录
        # 女儿本人已答「暂无子女」——事实已经确认，不必再由父母代答。
        # 没有这条，就会出现「女儿说了没有，爸妈那边还挂着一项」，
        # 与 67% / 底部条是同一类病：同一件事两个出口各算各的。
        # 注意「未婚」不能触发这条：未婚 ≠ 无子女，这一问仍然要问。
        if child.childless:
            continue
        items.append(
            {
                "key": "external",
                "label": f"{_display(child)}的子女",
                "question": f"{_display(child)}有子女吗？要记到家谱里吗？",
                "type": MissingTaskType.CREATE_PERSON.value,
                "prefill": {
                    "gender": None,
                    "generation": (child.generation + 1) if child.generation else None,
                    "lineageRole": int(LineageRole.EXTERNAL_DESC),
                    "linkAs": "child_of",
                    "parentId": child_id,
                },
                "required": [],
                "optional": ["name", "gender", "birthDate"],
                "skippable": True,
                "skipLabel": "暂不记外孙",
                "skipHint": "记为「外孙不入谱」，之后不再问",
                "reason": "外孙默认不进全族世系，只显示在「我的一家」里。填不填由您决定",
                "_sort": 0,
            }
        )
        if len(items) >= 2:
            break

    return items


def _check_dates(person: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    if person.living_status == int(LivingStatus.DECEASED):
        if person.death_date is None:
            items.append(
                {
                    "key": "death_date",
                    "label": "去世时间",
                    "question": f"{_display(person)}是哪一年去世的？",
                    "type": MissingTaskType.UPDATE_FIELD.value,
                    "targetTable": "person",
                    "targetId": person.id,
                    "field": "deathDate",
                    "required": [],
                    "optional": ["deathDatePrecision"],
                    "skippable": True,
                    "reason": "家谱中缺少去世时间",
                    "_sort": 0,
                }
            )
        if not person.burial_place:
            items.append(
                {
                    "key": "burial_place",
                    "label": "葬地",
                    "question": f"{_display(person)}葬在哪里？",
                    "type": MissingTaskType.UPDATE_FIELD.value,
                    "targetTable": "person",
                    "targetId": person.id,
                    "field": "burialPlace",
                    "required": [],
                    "skippable": True,
                    "reason": "家谱中缺少葬地记载",
                    "_sort": 1,
                }
            )
    elif person.living_status == int(LivingStatus.LIVING) and person.birth_date is None:
        items.append(
            {
                "key": "birth_date",
                "label": "出生时间",
                "question": "您是哪一年出生的？",
                "type": MissingTaskType.UPDATE_FIELD.value,
                "targetTable": "person",
                "targetId": person.id,
                "field": "birthDate",
                "required": [],
                "optional": ["birthDatePrecision"],
                "skippable": True,
                "reason": "家谱中缺少出生时间",
                "_sort": 2,
            }
        )

    return items


# ============================================================
# 辅助
# ============================================================


def _display(person: Any) -> str:
    if person.name:
        return person.name
    if person.surname:
        return f"{person.surname}氏"
    return "配某氏"


def count_missing_for_family(graph: RelationGraph) -> dict[str, int]:
    """全族数据完整度统计。用于族首页那条进度条——它是产品最重要的运营位。"""
    total = len(graph.person_of)
    missing_spouse = 0
    missing_mother = 0
    missing_birth = 0
    missing_burial = 0

    for pid, person in graph.person_of.items():
        if person.living_status == int(LivingStatus.LIVING):
            # 已声明婚姻状态（未婚/已婚/丧偶/离异）就不算"缺配偶"。
            # 不加这一条，点了「未婚」的人在族级统计里仍然被当成缺一项。
            known_marital = person.marital_status is not None
            if (
                not known_marital
                and not graph.spouses_of.get(pid)
                and person.birth_date
                and person.birth_date.year <= 2008
            ):
                missing_spouse += 1
            if person.birth_date is None:
                missing_birth += 1
        else:
            if person.burial_place is None:
                missing_burial += 1

        mid = graph.mother_of.get(pid)
        if mid is not None:
            mother = graph.person_of.get(mid)
            if mother is not None and not mother.name:
                missing_mother += 1
        elif graph.father_of.get(pid) is not None and (person.generation or 1) > 1:
            missing_mother += 1

    fields_per_person = 6
    holes = missing_spouse + missing_mother + missing_birth + missing_burial
    completeness = 1.0 if total == 0 else max(0.0, 1.0 - holes / (total * fields_per_person))

    return {
        "personCount": total,
        "missingSpouseCount": missing_spouse,
        "missingMotherCount": missing_mother,
        "missingBirthCount": missing_birth,
        "missingBurialCount": missing_burial,
        "completeness": round(completeness, 4),
    }
