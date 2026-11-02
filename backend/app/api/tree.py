"""家族树接口。

GET /tree/{familyId}?view=mine|lineage|overview&anchor={personId}&rev={cachedRev}
  -> 服务端用 Reingold-Tilford 算好坐标，返回 [{personId, x, y, gen}]，端上只画不排

这是整个小程序最核心的接口。参数差异全部收敛在这里，
端上三个视图调用同一个接口，只是 view 不同。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession, OptionalUser, resolve_context
from app.core.constants import TreeView
from app.services.mask import project_person_brief
from app.services.tree_service import get_tree as build_tree

router = APIRouter(prefix="/tree", tags=["tree"])


@router.get("/{family_id}", summary="家族树（三视图共用）")
async def get_tree(
    family_id: int,
    user: OptionalUser,
    db: DbSession,
    view: TreeView = Query(TreeView.MINE, description="mine 我的一家 / overview 全族总览（lineage 我的支系已于 2026-10-21 退休，仅为兼容保留）"),
    anchor: int | None = Query(None, description="view=mine 时的锚点 person_id"),
    rev: int | None = Query(None, description="客户端上次拿到的版本号，一致则返回 notModified"),
    detail: str = Query("full", description="full 带姓名 / lite 仅坐标（全族总览缩略图用）"),
    siblings: int | None = Query(
        None,
        ge=1,
        le=200,
        description="view=mine 时同代最多显示几个男丁节点（含自己）。不传取服务端默认 3，"
                    "端上点「还有 N 位」时传一个大值即可展开全部",
    ),
) -> dict:
    family, graph, viewer = resolve_context(db, user, family_id)

    # 未登录访客看全族总览时，把锚点固定到始祖，避免他们看到"别人的我的一家"
    if view == TreeView.OVERVIEW and anchor is None:
        anchor = None

    data = build_tree(
        db, family, graph, view, anchor, viewer,
        client_rev=rev,
        detail=detail,
        sibling_limit=siblings,
    )
    return {"code": 0, "msg": "ok", "data": data}


@router.get("/{family_id}/search", summary="按姓名搜人")
async def search_person(
    family_id: int,
    user: OptionalUser,
    db: DbSession,
    q: str = Query(..., min_length=1, description="姓名关键字"),
    gen: int | None = Query(None, description="限定世次"),
    branch_id: int | None = Query(None, description="限定房"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """搜人是一条"我在哪"的快捷路径。

    很多用户不知道自己的世次，但知道自己父亲或爷爷的名字——
    搜到父亲，再从父亲的页面找到自己。
    """
    family, graph, viewer = resolve_context(db, user, family_id)
    keyword = q.strip()

    hits = []
    for person in graph.person_of.values():
        if gen is not None and person.generation != gen:
            continue
        if branch_id is not None and person.branch_id != branch_id:
            continue

        name = person.name or ""
        surname = person.surname or ""
        if keyword not in name and keyword not in surname:
            continue

        brief = project_person_brief(person, viewer)
        if brief is None:
            continue
        brief["generationLabel"] = (
            f"第{_cn(person.generation)}世" if person.generation else None
        )
        hits.append(brief)

        if len(hits) >= limit:
            break

    hits.sort(key=lambda x: (x.get("generation") or 0, x["id"]))

    return {
        "code": 0,
        "msg": "ok",
        "data": {"keyword": keyword, "total": len(hits), "items": hits},
    }


@router.get("/{family_id}/path", summary="始祖到某人的路径")
async def path_to_person(
    family_id: int,
    user: OptionalUser,
    db: DbSession,
    to: int = Query(..., description="目标 person_id"),
) -> dict:
    from app.services.tree_service import _root_to_anchor

    family, graph, viewer = resolve_context(db, user, family_id)

    path_ids = _root_to_anchor(graph, to)
    items = []
    for pid in path_ids:
        person = graph.person_of.get(pid)
        if person is None:
            continue
        brief = project_person_brief(person, viewer)
        if brief is not None:
            items.append(brief)

    return {
        "code": 0,
        "msg": "ok",
        "data": {"path": items, "depth": len(items)},
    }


@router.get("/{family_id}/region", summary="全族总览按视口拉取节点详情")
async def region(
    family_id: int,
    user: OptionalUser,
    db: DbSession,
    x1: int = Query(0),
    y1: int = Query(0),
    x2: int = Query(100000),
    y2: int = Query(100000),
) -> dict:
    """全族总览先给缩略图（lite，约 40KB），拖动时按视口拉这一块的详情。

    这是唯一能在几百毫秒内出一屏几千节点的做法。
    """
    from sqlalchemy import select

    from app.models.system import TreeLayoutCache
    import json

    family, graph, viewer = resolve_context(db, user, family_id)

    cache = db.execute(
        select(TreeLayoutCache).where(
            TreeLayoutCache.family_id == family_id,
            TreeLayoutCache.view_key == "3:none:lite",
        )
    ).scalar_one_or_none()

    if cache is None or int(cache.family_rev) != int(family.data_rev):
        from app.services.tree_service import _compute_layout, _scope_overview

        layout = _compute_layout(graph, _scope_overview(graph))
    else:
        layout = json.loads(cache.layout_json)

    items = []
    for n in layout["nodes"]:
        if not (x1 <= n["x"] <= x2 and y1 <= n["y"] <= y2):
            continue
        person = graph.person_of.get(n["id"])
        if person is None:
            continue
        brief = project_person_brief(person, viewer)
        if brief is None:
            continue
        brief.update(
            {
                "x": n["x"],
                "y": n["y"],
                "gen": n["gen"],
                # 同主树：端上算连线和居中都要用，不能省
                "w": n.get("w"),
                "h": n.get("h"),
            }
        )
        items.append(brief)

    return {
        "code": 0,
        "msg": "ok",
        "data": {"total": len(items), "items": items},
    }


_CN = "零一二三四五六七八九"


def _cn(n: int | None) -> str:
    from app.services.mask import cn_number

    return cn_number(n)
