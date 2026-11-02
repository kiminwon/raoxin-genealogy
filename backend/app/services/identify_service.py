"""答题自证 —— 用「补全父母全名」确认族人身份。

## 为什么换掉房长审批

房长年纪大、不会操作审批界面；而同一族的码只会分享给族人，冒领少见。
阻塞的代价（产品没人用）远大于冒领的代价（可清洗的少量脏数据）。

## 为什么不是「在树上点自己」

脱敏规则下，认领**之前**用户看到的自己是「陈*明」（见 mask.py 的口径表），
他没法确认那是不是自己。所以改成：直接问，系统反查 ——
用户不需要先在树上找到任何东西。

## 题目强度取决于数据（这条反直觉，但决定了实现）

| 所填亲属 | 树上显示 | 题目强度 |
|---|---|---|
| 在世 | `陈*栋` | 强 —— 外人抄不到 |
| 已故 | `陈国栋` | 弱 —— 名字就在屏幕上，退化成读屏题 |

所以匹配成功后要顺手算一次**证据强度**并留痕（`claim.verify_answers`），
供房长抽查；而"谱里没我、要新建节点"这条路径上遇到弱证据时，追加一个门槛。

## 安全边界（三个洞 + 三个便宜的补丁）

1. 搜索即枚举器（`tree.py` 的按名搜人）→ 靠**限速**压住尝试成本
2. 已故者名字公开 → 靠**证据强度留痕** + 新建节点时追加门槛
3. 无限次尝试 → 靠**限速 + 模糊反馈**（失败不区分"没这人"和"不是你父亲"）

第 2 条的做法值得说明：不是"防住"，而是"通过但留痕 + 可撤销"。
家谱数据有长期价值，但垃圾数据可清洗，卡死的产品不可救。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import (
    ClaimStatus,
    ClaimType,
    Confidence,
    Gender,
    IdentifyResult,
    LineageRole,
    LivingStatus,
    ParentRelation,
    SourceType,
    VerifyMethod,
)
from app.core.errors import BizError, ErrCode
from app.models.family import Family
from app.models.person import FamilyUnion, Person
from app.models.system import AuditLog, Notification
from app.models.user import AppUser, Claim
from app.services.family_member import join_and_activate, set_claimed
from app.services.mask import Viewer, ViewerScope, project_person_brief
from app.services.revision_service import submit_create, submit_update
from app.services.scope_service import RelationGraph, grant_edit_scope

# ============================================================
# 限速（补丁 2）
# ============================================================

RATE_WINDOW_SECONDS = 3600
RATE_MAX_ATTEMPTS = 10        # 每小时最多答几次（含中性的，挡枚举速率）
RATE_MAX_TRAILING_FAILS = 5   # 连续真答错几次就冷却

# 一次作答的三种结果。**只有 "wrong" 才算失败**——
# 2026-10-29 修：原来 40901「该节点已被他人认领」也记失败，用户答案全对
# 却连撞五次被冷却一小时（当时云端节点被 mock 账号占着）。
# 罚错人比不罚更伤信任：错的是系统状态，不该记在用户头上。
#   ok      匹配成功，清空连续失败
#   wrong   名字匹配不上 —— 唯一的"真答错"
#   neutral 系统态 / 信息不足：节点已被占、已故祖先、重名要补填、数据异常。
#           计入窗口总次数（仍能挡住暴力枚举），但不累加连续失败。
_ATTEMPT_OK = "ok"
_ATTEMPT_WRONG = "wrong"
_ATTEMPT_NEUTRAL = "neutral"

# 进程内计数。V1 单实例部署够用；将来多实例换成 Redis 即可，接口不用改。
_buckets: dict[int, list[tuple[float, str]]] = {}


def _prune(user_id: int, now: float) -> list[tuple[float, str]]:
    events = [e for e in _buckets.get(user_id, []) if now - e[0] < RATE_WINDOW_SECONDS]
    _buckets[user_id] = events
    return events


def _trailing_fails(events: list[tuple[float, str]]) -> int:
    """从尾部数连续的真答错次数。遇到 ok / neutral 即断——用户换了输入条件，
    不是在同一条错误答案上反复试。"""
    n = 0
    for _, kind in reversed(events):
        if kind == _ATTEMPT_WRONG:
            n += 1
        else:
            break
    return n


def _retry_after(ts: float, now: float) -> int:
    """某条记录滑出窗口还差几秒。"""
    return max(1, int(round(ts + RATE_WINDOW_SECONDS - now)))


def check_rate_limit(user_id: int) -> None:
    """出题/答题前先过这一关。挡的是暴力枚举「父亲名 × 自己名」。

    被拒时把**还要等多久**一起给出去（retryAfterSeconds）：
    旧文案只说"请稍后再试"，用户不知道是等 5 分钟还是一整天，
    只能反复戳，几次之后就会认定"这软件坏了"。
    """
    now = time.time()
    events = _prune(user_id, now)

    if len(events) >= RATE_MAX_ATTEMPTS:
        raise BizError(
            ErrCode.RATE_LIMITED,
            "答题次数过多，请一小时后再试",
            {
                "reason": "too_many_attempts",
                "retryAfterSeconds": _retry_after(events[0][0], now),
                "attempts": len(events),
                "maxAttempts": RATE_MAX_ATTEMPTS,
                "failCount": _trailing_fails(events),
                "windowSeconds": RATE_WINDOW_SECONDS,
            },
        )

    trailing = _trailing_fails(events)
    if trailing >= RATE_MAX_TRAILING_FAILS:
        # 解禁时刻 = 这批连续失败里最早那条滑出窗口的时刻
        earliest_fail = events[-trailing][0]
        raise BizError(
            ErrCode.RATE_LIMITED,
            "连续答错多次，请稍后再试，或联系房长人工核实",
            {
                "reason": "trailing_fails",
                "retryAfterSeconds": _retry_after(earliest_fail, now),
                "attempts": len(events),
                "maxAttempts": RATE_MAX_ATTEMPTS,
                "failCount": trailing,
                "maxTrailingFails": RATE_MAX_TRAILING_FAILS,
                "windowSeconds": RATE_WINDOW_SECONDS,
                "unlockable": True,   # 房长可在管理端一键解禁
            },
        )


def record_attempt(user_id: int, kind: str = _ATTEMPT_WRONG) -> None:
    """记一次作答。kind 见 _ATTEMPT_*；兼容旧的布尔调用（True=ok / False=wrong）。"""
    if isinstance(kind, bool):
        kind = _ATTEMPT_OK if kind else _ATTEMPT_WRONG
    _prune(user_id, time.time())
    _buckets.setdefault(user_id, []).append((time.time(), kind))


def reset_rate_limit(user_id: int | None = None) -> None:
    """清空计数。None = 全清（测试用）；给 user_id = 解禁这个人（管理端用）。"""
    if user_id is None:
        _buckets.clear()
    else:
        _buckets.pop(user_id, None)


def lock_status(user_id: int, now: float | None = None) -> dict[str, Any]:
    """查一个人当前的限速状态。管理端「答题解禁」面板用。

    注意：计数在进程内存里，**重启服务即全部清零**——面板上要如实写这一条，
    否则理事会长会以为"解禁"是个需要慎重对待的重操作。
    """
    now = time.time() if now is None else now
    events = _prune(user_id, now)
    trailing = _trailing_fails(events)

    locked = False
    reason = None
    retry_after = 0
    if len(events) >= RATE_MAX_ATTEMPTS:
        locked, reason = True, "too_many_attempts"
        retry_after = _retry_after(events[0][0], now)
    elif trailing >= RATE_MAX_TRAILING_FAILS:
        locked, reason = True, "trailing_fails"
        retry_after = _retry_after(events[-trailing][0], now)

    return {
        "userId": user_id,
        "locked": locked,
        "reason": reason,
        "retryAfterSeconds": retry_after,
        "attempts": len(events),
        "failCount": trailing,
        "lastAttemptAt": (
            datetime.fromtimestamp(events[-1][0]).strftime("%Y-%m-%d %H:%M:%S")
            if events else None
        ),
    }


def list_locked_user_ids(now: float | None = None) -> list[int]:
    """当前进程里处于冷却状态的 userId。管理端列表用。"""
    now = time.time() if now is None else now
    return [uid for uid in list(_buckets.keys()) if lock_status(uid, now)["locked"]]



# ============================================================
# 姓名归一化（含繁简转换 —— 老谱是繁体，用户输入是简体）
# ============================================================

# 成对的「繁体 -> 简体」。逐块校验长度，错位会直接抛错而不是静默错位映射。
_TRAD_SIMP_PAIRS: tuple[tuple[str, str], ...] = (
    ("陳張劉黃楊吳趙孫馬鄭", "陈张刘黄杨吴赵孙马郑"),
    ("謝許韓馮鄧蕭蔣葉蘇魏", "谢许韩冯邓萧蒋叶苏魏"),
    ("呂盧鍾譚陸賈韋鄒閆賀", "吕卢钟谭陆贾韦邹闫贺"),
    ("顧龔萬錢嚴歐樂畢邊龐", "顾龚万钱严欧乐毕边庞"),
    ("闞齊婁樓蘆鞏閔閻賴", "阚齐娄楼芦巩闵阎赖"),
    ("厲勵國華偉東強軍輝傑", "厉励国华伟东强军辉杰"),
    ("濤剛蘭鳳雲飛鵬興財富", "涛刚兰凤云飞鹏兴财富"),
    ("貴榮發達遠進開學禮義", "贵荣发达远进开学历义"),
    ("慶龍傳業廣豐嶽巖巒嵐", "庆龙传业广丰岳岩峦岚"),
    ("劍鋒銘鈞銳淵澤濟瀚滄", "剑锋铭钧锐渊泽济瀚沧"),
    ("橋樑棟樹果憲賢貞觀親", "桥梁栋树果宪贤贞观亲"),
    ("儀儉優傳來倉儲僅億價", "仪俭优传来仓储仅亿价"),
    ("養餘類數應標樸權欽歡", "养余类数应标朴权钦欢"),
    ("歲歷歸殘動務勝勢區醫", "岁历归残动务胜势区医"),
    ("藥師隊陣顯靈寶寧靜潔", "药师德阵显灵宝宁静洁"),
    ("淨鶴鸞鹽繼斷書畫藝錦", "净鹤鸾盐继断书画艺锦"),
    ("鈺銀鐵訓詔詩詞語謙讓", "钰银铁训诏诗词语谦让"),
    ("議輔軒領頑飲飯館鳥鳴", "议辅轩领顽饮饭馆鸟鸣"),
    ("鷹驅騎驗鬚齋夢覺懷憶", "鹰驱骑验须斋梦觉怀忆"),
    ("戀壘處號虧麥燈燦煉統", "恋垒处号亏麦灯灿炼统"),
    ("經織結紀紅純島峽嶺灣", "经织结纪红纯岛峡岭湾"),
    ("庫莊營農場園團圍啟濬", "库庄营农场园团围启浚"),
    ("徵堃昇喆甦峯崑鑑鐘詠", "征坤升哲苏峰昆鉴钟咏"),
    ("詒廈廬礎祿禎蔭蓮驊驥", "诒厦庐础禄祯荫莲骅骥"),
    # 异体字／常见变体。不是严格的繁简关系，但归一化的目的就是"写法不同算同一个人"。
    # 「啓」是「啟」的异体，老谱里出现频率极高，漏了它字辈用字就整批匹配不上。
    ("啓甯羣恆涼麗燁曄禕", "启宁群恒凉丽烨晔祎"),
    ("鏞釗頤頌懌灑", "镛钊颐颂怿洒"),
)


def _build_translate_table() -> dict[int, str]:
    table: dict[int, str] = {}
    for trad, simp in _TRAD_SIMP_PAIRS:
        if len(trad) != len(simp):
            # 静默截断（zip 的行为）会让一部分字永远转不过来，
            # 症状是"有些人就是匹配不上"，极难排查。宁可启动就炸。
            raise ValueError(
                f"繁简对照表长度不一致：{len(trad)} vs {len(simp)}\n  {trad}\n  {simp}"
            )
        for t, s in zip(trad, simp):
            table[ord(t)] = s
    return table


TRANSLATE_TABLE: dict[int, str] = _build_translate_table()

# 姓名里不该参与比对的东西：空白、间隔号、连接符
_NOISE = re.compile(r"[\s\u3000·・‧•．.\-—_]+")


def simplify(text: str) -> str:
    return text.translate(TRANSLATE_TABLE)


def normalize_name(raw: str | None) -> str:
    """姓名归一化：去空白与间隔号 → 转简体 → 去掉尾部「氏」。

    去掉「氏」是为了让用户把「配王氏」照抄进来时也能对上。
    """
    if not raw:
        return ""
    s = _NOISE.sub("", str(raw))
    s = simplify(s)
    if len(s) > 1 and s.endswith("氏"):
        s = s[:-1]
    return s


# ============================================================
# 定位（方案 §3.4）
# ============================================================


@dataclass
class LocateOutcome:
    result: str
    person_id: int | None = None
    relative_id: int | None = None
    relative_ids: list[int] = field(default_factory=list)
    candidates: list[int] = field(default_factory=list)
    strength: str = "weak"
    strength_note: str = ""


NOTE_STRONG = "您填的这位在谱上在世，其姓名在家族树上是脱敏的，非族人不掌握"
NOTE_WEAK = "您填的这位在谱上已故，姓名在家族树上是公开的，任何人都能看到"


def _strength_of(person: Person | None) -> tuple[str, str]:
    if person is None:
        return "weak", ""
    if person.living_status == int(LivingStatus.LIVING):
        return "strong", NOTE_STRONG
    if person.living_status == int(LivingStatus.DECEASED):
        return "weak", NOTE_WEAK
    return "medium", "您填的这位在谱上生卒不详"


def locate(
    graph: RelationGraph,
    *,
    relation: str,
    relative_name: str,
    self_name: str,
) -> LocateOutcome:
    """在树里反查「我是谁」。

    关系方向由 relation 决定：
      father / mother —— 所填是本人在谱中的父或母，反查其子女里有没有本人
      spouse          —— 所填是本人在谱中的配偶（嫁入的媳妇、入赘的女婿走这条）

    为什么必须全取同名候选、不能用 first：
        同名跨世代是常态（"国栋"在一支里可能有两代）。
        取 first 会把这个最常见的歧义静默当成命中，认领到错误的节点上——
        而错误的认领比认领失败难查得多。
    """
    rel_key = normalize_name(relative_name)
    me_key = normalize_name(self_name)
    if not rel_key or not me_key:
        return LocateOutcome(result=IdentifyResult.NO_MATCH.value)

    pool = list(graph.person_of.values())

    # 不按"这个人当过父母"过滤。
    # 看起来更严谨，但会把主路径堵死：老谱里 陈国梁 名下还没有子女记录，
    # 他的儿子来答题就永远匹配不上——而这恰恰是接力修谱最常见的情形：
    # 谱上有父亲、谱上还没你。过滤掉这些长辈，等于把新人挡在门外。
    relatives = [p for p in pool if normalize_name(p.name) == rel_key]
    if not relatives:
        return LocateOutcome(result=IdentifyResult.NO_MATCH.value)

    # 更像"本人直系长辈"的排前面：有子女的、在世的、世次深的优先。
    # 建新节点时就挂在这个人下面，所以要选最像的那一个。
    relatives.sort(
        key=lambda p: (
            0 if graph.children_of.get(p.id) else 1,
            0 if p.living_status == int(LivingStatus.LIVING) else 1,
            -(p.generation or 0),
        )
    )

    rel_ids = sorted(r.id for r in relatives)

    if relation == "spouse":
        matched: set[int] = set()
        for r in relatives:
            for sp in graph.spouses_of.get(r.id, []):
                p = graph.person_of.get(sp)
                if p is not None and normalize_name(p.name) == me_key:
                    matched.add(sp)
    else:
        child_ids: set[int] = set()
        for r in relatives:
            child_ids |= set(graph.children_of.get(r.id, []))

        matched = {
            cid for cid in child_ids
            if normalize_name(graph.person_of[cid].name) == me_key
        }

    # 证据强度取"最有利的那一个"——命中越"抄不到"，这次通过越可信
    best: Person | None = None
    best_rank = -1
    rank = {"strong": 2, "medium": 1, "weak": 0}
    for r in relatives:
        s, _ = _strength_of(r)
        if rank[s] > best_rank:
            best, best_rank = r, rank[s]
    strength, note = _strength_of(best)

    if len(matched) == 1:
        pid = next(iter(matched))
        return LocateOutcome(
            result=IdentifyResult.MATCHED.value,
            person_id=pid,
            relative_id=best.id if best else None,
            relative_ids=rel_ids,
            strength=strength,
            strength_note=note,
        )

    if len(matched) > 1:
        return LocateOutcome(
            result=IdentifyResult.AMBIGUOUS.value,
            candidates=sorted(matched),
            relative_id=best.id if best else None,
            relative_ids=rel_ids,
            strength=strength,
            strength_note=note,
        )

    # 亲属找到了、本人不在谱里 —— 这是**产品主路径**，不是异常分支。
    # 接力修谱的主力就是这批"老谱上没有、但人是活的"年轻人。
    return LocateOutcome(
        result=IdentifyResult.NEW_MEMBER.value,
        relative_id=best.id if best else None,
        relative_ids=rel_ids,
        strength=strength,
        strength_note=note,
    )


# ============================================================
# 出题
# ============================================================


def build_quiz(family: Family, graph: RelationGraph) -> dict[str, Any]:
    persons = list(graph.person_of.values())
    generations = [p.generation or 0 for p in persons]
    living = sum(1 for p in persons if p.living_status == int(LivingStatus.LIVING))

    return {
        "familyId": family.id,
        "familyName": family.name,
        "personCount": len(persons),
        "generationCount": max(generations) if generations else 0,
        "livingCount": living,
        "questions": [
            {
                "key": "relation",
                "type": "choice",
                "required": True,
                "label": "您和家谱里的哪一位最亲？",
                "defaultValue": "father",
                "options": [
                    {"value": "father", "label": "我父亲"},
                    {"value": "mother", "label": "我母亲"},
                    {"value": "spouse", "label": "我配偶"},
                ],
                "hint": "嫁入本族的媳妇、入赘的女婿，请选「我配偶」",
            },
            {
                "key": "relativeName",
                "type": "text",
                "required": True,
                "label": "他/她的名字",
                "strength": "strong",
                "hint": "按家谱上的写法填，繁体、简体都可以",
            },
            {
                "key": "selfName",
                "type": "text",
                "required": True,
                "label": "您自己的名字",
            },
            {
                "key": "extraName",
                "type": "text",
                "required": False,
                "label": "另一位家长的名字（选填）",
                "hint": "同名较多时，多填一位能帮我们确认是哪一支",
            },
        ],
        "hint": "答对就能直接进入，不需要等谁批。",
    }


# ============================================================
# 答题 + 自动认领
# ============================================================


@dataclass
class VerifyIn:
    """把入参收成一个内部结构，避免和 Pydantic 模型互相牵扯。"""

    relation: str = "father"
    relative_name: str = ""
    self_name: str = ""
    extra_name: str | None = None
    self_gender: int | None = None
    invite_code: str | None = None


def verify(
    db: Session,
    *,
    user: AppUser,
    family: Family,
    graph: RelationGraph,
    body: VerifyIn,
) -> dict[str, Any]:
    check_rate_limit(user.id)

    outcome = locate(
        graph,
        relation=body.relation,
        relative_name=body.relative_name,
        self_name=body.self_name,
    )

    snapshot: dict[str, Any] = {
        "relation": body.relation,
        "relativeName": body.relative_name,
        "selfName": body.self_name,
        "extraName": body.extra_name,
        "inviteCode": body.invite_code,
        "result": outcome.result,
        "matchedPersonId": outcome.person_id,
        "relativePersonIds": outcome.relative_ids,
        "evidenceStrength": outcome.strength,
        "evidenceNote": outcome.strength_note,
        "at": datetime.now().isoformat(timespec="seconds"),
    }

    if outcome.result == IdentifyResult.MATCHED.value:
        return _on_matched(db, user=user, family=family, graph=graph,
                           body=body, outcome=outcome, snapshot=snapshot)

    if outcome.result == IdentifyResult.NEW_MEMBER.value:
        return _on_new_member(db, user=user, family=family, graph=graph,
                              body=body, outcome=outcome, snapshot=snapshot)

    if outcome.result == IdentifyResult.AMBIGUOUS.value:
        # 重名要补填另一位家长：信息不足，不是答错
        record_attempt(user.id, _ATTEMPT_NEUTRAL)
        guests = Viewer(scope=ViewerScope.GUEST)
        candidates = []
        for cid in outcome.candidates:
            p = graph.person_of.get(cid)
            brief = project_person_brief(p, guests) if p is not None else None
            if brief is not None:
                brief["generationLabel"] = _gen_label(p.generation if p else None)
                candidates.append(brief)
        return {
            "result": IdentifyResult.AMBIGUOUS.value,
            "candidates": candidates,
            "msg": "族里有好几位同名，请补填另一位家长的名字再试",
            "needExtra": True,
        }

    # NO_MATCH —— 模糊反馈：不区分"谱里没这人"和"有但不是你父亲"。
    # 区分开就等于把树当成口令字典，逐个试出来。
    # 名字匹配不上 —— 唯一的"真答错"
    record_attempt(user.id, _ATTEMPT_WRONG)
    return {
        "result": IdentifyResult.NO_MATCH.value,
        "msg": "没匹配上。请核对名字的写法后重试",
        "fallback": {
            "channel": "manual_claim",
            "hint": "也可以请房长人工核实，或先用族浏览码浏览全族",
        },
    }


# ---------- 命中已有节点 ----------


def _on_matched(
    db: Session,
    *,
    user: AppUser,
    family: Family,
    graph: RelationGraph,
    body: VerifyIn,
    outcome: LocateOutcome,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    person = graph.person_of.get(outcome.person_id or 0)
    if person is None:
        record_attempt(user.id, _ATTEMPT_NEUTRAL)   # 数据异常
        return {"result": IdentifyResult.NO_MATCH.value, "msg": "没匹配上。请核对名字的写法后重试"}

    # 幂等：已经确认过身份的人再答一遍题，返回原结果而不是报错
    if user.claimed_person_id == person.id:
        reset_rate_limit(user.id)
        return {
            "result": IdentifyResult.MATCHED.value,
            "personId": person.id,
            "personName": person.name,
            "generation": person.generation,
            "generationLabel": _gen_label(person.generation),
            "grantedNodeCount": 0,
            "filledFields": [],
            "evidenceStrength": outcome.strength,
            "alreadyClaimed": True,
            "msg": "您已经确认过身份了",
        }

    # 一个人只能代表一个节点。允许多次认领会让人悄悄换身份，
    # 而"某个节点下面的人换了"这种事，房长事后几乎不可能发现。
    if user.claimed_person_id:
        raise BizError(
            ErrCode.PARAM_INVALID,
            "您已经确认过另一个节点的身份，如需更换请先联系房长",
        )

    # 决策：已故的祖先节点不能被认领。
    # 理由不是"不尊重"，而是那条路径上答案就写在树上（已故者姓名不脱敏），
    # 任何人抄一下就能过。已故祖先由后代在其家长格子上补全，不走认领。
    if person.living_status == int(LivingStatus.DECEASED):
        # 填的是真名，只是规则不允许本人认领已故者
        record_attempt(user.id, _ATTEMPT_NEUTRAL)
        return {
            "result": IdentifyResult.NO_MATCH.value,
            "msg": "没匹配上。请核对名字的写法后重试",
            "fallback": {
                "channel": "manual_claim",
                "hint": "已故先祖不由本人认领，可由后代补全其信息；如有疑问请联系房长",
            },
        }

    # 已被他人认领 —— 不先到先得，标冲突转人工
    holder = db.execute(
        select(AppUser).where(
            AppUser.claimed_person_id == person.id, AppUser.id != user.id
        )
    ).scalars().first()
    if holder is not None:
        # 答案可能全对，错在节点被别人占着 —— 绝不记在用户头上
        record_attempt(user.id, _ATTEMPT_NEUTRAL)
        raise BizError(
            ErrCode.CLAIM_CONFLICT,
            "该节点已被他人认领，请联系房长核实",
            {"hint": "如您确认这是您本人，可由房长处理冲突"},
        )

    filled = _fill_blanks(
        db,
        user=user,
        family_id=family.id,
        person_id=person.id,
        candidates=[body.extra_name, body.relative_name],
        graph=graph,
    )

    claim = Claim(
        family_id=family.id,
        user_id=user.id,
        person_id=person.id,
        claim_type=int(ClaimType.CLAIM_EXISTING),
        evidence=_evidence_text(body),
        verify_method=int(VerifyMethod.ANSWER),
        verify_answers={**snapshot, "filledFields": filled},
        status=int(ClaimStatus.APPROVED),
        review_note="答题自证通过",
        reviewed_at=datetime.now(),
        created_at=datetime.now(),
    )
    db.add(claim)

    # 认领成功 = 我确实属于这个族。切过去 + 记下"我在这个族认领了谁"。
    # claimed_person_id 现在按族分开存（user_family），父系认领"陈雨欣"、
    # 母系认领"王某某"互不覆盖；同时回填 app_user 上的当前族快照。
    join_and_activate(db, user, family.id)
    set_claimed(db, user, family.id, person.id)

    granted = _grant_self_scope(db, user=user, family_id=family.id, person_id=person.id,
                                graph=graph)

    db.add(
        Notification(
            user_id=user.id,
            family_id=family.id,
            type=1,  # NotificationType.CLAIM_RESULT
            title="身份已确认",
            body="现在可以补全您和直系亲属的信息了",
            payload={"claimId": None, "personId": person.id},
            created_at=datetime.now(),
        )
    )
    _audit(db, user=user, family_id=family.id, action="claim.auto_verified",
           target_table="person", target_id=person.id,
           detail={"strength": outcome.strength, "answers": snapshot})

    db.commit()
    reset_rate_limit(user.id)

    return {
        "result": IdentifyResult.MATCHED.value,
        "personId": person.id,
        "personName": person.name,
        "generation": person.generation,
        "generationLabel": _gen_label(person.generation),
        "grantedNodeCount": granted,
        "filledFields": filled,
        "evidenceStrength": outcome.strength,
        "evidenceNote": outcome.strength_note,
        "msg": "确认成功，欢迎回家",
    }


# ---------- 谱里没有我：建新节点 ----------


def _on_new_member(
    db: Session,
    *,
    user: AppUser,
    family: Family,
    graph: RelationGraph,
    body: VerifyIn,
    outcome: LocateOutcome,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """谱里没有本人 —— 这是接力修谱的主路径，不是异常。

    但要走一道门槛：**证据强度 weak 且没补填另一位家长**时先不放行。
    weak 意味着所填亲属已故、名字在树上公开可见，任何人都能照着建节点；
    多答一位家长的名字，成本很低，却能把这批噪音挡在外面。
    """
    if outcome.strength == "weak" and not (body.extra_name or "").strip():
        record_attempt(user.id, _ATTEMPT_NEUTRAL)   # 只是要求补填，不是答错
        return {
            "result": IdentifyResult.AMBIGUOUS.value,
            "needExtra": True,
            "candidates": [],
            "msg": "谱上还没有您。请再补填另一位家长的名字，确认是哪一支",
            "reason": outcome.strength_note,
        }

    relative = graph.person_of.get(outcome.relative_id or 0)
    if relative is None:
        record_attempt(user.id, _ATTEMPT_NEUTRAL)   # 数据异常
        return {"result": IdentifyResult.NO_MATCH.value, "msg": "没核对上，请重试"}

    if user.claimed_person_id:
        raise BizError(
            ErrCode.PARAM_INVALID,
            "您已经确认过身份了，不需要再新建节点；如需修改请联系房长",
        )

    fields, relation = _build_new_member(body, relative, graph)
    viewer = Viewer(
        scope=ViewerScope.CLOSE,
        user_id=user.id,
        role=user.role,
        family_id=family.id,
        claimed_person_id=None,
    )

    rev = submit_create(
        db,
        viewer=viewer,
        family_id=family.id,
        fields=fields,
        relation=relation,
        submit_note=f"答题自证：{_evidence_text(body)}",
        auto_merge=True,
    )
    applied = getattr(rev, "applied_result", None) or {}
    new_id = int(applied.get("personId") or 0)
    if not new_id:
        # 自动落库失败时不要假装成功：撤回这次修订，让用户走人工通道。
        rev.status = int(ClaimStatus.REJECTED)
        db.commit()
        raise BizError(ErrCode.SERVER_ERROR, "新建节点失败，请联系房长人工核实")

    # 由 self-report 建出来的节点，可信度按证据强度标，供房长抽查与后续清洗
    new_person = db.get(Person, new_id)
    if new_person is not None:
        new_person.confidence = int(
            Confidence.HIGH if outcome.strength == "strong" else Confidence.MEDIUM
        )
        new_person.source_note = f"答题自证（{outcome.strength}）：{_evidence_text(body)}"

    claim = Claim(
        family_id=family.id,
        user_id=user.id,
        person_id=new_id,
        claim_type=int(ClaimType.CREATE_SELF),
        evidence=_evidence_text(body),
        verify_method=int(VerifyMethod.ANSWER),
        verify_answers={**snapshot, "createdPersonId": new_id, "revisionId": rev.id},
        status=int(ClaimStatus.APPROVED),
        review_note="答题自证通过（新建本人节点）",
        reviewed_at=datetime.now(),
        created_at=datetime.now(),
    )
    db.add(claim)

    join_and_activate(db, user, family.id)
    set_claimed(db, user, family.id, new_id)

    # 授权时要基于新建后的关系图重算，否则"我的直系亲属"算不出来
    fresh = RelationGraph.load(db, family.id)
    granted = _grant_self_scope(db, user=user, family_id=family.id, person_id=new_id,
                                graph=fresh)

    db.add(
        Notification(
            user_id=user.id,
            family_id=family.id,
            type=1,
            title="身份已确认",
            body="家谱上原来没有您，现在已经把您接上去了",
            payload={"personId": new_id},
            created_at=datetime.now(),
        )
    )
    _audit(db, user=user, family_id=family.id, action="claim.auto_created_self",
           target_table="person", target_id=new_id,
           detail={"strength": outcome.strength, "answers": snapshot,
                   "revisionId": rev.id, "linkedTo": relative.id})

    db.commit()
    reset_rate_limit(user.id)

    return {
        "result": IdentifyResult.NEW_MEMBER.value,
        "personId": new_id,
        "personName": new_person.name if new_person else body.self_name,
        "relativeId": relative.id,
        "relativeName": relative.name,
        "generation": new_person.generation if new_person else None,
        "generationLabel": _gen_label(new_person.generation if new_person else None),
        "grantedNodeCount": granted,
        "evidenceStrength": outcome.strength,
        "msg": "家谱上原来没有您，已按您填的信息接上去了",
    }


def _build_new_member(
    body: VerifyIn, relative: Person, graph: RelationGraph
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按关系推出新节点的默认属性与挂载方式。

    默认值都是"最可能的那一种"，用户可以事后在详情页改；
    这里的目标是让新人**零表单**进门，而不是一次问全。
    """
    generation = relative.generation or 0
    role_by_relation = {
        "father": int(LineageRole.MALE),
        "mother": int(LineageRole.MALE),
        "spouse": int(LineageRole.SPOUSE_IN),
    }
    role = role_by_relation.get(body.relation, int(LineageRole.MALE))
    gender = body.self_gender or (
        int(Gender.FEMALE) if body.relation == "spouse" else int(Gender.MALE)
    )

    fields: dict[str, Any] = {
        "name": body.self_name.strip(),
        "surname": (relative.surname or (relative.name or "")[:1] or None),
        "gender": gender,
        "generation": generation + 1 if body.relation in ("father", "mother") else generation,
        "lineageRole": role,
        "livingStatus": int(LivingStatus.LIVING),
    }

    if body.relation == "spouse":
        return fields, {"type": "spouse_of", "partnerId": relative.id}

    return fields, {
        "type": "child_of",
        "parentId": relative.id,
        "unionId": _union_of(relative.id, graph),
        "relation": int(ParentRelation.BIOLOGICAL),
    }


def _union_of(person_id: int, graph: RelationGraph) -> int | None:
    """取该人所在的婚姻单元。

    必须显式带上，不能靠 parentId 让下游自己找 —— 下游只找 partner_a，
    于是"我母亲是X"会被挂到给母亲单开的一个新单元上，孩子与父亲的关系就断了。

    优先取"两口子都在"的那个单元（那才是孩子该挂的地方）。
    """
    fallback: int | None = None
    for uid, union in graph.unions.items():
        if person_id not in (union.partner_a_id, union.partner_b_id):
            continue
        if union.partner_b_id is not None and union.partner_a_id != union.partner_b_id:
            return uid
        if fallback is None:
            fallback = uid
    return fallback


# ---------- 顺手填补谱上的空位 ----------


def _fill_blanks(
    db: Session,
    *,
    user: AppUser,
    family_id: int,
    person_id: int,
    candidates: list[str | None],
    graph: RelationGraph,
) -> list[dict[str, Any]]:
    """把答案里"谱上缺的空位"补上——**只填空，绝不覆盖**。

    典型场景：答出了父亲名，顺手把另一位家长（谱上只写「配某氏」）的名字补上。
    这正是产品最强的情绪点：不是数据录入，是把一个被历史省略的人写回来。

    严格只写 name 为空的节点。家谱上的原文（哪怕繁体、哪怕异体字）
    都不许被用户输入覆盖 —— 那是史料，不是待清洗的数据。
    """
    person = graph.person_of.get(person_id)
    if person is None:
        return []

    pool = [c.strip() for c in candidates if c and c.strip()]
    if not pool:
        return []

    targets = [graph.father_of.get(person_id), graph.mother_of.get(person_id)]
    filled: list[dict[str, Any]] = []

    for tid in targets:
        if tid is None or not pool:
            continue
        node = graph.person_of.get(tid)
        if node is None or node.name:
            continue  # 有名就不动

        value = pool.pop(0)
        viewer = Viewer(scope=ViewerScope.CLOSE, user_id=user.id, role=user.role,
                        family_id=family_id, claimed_person_id=person_id)
        rev = submit_update(
            db,
            viewer=viewer,
            family_id=family_id,
            person=node,
            fields={"name": value},
            submit_note="答题自证时由后代补全",
            auto_merge=True,
        )
        filled.append(
            {
                "personId": node.id,
                "relation": "father" if tid == graph.father_of.get(person_id) else "mother",
                "before": f"{node.surname}氏" if node.surname else "配某氏",
                "after": value,
                "revisionId": rev.id,
            }
        )

    return filled


# ---------- 授权 / 留痕 / 文案 ----------


def _grant_self_scope(
    db: Session,
    *,
    user: AppUser,
    family_id: int,
    person_id: int,
    graph: RelationGraph,
) -> int:
    """认领 = 你能改自己 + 直系上下两代。这就是「每个人只填自己那一小块」的落地。"""
    scope_ids = graph.close_relatives(person_id, up=2, down=2)
    from app.core.constants import ScopeSource

    return grant_edit_scope(
        db,
        user_id=user.id,
        family_id=family_id,
        person_ids=scope_ids,
        source=ScopeSource.CLAIM_SELF,
        granted_by=user.id,
    )


def _audit(
    db: Session,
    *,
    user: AppUser,
    family_id: int,
    action: str,
    target_table: str,
    target_id: int,
    detail: dict[str, Any],
) -> None:
    db.add(
        AuditLog(
            family_id=family_id,
            user_id=user.id,
            action=action,
            target_table=target_table,
            target_id=target_id,
            detail=detail,
            created_at=datetime.now(),
        )
    )


def _evidence_text(body: VerifyIn) -> str:
    rel_label = {"father": "父亲", "mother": "母亲", "spouse": "配偶"}.get(
        body.relation, "亲属"
    )
    text = f"答题自证：我{rel_label}是{body.relative_name}，我是{body.self_name}"
    if body.extra_name:
        text += f"，另{body.extra_name}"
    return text[:500]


def _gen_label(generation: int | None) -> str | None:
    if not generation:
        return None
    from app.services.mask import cn_number

    return f"第{cn_number(generation)}世"
