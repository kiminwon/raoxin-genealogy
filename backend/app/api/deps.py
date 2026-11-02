"""FastAPI 依赖：数据库会话、当前用户、访问者上下文。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.errors import not_found, unauthorized
from app.core.security import decode_token, token_subject
from app.db.session import get_db
from app.models.family import Family
from app.models.user import AppUser
from app.services.mask import Viewer
from app.services.scope_service import RelationGraph, build_viewer

DbSession = Annotated[Session, Depends(get_db)]


async def get_optional_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> AppUser | None:
    """有 token 就解析，没有就返回 None。用于"未登录也能看脱敏三代"的接口。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
        uid = token_subject(payload)
    except Exception:
        return None
    user = db.get(AppUser, uid)
    if user is None or user.status != 1:
        return None
    return user


async def get_current_user(
    user: Annotated[AppUser | None, Depends(get_optional_user)],
) -> AppUser:
    if user is None:
        raise unauthorized()
    return user


OptionalUser = Annotated[AppUser | None, Depends(get_optional_user)]
CurrentUser = Annotated[AppUser, Depends(get_current_user)]


# ---------------------------------------------------------------
# 领域依赖（普通函数，路由里显式调用，便于按 familyId 构建）
# ---------------------------------------------------------------


def load_family(db: Session, family_id: int) -> Family:
    family = db.get(Family, family_id)
    if family is None or family.status != 1:
        raise not_found("宗族")
    return family


def resolve_context(
    db: Session,
    user: AppUser | None,
    family_id: int,
    *,
    with_close: bool = True,
) -> tuple[Family, RelationGraph, Viewer]:
    """一次性把族、关系图、访问者上下文都准备好。

    关系图全族只加载一次，后续所有 BFS / 布局都复用它——
    这是树接口能在几百毫秒内出结果的关键。
    """
    family = load_family(db, family_id)
    graph = RelationGraph.load(db, family_id)
    viewer = build_viewer(db, user, family_id, graph=graph, with_close=with_close)
    return family, graph, viewer
