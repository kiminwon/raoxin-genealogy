"""ORM 模型统一出口。

必须在这里全部导入，否则 Base.metadata 不完整（数据一致性巡检脚本依赖它）。
"""

from app.models.article import FamilyArticle
from app.models.family import Branch, Family
from app.models.person import FamilyUnion, ParentChild, Person
from app.models.system import AuditLog, Notification, Revision, TreeLayoutCache
from app.models.user import AppUser, Claim, EditScope, InviteCode, UserFamily

__all__ = [
    "AuditLog",
    "AppUser",
    "Branch",
    "Claim",
    "EditScope",
    "Family",
    "FamilyArticle",
    "FamilyUnion",
    "InviteCode",
    "Notification",
    "ParentChild",
    "Person",
    "Revision",
    "TreeLayoutCache",
    "UserFamily",
]
