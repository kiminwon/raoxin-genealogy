"""路由汇总。

注意注册顺序：带路径参数的通配路由（如 /invite/{code}）必须放在
同前缀的固定路由（如 /invite/mine）之后，否则会被抢先匹配。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import admin, article, auth, family, feed, identify, invite, person, tree

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(identify.router)
api_router.include_router(family.router)
api_router.include_router(article.router)
api_router.include_router(feed.router)
api_router.include_router(tree.router)
api_router.include_router(person.router)
api_router.include_router(invite.router)
api_router.include_router(admin.router)
