"""谱文接口：列表 / 详情 / 新建 / 更新 / 软删。

口径（启明 2026-10-21 拍板）：
    读   = 本族成员（user_family 有行）。访客暂不给看——谱文是族内资料，
           将来要开放给"带浏览码的访客"再说，先把门关上。
    写   = 房长及以上，直接生效，不走修订审核。留痕靠 created_by/updated_by，
           删除是软删（status=0），审计可查。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, load_family
from app.core.constants import ArticleCategory, UserRole
from app.core.errors import forbidden, not_found, param_invalid
from app.models.article import FamilyArticle
from app.models.user import UserFamily

router = APIRouter(tags=["articles"])

CATEGORY_LABEL = {
    int(ArticleCategory.PREFACE): "谱序",
    int(ArticleCategory.REVISION_HISTORY): "编修历史",
    int(ArticleCategory.MIGRATION): "迁徙源流",
    int(ArticleCategory.EXAMPLES): "凡例",
    int(ArticleCategory.GENERATION_POEM): "字辈派语",
    int(ArticleCategory.OTHER): "其他",
}

EXCERPT_LEN = 60


def _membership(db, user, family_id: int) -> UserFamily:
    """本族成员资格。**按族查 user_family**，role 也是——app_user.role 是
    "当前族"的快照，文章接口的 family_id 在路径里，两者可能是不同的族。"""
    row = db.execute(
        select(UserFamily).where(
            UserFamily.user_id == user.id,
            UserFamily.family_id == family_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise forbidden("仅本族成员可以查看谱文")
    return row


def _require_writer(member: UserFamily) -> None:
    if int(member.role) < int(UserRole.BRANCH_HEAD):
        raise forbidden("仅理事会成员或管理员可以编辑谱文")


def _load_article(db, article_id: int) -> FamilyArticle:
    a = db.get(FamilyArticle, article_id)
    if a is None or a.status != 1:
        raise not_found("谱文")
    return a


def _excerpt(content: str) -> str:
    flat = " ".join((content or "").split())
    return flat[:EXCERPT_LEN] + ("…" if len(flat) > EXCERPT_LEN else "")


def _brief(a: FamilyArticle) -> dict:
    return {
        "id": a.id,
        "category": a.category,
        "categoryLabel": CATEGORY_LABEL.get(a.category, "其他"),
        "title": a.title,
        "excerpt": _excerpt(a.content),
        "sortOrder": a.sort_order,
        "updatedAt": a.updated_at.strftime("%Y-%m-%d") if a.updated_at else None,
    }


def _detail(a: FamilyArticle) -> dict:
    return {
        **_brief(a),
        "content": a.content,
        "createdBy": a.created_by,
        "updatedBy": a.updated_by,
    }


@router.get("/family/{family_id}/articles", summary="谱文列表（本族成员）")
async def list_articles(
    family_id: int,
    user: CurrentUser,
    db: DbSession,
    category: int | None = Query(None, description="按分类过滤，缺省全部分类"),
) -> dict:
    load_family(db, family_id)
    _membership(db, user, family_id)

    q = select(FamilyArticle).where(
        FamilyArticle.family_id == family_id,
        FamilyArticle.status == 1,
    )
    if category is not None:
        q = q.where(FamilyArticle.category == category)
    rows = db.execute(
        q.order_by(FamilyArticle.category, FamilyArticle.sort_order, FamilyArticle.id)
    ).scalars().all()

    return {"code": 0, "msg": "ok", "data": {"items": [_brief(a) for a in rows]}}


@router.get("/article/{article_id}", summary="谱文详情（本族成员）")
async def get_article(article_id: int, user: CurrentUser, db: DbSession) -> dict:
    a = _load_article(db, article_id)
    _membership(db, user, a.family_id)
    return {"code": 0, "msg": "ok", "data": _detail(a)}


class ArticleIn(BaseModel):
    category: int = Field(..., description="1谱序 2编修历史 3迁徙源流 4凡例 5字辈派语 6其他")
    title: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=60000, description="纯文本，按空行分段")
    sortOrder: int = Field(0, ge=0, le=9999)


@router.post("/family/{family_id}/articles", summary="新建谱文（房长及以上）")
async def create_article(
    family_id: int, body: ArticleIn, user: CurrentUser, db: DbSession
) -> dict:
    load_family(db, family_id)
    member = _membership(db, user, family_id)
    _require_writer(member)

    if body.category not in CATEGORY_LABEL:
        raise param_invalid(f"category 取值 1-6，收到 {body.category}")

    a = FamilyArticle(
        family_id=family_id,
        category=body.category,
        title=body.title.strip(),
        content=body.content,
        sort_order=body.sortOrder,
        status=1,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(a)
    db.commit()
    return {"code": 0, "msg": "ok", "data": _detail(a)}


@router.put("/article/{article_id}", summary="更新谱文（房长及以上）")
async def update_article(
    article_id: int, body: ArticleIn, user: CurrentUser, db: DbSession
) -> dict:
    a = _load_article(db, article_id)
    member = _membership(db, user, a.family_id)
    _require_writer(member)

    if body.category not in CATEGORY_LABEL:
        raise param_invalid(f"category 取值 1-6，收到 {body.category}")

    a.category = body.category
    a.title = body.title.strip()
    a.content = body.content
    a.sort_order = body.sortOrder
    a.updated_by = user.id
    db.commit()
    return {"code": 0, "msg": "ok", "data": _detail(a)}


@router.delete("/article/{article_id}", summary="删除谱文（软删，房长及以上）")
async def delete_article(article_id: int, user: CurrentUser, db: DbSession) -> dict:
    a = _load_article(db, article_id)
    member = _membership(db, user, a.family_id)
    _require_writer(member)

    a.status = 0
    a.updated_by = user.id
    db.commit()
    return {"code": 0, "msg": "ok", "data": {"id": a.id, "deleted": True}}
