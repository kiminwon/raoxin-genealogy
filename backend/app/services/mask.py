"""人物字段脱敏投影。

## 为什么放在 API 层而不是数据库视图

脱敏规则一定会变——今天对访客脱敏，明天可能要求对全体族人收紧。
放数据库视图改一次要迁移；放这里改几行代码。

代价是**任何一个新接口都必须记得走投影函数**，漏一个就泄露。
所以硬性约定：所有返回 person 数据的接口，一律经 project_person() 输出，
不允许直接返回 ORM 对象或 __dict__。

## 脱敏口径（决策第 4 条，2026-10-16 调整）

| 字段 | 访客 / 未认领 | 已认领·旁系 | 已认领·近亲 | 管理员 |
|---|---|---|---|---|
| 在世者姓名 | 陈*明 | **全名** | 全名 | 全名 |
| 在世者出生 | 仅年份 | 仅年份 | 年月日 | 年月日 |
| 在世者职业/生平 | 隐藏 | 隐藏 | 显示 | 显示 |
| 已故者全部字段 | 显示 | 显示 | 显示 | 显示 |

**姓名与其它字段的分界线就是"认领"**：认领成功 = 答题自证通过 = 已经证明
自己是这一族的人，同族在世者的姓名就应当可见。分界线的设计理由见
`project_name()` 的注释。而出生日期精度、职业、生平仍只对近亲开放——
那几项的个人信息浓度比姓名高，没必要跟着放大。

另外：person 表里**根本没有**手机号和详细住址字段，这一层不需要处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.constants import DatePrecision, LivingStatus, PrivacyLevel, UserRole, ViewerScope


@dataclass
class Viewer:
    """访问者上下文。由 deps 层构建，一路透传到投影函数。"""

    scope: ViewerScope = ViewerScope.GUEST
    user_id: int | None = None
    role: int = int(UserRole.NORMAL)
    family_id: int | None = None
    claimed_person_id: int | None = None

    # 近亲集合：直系上下 3 代 + 兄弟姐妹 + 配偶
    close_person_ids: set[int] = field(default_factory=set)
    # 可编辑集合（edit_scope 命中的 person_id）
    edit_scope_ids: set[int] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.role >= int(UserRole.BRANCH_HEAD)

    @property
    def is_logged_in(self) -> bool:
        return self.user_id is not None

    @property
    def can_see_all_names(self) -> bool:
        """能否看到全族在世者的真实姓名。

        认领成功（scope 升到 CLOSE）或管理员以上才放开。
        **刻意不含 LINEAGE**（登录了、入了族、但没认领）——那种状态很可能是
        拿着族浏览码从家族群点进来的，而码会被转发到族外，所以照样打码。
        换句话说：进入这个族靠码，证明"你是这个族的人"靠答题自证，
        姓名可见性绑定在后一件事上。
        """
        return self.is_admin or self.scope in (ViewerScope.CLOSE, ViewerScope.ADMIN)

    def can_edit(self, person_id: int) -> bool:
        return self.is_admin or person_id in self.edit_scope_ids

    def is_close_to(self, person_id: int) -> bool:
        return self.claimed_person_id == person_id or person_id in self.close_person_ids


# ============================================================
# 姓名
# ============================================================

_CN_DIGITS = "零一二三四五六七八九"


def cn_number(n: int | None) -> str:
    """19 -> 十九，24 -> 二十四。用于生成"第十九世"这类标签。"""
    if n is None:
        return ""
    if n < 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    return str(n)


def mask_name(name: str | None) -> str | None:
    """陈启明 -> 陈*明；陈明 -> 陈*；单字不脱敏。"""
    if not name:
        return None
    n = len(name)
    if n <= 1:
        return name
    if n == 2:
        return name[0] + "*"
    return name[0] + "*" * (n - 2) + name[-1]


def raw_display_name(person: Any) -> str:
    """未脱敏的展示名。

    老谱里"配王氏"是常态——只知道姓。这时显示"王氏"而不是空白，
    且这条记录会自动进入待补全清单，由后代补全。
    """
    if person.name:
        return person.name
    if person.surname:
        return f"{person.surname}氏"
    return "配某氏"


def project_name(person: Any, viewer: Viewer) -> tuple[str | None, str]:
    """返回 (脱敏后的 name, 展示名)。

    已故者姓名一律不脱敏（属于史料）；在世者对**已认领身份的族人**与管理员
    显示全名，对访客与未认领者打码。

    ## 为什么 2026-10-16 从"只给近亲"改成"认领即可见"

    原来的判定是 `is_close_to()`（直系上下 3 代 + 兄弟姐妹 + 配偶）。
    实测下来不对劲：以演示族的陈雨欣（认领者）为例，她打开全族总览看到的
    是三世伯父陈*梁、陈*华和两个堂侄陈*远、陈*彤——整整 5 个人糊着。
    而"全族总览"这一屏的存在意义就是看清整个族的结构，名字糊掉等于白做。

    更关键的是自相矛盾：已定决策第 6 条已经把"认领"的门槛降到答题自证、
    不经任何审批，理由是"同族的码只会分享给族人，阻塞的代价远大于冒领的
    代价"。既然认领都这么放了，认领之后还遮着同族人的名字，逻辑上讲不通。

    所以姓名这一项绑定到"认领"上，防线退回到它本来该在的位置——
    **未认领的访客**（族浏览码可能被转发到族外，那个必须继续打码）。
    """
    full = raw_display_name(person)

    if _is_living(person) and not viewer.can_see_all_names:
        if person.name:
            return mask_name(person.name), mask_name(person.name) or full
        return None, full  # 只有姓氏，本来就是"王氏"，无需再脱敏

    return person.name, full


def _is_living(person: Any) -> bool:
    return getattr(person, "living_status", None) == int(LivingStatus.LIVING)


# ============================================================
# 可见性
# ============================================================


def can_view_person(person: Any, viewer: Viewer) -> bool:
    """privacy_level 控制"这条记录能不能被看到"。"""
    if viewer.is_admin:
        return True
    if viewer.claimed_person_id is not None and viewer.claimed_person_id == person.id:
        return True

    level = getattr(person, "privacy_level", int(PrivacyLevel.LINEAGE))

    if level == int(PrivacyLevel.PUBLIC) or level == int(PrivacyLevel.LINEAGE):
        return True          # 访客可看脱敏版
    if level == int(PrivacyLevel.CLOSE):
        return viewer.is_close_to(person.id)
    if level == int(PrivacyLevel.PRIVATE):
        return False          # 仅本人与管理员，近亲也不给
    return True


# ============================================================
# 日期
# ============================================================


def format_date(
    d: date | None,
    precision: int = 0,
    *,
    year_only: bool = False,
) -> dict[str, Any] | None:
    """按精度输出日期。year_only=True 时只给年份（用于在世者的对外展示）。"""
    if d is None:
        return None

    prec = int(precision or 0)

    if year_only or prec == int(DatePrecision.YEAR):
        return {"date": f"{d.year:04d}", "precision": 1, "display": f"{d.year}年"}
    if prec == int(DatePrecision.MONTH):
        return {
            "date": f"{d.year:04d}-{d.month:02d}",
            "precision": 2,
            "display": f"{d.year}年{d.month}月",
        }
    if prec == int(DatePrecision.DAY):
        return {
            "date": d.isoformat(),
            "precision": 3,
            "display": f"{d.year}年{d.month}月{d.day}日",
        }
    # 精度未知：退化成只给年份，宁少不多
    return {"date": f"{d.year:04d}", "precision": 1, "display": f"{d.year}年"}


# ============================================================
# 主投影
# ============================================================


def project_person(
    person: Any,
    viewer: Viewer,
    *,
    include_meta: bool = True,
) -> dict[str, Any] | None:
    """把 ORM 对象投影成对外的 dict。不可见时返回 None。"""
    if not can_view_person(person, viewer):
        return None

    name, display_name = project_name(person, viewer)
    living = _is_living(person)
    close = viewer.is_close_to(person.id) or viewer.is_admin
    # 在世者对外只给年份
    birth_year_only = living and not close
    death_year_only = living and not close   # 已故者不脱敏

    d: dict[str, Any] = {
        "id": person.id,
        "familyId": person.family_id,
        "branchId": getattr(person, "branch_id", None),
        "name": name,
        "displayName": display_name,
        "surname": person.surname,
        "gender": person.gender,
        "generation": person.generation,
        "generationLabel": f"第{cn_number(person.generation)}世" if person.generation else None,
        "lineageRole": person.lineage_role,
        "isLineageCarrier": bool(getattr(person, "is_lineage_carrier", 0)),
        "courtesyName": getattr(person, "courtesy_name", None),
        "artName": getattr(person, "art_name", None),
        "rankInFamily": getattr(person, "rank_in_family", None),
        "birth": format_date(person.birth_date, person.birth_date_precision, year_only=birth_year_only),
        "death": format_date(person.death_date, person.death_date_precision, year_only=death_year_only),
        "livingStatus": person.living_status,
        "burialPlace": person.burial_place,
        "burialNote": person.burial_note,
        "nativePlace": person.native_place,
        "privacyLevel": person.privacy_level,
        "status": person.status,
        "altFamilyId": getattr(person, "alt_family_id", None),
        "myPermissions": {
            "canEdit": viewer.can_edit(person.id),
            "canClaim": (
                viewer.is_logged_in
                and viewer.claimed_person_id is None
                and person.status == 1
            ),
            "alreadyClaimedByMe": viewer.claimed_person_id == person.id,
            "isCloseRelative": close,
        },
    }

    # 在世者的职业与生平只对近亲开放
    if not living or close:
        d["education"] = person.education
        d["occupation"] = person.occupation
        d["biography"] = person.biography

    if include_meta:
        d["sourceType"] = person.source_type
        d["sourceNote"] = person.source_note
        d["confidence"] = person.confidence
        d["verifyStatus"] = person.verify_status

    return d


def project_person_brief(person: Any, viewer: Viewer) -> dict[str, Any] | None:
    """列表/关系链场景的轻量投影：只要姓名与世代。"""
    if not can_view_person(person, viewer):
        return None
    name, display_name = project_name(person, viewer)
    return {
        "id": person.id,
        "name": name,
        "displayName": display_name,
        "gender": person.gender,
        "generation": person.generation,
        "livingStatus": person.living_status,
        "lineageRole": person.lineage_role,
    }
