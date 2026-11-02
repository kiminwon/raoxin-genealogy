"""字辈校验。

## 决策第 1 条：只做提示参考，不做强制校验

这条决策落到实现上，就是：**校验逻辑绝不写进数据库约束，也不阻断任何提交**。
它的唯一产物是一个提示对象，前端渲染成黄色信息条。

## 为什么不能强制

1. 现在起名越来越不看字辈，这是事实
2. 强制校验会误伤三类人：女性成员（基本不用字辈）、外姓成员（字辈诗根本不同）、
   以及文革期间改名的那一代
3. 一旦出现"系统不让我录我爷爷"，用户直接弃用——这比数据里有个小错严重得多

## 什么时候才跑校验

仅当 `lineage_role=1`（本族男丁）**且**用户主动填了 `generation_name` 字段时才比对。
不填就完全不打扰。这样既不失灵，也不烦人。
"""

from __future__ import annotations

from typing import Any

from app.core.constants import LineageRole
from app.services.mask import cn_number


def expected_generation_from_poem(
    poem: str | None,
    first_gen_label: int,
    generation_name: str | None,
) -> int | None:
    """字辈诗第 i 个汉字对应第 (first_gen_label + i) 世。

    例：poem='文章华国诗礼传家', first_gen_label=15
        '文'->15世 '章'->16 '华'->17 '国'->18 …
    """
    if not poem or not generation_name:
        return None
    pos = poem.find(generation_name)
    if pos < 0:
        return None
    return int(first_gen_label) + pos


def check_generation(
    family: Any,
    *,
    generation_name: str | None,
    claimed_generation: int | None,
    lineage_role: int = int(LineageRole.MALE),
) -> dict[str, Any] | None:
    """返回提示对象，或 None（不提示）。

    返回结构：
        {
          "matched": bool,
          "expected": int | None,
          "found": int | None,
          "char": str,
          "level": "info",
          "message": str,
          "blocking": False        # 恒为 False，见模块文档
        }
    """
    # 校验开关关掉时，什么都不提示
    if not getattr(family, "gen_poem_enabled", 1):
        return None

    # 只对本族男丁跑。女性、媳妇、女婿、外孙用的不是这套字辈诗。
    if lineage_role != int(LineageRole.MALE):
        return None

    # 用户没填字辈用字，就不打扰
    if not generation_name:
        return None

    poem = getattr(family, "generation_poem", None)
    first_label = int(getattr(family, "first_gen_label", 1) or 1)

    expected = expected_generation_from_poem(poem, first_label, generation_name)
    if expected is None:
        # 这个字不在字辈诗里（可能是分支用字、或者是笔误）。
        # 只做弱提示，语气要轻，因为诗本身可能记不全。
        return {
            "matched": False,
            "expected": None,
            "found": claimed_generation,
            "char": generation_name,
            "level": "info",
            "message": (
                f"「{generation_name}」字不在已录入的字辈诗中，"
                f"如果族里还有后续字辈，可以请房长补充"
            ),
            "blocking": False,
        }

    if claimed_generation is None:
        return {
            "matched": True,
            "expected": expected,
            "found": None,
            "char": generation_name,
            "level": "info",
            "message": f"按字辈诗，「{generation_name}」字对应第{cn_number(expected)}世",
            "suggestGeneration": expected,
            "blocking": False,
        }

    if int(claimed_generation) == expected:
        return None   # 一致就别打扰用户

    poem_hint = f"「{poem}」" if poem else ""
    return {
        "matched": False,
        "expected": expected,
        "found": int(claimed_generation),
        "char": generation_name,
        "level": "info",
        "message": (
            f"按字辈诗{poem_hint}，「{generation_name}」字应为第{cn_number(expected)}世，"
            f"您填写的是第{cn_number(int(claimed_generation))}世，请确认是否无误"
        ),
        "blocking": False,
    }


def explain_poem(poem: str | None, first_gen_label: int, limit: int = 24) -> list[dict[str, Any]]:
    """把字辈诗拆成「字 -> 世次」对照，用于族首页展示。

    这一段在族首页很有价值：很多年轻人是第一次知道自家字辈诗的完整内容。
    """
    if not poem:
        return []
    return [
        {
            "char": ch,
            "generation": int(first_gen_label) + i,
            "label": f"第{cn_number(int(first_gen_label) + i)}世",
        }
        for i, ch in enumerate(poem[:limit])
    ]
