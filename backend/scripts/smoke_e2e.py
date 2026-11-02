"""端到端冒烟测试：不起网络服务，直接用 ASGI 传输打应用。

## 为什么需要这个脚本

之前验证过的只有"模块能导入、OpenAPI 能生成"，那只说明没有语法错误。
真正要验证的是几条核心机制**在真实请求下是否成立**：

  1. 脱敏      —— 游客视角看不到活人全名，但看得到已故先祖全名
  2. 先审后发  —— 提交的修改在管理员批准前不落库
  3. 权限边界  —— 越权编辑被拒，未登录不能改
  4. 答题自证  —— 答出父母姓名即可确认身份，范围内免审、范围外排队、可撤销
     （【8】段。这条链路替换掉了原来的"房长逐个审批"，是身份确认的主通道）

顺带覆盖一遍完整用户旅程：游客看落地页 → 族人看"我的一家" →
补全一处缺口 → 管理员审核 → 数据落库。

## 用法

    cd backend
    .venv\\Scripts\\python.exe scripts/smoke_e2e.py

前置：已跑过 init_db.py 与 seed_demo.py（脚本会按姓名查库取 id）。
注意：本脚本会真的改数据（补上张氏的名字、给陈国华配配偶、改陈启华的职业），
     跑完如需复原请重跑 seed_demo.py。【8】段自建的夹具会在结束时自行拆除。
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys

import httpx
from sqlalchemy import or_, select, text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.constants import (  # noqa: E402
    ClaimStatus,
    LineageRole,
    LivingStatus,
    ParentRelation,
    RevisionOp,
    ReviewStatus,
    UnionEndReason,
    VerifyMethod,
)
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.family import Family  # noqa: E402
from app.models.person import FamilyUnion, ParentChild, Person  # noqa: E402
from app.models.system import Revision  # noqa: E402
from app.models.user import Claim, EditScope  # noqa: E402
from app.services.identify_service import reset_rate_limit  # noqa: E402

# app.main 里的 basicConfig(level=DEBUG) 会把 httpx 的每条请求都打出来，
# 把断言报告淹在几千行 DEBUG 里。报告是给人看的，压回去。
logging.getLogger().setLevel(logging.WARNING)

VIEW_CODE = "CHEN2026"
EDIT_CODE = "K7M2P9QT"

# 【8】入族链路专用的一次性账号。跑前跑后都只"解绑"不删除，
# 所以它永远是个没入族、没认领的空壳，可以反复拿来测"新用户"。
PROBE_OPENID = "probe_entry"


# ============================================================
# 输出与断言
# ============================================================


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed += 1
            self.failures.append(name)
            print(f"  ❌ {name}")
            if detail:
                for line in str(detail).splitlines()[:6]:
                    print(f"       {line}")
        return ok

    def note(self, text: str) -> None:
        print(f"  ·  {text}")

    def section(self, title: str) -> None:
        print(f"\n{'-' * 68}\n{title}\n{'-' * 68}")


RPT = Report()


# ============================================================
# 极简客户端
# ============================================================


class Api:
    """一个带身份的调用者。token 设上之后所有请求自动带 Authorization。"""

    def __init__(self, client: httpx.AsyncClient, label: str) -> None:
        self.c = client
        self.label = label
        self.token: str | None = None

    async def call(self, method: str, path: str, **kw) -> tuple[int, dict]:
        headers = dict(kw.pop("headers", None) or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = await self.c.request(method, path, headers=headers, **kw)
        except Exception as exc:  # 未捕获异常：当成 599 看待，别让脚本崩掉
            return 599, {"code": 599, "msg": f"{type(exc).__name__}: {exc}"}
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"code": -1, "msg": "非 JSON 响应", "raw": r.text[:300]}

    async def login(self, openid: str, invite_code: str | None = None) -> dict:
        payload: dict = {"code": f"dev:{openid}"}
        if invite_code:
            payload["inviteCode"] = invite_code
        st, body = await self.call("POST", "/v1/auth/login", json=payload)
        if st == 200 and body.get("code") == 0:
            self.token = (body.get("data") or {}).get("token")
        return body


# ============================================================
# 从库里取"真相"（用于和 API 的投影做对比）
# ============================================================


def load_fixture() -> tuple[int, dict[str, int]]:
    db = SessionLocal()
    try:
        fam = db.query(Family).order_by(Family.id).first()
        assert fam is not None, "库里没有宗族数据，请先跑 scripts/seed_demo.py"

        by_name = {
            p.name: p.id
            for p in db.query(Person).filter(Person.family_id == fam.id).all()
            if p.name
        }
        unnamed = (
            db.query(Person)
            .filter(Person.family_id == fam.id, Person.name.is_(None))
            .first()
        )
        ids = {
            "qiming": by_name["陈启明"],
            "guodong": by_name["陈国栋"],
            # 吴敏是媳妇，且**不在**启明的 edit_scope 里——用来验"范围外看不了清单"。
            # 这两个人的分工是刻意的：国栋在范围内（房长签发过），吴敏在范围外，
            # 才能证明放行/拒绝看的是权限，而不是"是不是自己"。
            "wumin": by_name["吴敏"],
            "guohua": by_name["陈国华"],
            "dechang": by_name["陈德昌"],
            "siyuan": by_name["陈思远"],
            "sitong": by_name["陈思彤"],
        }
        # 答题自证那段要用到的几位。单独取出来是因为断言里到处引用具体节点，
        # 写死 14 / 9 这种字面量，一旦种子顺序变了，失败信息会指向错误的地方。
        for key, nm in (
            ("qihua", "陈启华"),
            ("guoliang", "陈国梁"),
            ("wenshan", "陈文山"),
            ("wenhai", "陈文海"),
        ):
            ids[key] = by_name[nm]
        ids["unnamed"] = unnamed.id if unnamed else 0
        return fam.id, ids
    finally:
        db.close()


def count_rows(model, family_id: int) -> int:
    """数一张业务表里某族的行数。用于验证"落库后多了什么/没多什么"。"""
    db = SessionLocal()
    try:
        return int(db.query(model).filter(model.family_id == family_id).count())
    finally:
        db.close()


def latest_create_revision() -> tuple[int | None, dict | None, int | None]:
    """取最近一条新增修订（op=CREATE）。返回 (id, payload, status)。

    不再只看 PENDING：房长自己的写入现在是**直接生效**的（AUTO_MERGED），
    修订照样落行、payload 照样完整，所以回归要读"最后一条"，
    而不是"最后一条待审的"——后者在房长路径上永远是空的。
    """
    db = SessionLocal()
    try:
        row = (
            db.query(Revision)
            .filter(Revision.op == int(RevisionOp.CREATE))
            .order_by(Revision.id.desc())
            .first()
        )
        return (row.id, row.payload, row.status) if row else (None, None, None)
    finally:
        db.close()


def name_of(person_id: int) -> str | None:
    db = SessionLocal()
    try:
        p = db.get(Person, person_id)
        return p.name if p else None
    finally:
        db.close()


def pending_revision_id(target_id: int) -> int | None:
    db = SessionLocal()
    try:
        row = (
            db.query(Revision)
            .filter(
                Revision.target_table == "person",
                Revision.target_id == target_id,
                Revision.status == 1,
            )
            .order_by(Revision.id.desc())
            .first()
        )
        return row.id if row else None
    finally:
        db.close()


# ---------- 临时夹具：测试自己造数据，用完自己拆 ----------


def scalar(sql: str, **kw):
    db = SessionLocal()
    try:
        return db.execute(text(sql), kw).scalar()
    finally:
        db.close()


def db_count(sql: str, **kw) -> int:
    return int(scalar(sql, **kw) or 0)


def db_first(sql: str, **kw):
    """取第一行（Row）。比 scalar 多用于多列查询。"""
    db = SessionLocal()
    try:
        return db.execute(text(sql), kw).first()
    finally:
        db.close()


def db_add_person(
    family_id: int,
    *,
    name: str | None,
    surname: str,
    gender: int,
    generation: int,
    role: LineageRole,
    living: LivingStatus,
) -> int:
    db = SessionLocal()
    try:
        p = Person(
            family_id=family_id,
            name=name,
            surname=surname,
            gender=gender,
            generation=generation,
            lineage_role=int(role),
            living_status=int(living),
            privacy_level=1,
            status=1,
            source_type=2,
            confidence=2,
            verify_status=1,
        )
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


def db_add_union(
    family_id: int,
    a: int,
    b: int,
    *,
    seq: int = 1,
    union_type: int = 1,
    end_reason: int | None = None,
) -> int:
    db = SessionLocal()
    try:
        u = FamilyUnion(
            family_id=family_id, partner_a_id=a, partner_b_id=b,
            union_type=union_type, union_seq=seq, end_reason=end_reason, status=1,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def db_add_child(family_id: int, union_id: int, child_id: int) -> None:
    db = SessionLocal()
    try:
        db.add(
            ParentChild(
                family_id=family_id, union_id=union_id, child_id=child_id,
                relation=int(ParentRelation.BIOLOGICAL),
                is_primary=1, is_biological=1, seq=1, status=1,
            )
        )
        db.commit()
    finally:
        db.close()


def db_drop_fixture(*person_ids: int, openids: tuple[str, ...] = ()) -> None:
    """拆掉测试自己造的人和关系。

    两件事都不能省：
    - 关系与修订要一起删：只删 person 会留下指向不存在节点的行，
      下一轮跑鉴权/布局时症状很怪。
    - 用户不能删，但必须**解绑**：openid 是固定的（mock_a…），
      留着 claimed_person_id 指向已删除的节点，下一轮再答题会直接
      撞上"您已经确认过另一个节点"——一个看起来像业务 bug 的假故障。
    """
    db = SessionLocal()
    try:
        for oid in openids:
            row = db.execute(
                text("SELECT id FROM app_user WHERE openid=:o"), {"o": oid}
            ).first()
            if not row:
                continue
            uid = int(row[0])
            db.execute(text("DELETE FROM edit_scope WHERE user_id=:u"), {"u": uid})
            db.execute(text("DELETE FROM claim WHERE user_id=:u"), {"u": uid})
            db.execute(text("DELETE FROM notification WHERE user_id=:u"), {"u": uid})
            # user_family 必须一起清：多族之后"解绑"= 把 app_user.family_id 置空
            # 已经不够了，成员关系还在就得再 join，下一轮这段就测不出"新账号"
            db.execute(text("DELETE FROM user_family WHERE user_id=:u"), {"u": uid})
            db.execute(
                text("UPDATE app_user SET claimed_person_id=NULL, family_id=NULL "
                     "WHERE id=:u"),
                {"u": uid},
            )
        for pid in person_ids:
            db.execute(text("DELETE FROM parent_child WHERE child_id=:i"), {"i": pid})
            db.query(Revision).filter(Revision.target_id == pid).delete()
            db.query(EditScope).filter(EditScope.person_id == pid).delete()
        for pid in person_ids:
            db.execute(text("DELETE FROM person WHERE id=:i"), {"i": pid})
        db.commit()
    finally:
        db.close()


# ============================================================
# 各段测试
# ============================================================


async def part_guest(client: httpx.AsyncClient, fid: int, ids: dict[str, int]) -> Api:
    RPT.section("【1】游客：不登录能看到什么")

    anon = Api(client, "anon")

    st, body = await anon.call("GET", "/health")
    RPT.check("健康检查可用", st == 200, f"HTTP {st} {body}")

    st, body = await anon.call("GET", f"/v1/invite/{VIEW_CODE}")
    RPT.check("族浏览码可预览（微信群传播的第一屏）", st == 200 and body.get("code") == 0,
              f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")

    st, body = await anon.call("GET", f"/v1/invite/{EDIT_CODE}")
    RPT.check("编辑码可预览（带范围和节点数）", st == 200 and body.get("code") == 0,
              f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")

    st, body = await anon.call("GET", f"/v1/invite/{'NOPECODE'}")
    RPT.check("无效邀请码返回 40302，不是 500",
              body.get("code") == 40302, f"HTTP {st} {body}")

    # 游客登录（新族人从家族群点进来）
    body = await anon.login("mock_new_member", VIEW_CODE)
    RPT.check("新族人可登录并拿到 token", bool(anon.token),
              json.dumps(body, ensure_ascii=False)[:200])

    await anon.call("POST", "/v1/auth/login", json={"code": "dev:mock_new_member"})
    return anon


async def part_mask(client: httpx.AsyncClient, fid: int, ids: dict[str, int]) -> None:
    RPT.section("【2】脱敏：这是家谱类产品最容易出事的地方")

    # 用真正不带 token 的身份。这和"登录但未认领"是两种视角，不能混着测。
    pure = Api(client, "pure-anon")

    st, body = await pure.call("GET", f"/v1/tree/{fid}?view=overview")
    if not RPT.check("全族总览可返回", st == 200 and body.get("code") == 0,
                     f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}"):
        return

    raw = json.dumps(body, ensure_ascii=False)
    data = body.get("data") or {}
    nodes = data.get("nodes") or []
    RPT.note(f"总览返回 {len(nodes)} 个节点")

    RPT.check("未登录视角不出现在世者全名「陈启明」", "陈启明" not in raw)
    RPT.check("未登录视角不出现在世者全名「陈国栋」", "陈国栋" not in raw)
    RPT.check("未登录视角出现脱敏形式（含 * 号）", "*" in raw)
    RPT.check("已故先祖全名可见（陈德昌）—— 否则家谱就失去意义了",
              "陈德昌" in raw)

    roles_overview = {n.get("role") for n in nodes}
    RPT.check("★ 外孙不出现在全族总览（决策第 2 条的另一半）",
              5 not in roles_overview,
              f"role 集合：{sorted(r for r in roles_overview if r is not None)}")
    RPT.check("外姓配偶不出现在全族总览（世系树只画承袭世系）",
              4 not in roles_overview,
              f"role 集合：{sorted(r for r in roles_overview if r is not None)}")

    # ---------- 「认领」才是姓名脱敏的分界线（2026-10-16 口径）----------
    #
    # 这里刻意用一次性账号自己造出"已入族但未认领"，**不用 mock_new_member**：
    # 那是演示账号，手工测试时很可能已经被认领了（启明在小程序里走一遍答题
    # 自证就会把它认领掉），断言随之静默空转——报告上是绿的，实际什么都没测。
    # 这正是"不幂等"脚本最阴的一类失败：不是红，是假绿。
    db_drop_fixture(openids=(PROBE_OPENID,))
    reset_rate_limit()
    newcomer = Api(client, "unclaimed")
    try:
        await newcomer.login(PROBE_OPENID)
        await newcomer.call("POST", f"/v1/invite/{VIEW_CODE}/accept")

        st2, body2 = await newcomer.call("GET", f"/v1/tree/{fid}?view=overview")
        raw2 = json.dumps(body2, ensure_ascii=False)
        d2 = body2.get("data") or {}
        RPT.check("已登录、已入族但**未认领** → 看总览仍被挡在世者全名之外"
                  "（★ 这条别松开：族浏览码会被转发到族外）",
                  st2 == 200 and "陈国栋" not in raw2 and "*" in raw2,
                  f"HTTP {st2} viewerScope={d2.get('viewerScope')} "
                  f"漏出全名={'陈国栋' in raw2}")

        # 未认领用户看"我的一家"：应给明确提示，由前端跳认领引导页，而不是 500
        st3, body3 = await newcomer.call("GET", f"/v1/tree/{fid}?view=mine")
        RPT.check("未认领用户看「我的一家」返回明确提示，不是 500",
                  st3 == 200 and body3.get("code") in (0, 40001),
                  f"HTTP {st3} {json.dumps(body3, ensure_ascii=False)[:300]}")
        if body3.get("code") == 40001:
            RPT.note(f"提示语：{body3.get('msg')}（前端据此跳认领引导页）")
    finally:
        db_drop_fixture(openids=(PROBE_OPENID,))

    # 认领之后：全族在世者姓名都该给出来，**包括旁系**。
    # 陈国梁是启明（四世）的三世伯父——不在他的近亲集合里，
    # 正是改口径之前被糊掉的那一类人。
    claimed = Api(client, "claimed")
    await claimed.login("mock_qiming")
    st4, body4 = await claimed.call("GET", f"/v1/tree/{fid}?view=overview")
    raw4 = json.dumps(body4, ensure_ascii=False)
    d4 = body4.get("data") or {}
    still = [f"{n.get('id')} {n.get('displayName')}" for n in (d4.get("nodes") or [])
             if "*" in (n.get("displayName") or "")]
    RPT.check("★ 已认领后看得到旁系在世者的全名（陈国梁 = 三世伯父）",
              st4 == 200 and "陈国梁" in raw4,
              f"viewerScope={d4.get('viewerScope')}，仍打码："
              + ("、".join(still) or "（无）"))

    # 只放开了**姓名**。生日精度、职业、生平仍按近亲口径守着——
    # 否则一次改动就把个人信息面放大了一整圈。
    st5, body5 = await claimed.call("GET", f"/v1/person/{ids['guoliang']}")
    p5 = (body5.get("data") or {}) if st5 == 200 else {}
    birth = (p5.get("birth") or {}).get("display") if p5.get("birth") else "（无）"
    RPT.check("已认领后旁系的姓名给了，但生日仍只到年、职业仍隐藏（只放开姓名）",
              p5.get("displayName") == "陈国梁"
              and (p5.get("birth") is None
                   or (p5.get("birth") or {}).get("precision") == 1)
              and "occupation" not in p5,
              f"displayName={p5.get('displayName')} 生日={birth} "
              f"occupation={p5.get('occupation', '（未返回）')}")


async def part_member(client: httpx.AsyncClient, fid: int,
                      ids: dict[str, int]) -> Api:
    RPT.section("【3】族人陈启明：登录 → 我的一家 → 待补全")

    me = Api(client, "qiming")
    body = await me.login("mock_qiming")
    RPT.check("陈启明可登录", bool(me.token),
              json.dumps(body, ensure_ascii=False)[:200])

    st, body = await me.call("GET", "/v1/auth/me")
    data = body.get("data") or {}
    user = data.get("user") or data
    claimed = user.get("claimedPersonId") if isinstance(user, dict) else None
    RPT.check("已认领身份指向陈启明本人节点", claimed == ids["qiming"],
              f"claimed={claimed} 期望={ids['qiming']}")

    st, body = await me.call("GET", f"/v1/tree/{fid}?view=mine&anchor={ids['qiming']}")
    ok = RPT.check("「我的一家」可返回", st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if ok:
        data = body.get("data") or {}
        nodes = data.get("nodes") or []
        edges = data.get("edges") or []
        canvas = data.get("canvas") or {}
        RPT.note(f"节点 {len(nodes)}　边 {len(edges)}　画布 {canvas.get('width')}×{canvas.get('height')}")
        RPT.check("每个节点都带服务端算好的 x/y（端上只做绝对定位）",
                  bool(nodes) and all("x" in n and "y" in n for n in nodes))
        # 这两个字段丢了不会报错，只会让端上算出 NaN：线画不出来、锚点不居中。
        # 所以必须专门盯住——上一版就是在这里退化的。
        RPT.check("★ 每个节点都带 w/h（端上算连线和居中都要用）",
                  bool(nodes) and all(
                      isinstance(n.get("w"), (int, float))
                      and isinstance(n.get("h"), (int, float))
                      for n in nodes),
                  f"第一个节点：{json.dumps(nodes[0], ensure_ascii=False) if nodes else '无'}")
        # 边只给 from/to，坐标由端上从 nodes 里查——省流量，且缩放时不会错位
        RPT.check("每条边都带 from/to（端上据此连线）",
                  bool(edges) and all("from" in e and "to" in e for e in edges),
                  json.dumps(edges[:2], ensure_ascii=False))
        edge_ids = {n.get("id") for n in nodes}
        dangling = [e for e in edges if e.get("from") not in edge_ids or e.get("to") not in edge_ids]
        RPT.check("没有指向范围外节点的悬空边（连线会接不上）",
                  not dangling, json.dumps(dangling[:3], ensure_ascii=False))

        # 截断提示是**唯一教用户怎么走得更远的地方**。指向一个已经删掉的入口，
        # 用户会去找那个按钮（找不到），或者以为自己点错了。
        # 这是文案回归里最容易复发的一类：视图删了，提示词还留在原地。
        tr = data.get("truncated") or {}
        hint = tr.get("hint") or ""
        RPT.check(
            "★ 截断提示不得指向已退休的「我的支系」",
            "支系" not in hint and "lineage" not in hint,
            f"hint={hint}",
        )
        RPT.check(
            "被截断却没有任何提示（用户不知道上面还有更早的祖先）",
            (not tr.get("up")) or bool(hint),
            f"truncated={json.dumps(tr, ensure_ascii=False)}",
        )

        names = [n.get("name") for n in nodes]
        roles = {n.get("role") for n in nodes}

        # LineageRole: 1承袭男 2承袭女 3配偶入 4配偶出 5外姓后代(外孙)
        RPT.check("★ 外孙出现在「我的一家」（决策第 2 条：外孙只在我的一家显示）",
                  5 in roles,
                  f"role 集合：{sorted(r for r in roles if r is not None)}")
        RPT.check("锚点本人被标成 isSelf（这一屏唯一要确认的事就是「我在哪」）",
                  sum(1 for n in nodes if n.get("isSelf")) == 1,
                  f"isSelf 节点数：{sum(1 for n in nodes if n.get('isSelf'))}")
        RPT.check("从始祖到锚点的路径被标成 onPath（树上那条金线）",
                  any(n.get("onPath") for n in nodes))
        RPT.note("节点姓名样本：" + "、".join(str(x) for x in names[:8]))

    st, body = await me.call("GET", f"/v1/tree/{fid}/search?q=陈国栋")
    RPT.check("搜人可用", st == 200 and body.get("code") == 0,
              json.dumps(body, ensure_ascii=False)[:200])

    st, body = await me.call("GET", f"/v1/person/{ids['qiming']}")
    RPT.check("本人详情可看", st == 200 and body.get("code") == 0,
              json.dumps(body, ensure_ascii=False)[:200])

    st, body = await me.call("GET", f"/v1/person/{ids['qiming']}/missing")
    RPT.check("自己的待补全清单可返回", st == 200 and body.get("code") == 0,
              json.dumps(body, ensure_ascii=False)[:300])
    if body.get("code") == 0:
        d = body.get("data") or {}
        items = d.get("items") or []
        RPT.note(f"陈启明待补全 {len(items)} 条：" +
                 "、".join(str(i.get("question")) for i in items[:3]))
        # ★ 完整度与清单必须是同一套口径。曾经各算各的：
        #   完整度自算 6 项（含配偶/生平），清单另算——
        #   结果出现「您这一格已经完整了 67%」（用户实报的 bug）。
        mc = int(d.get("missingCount") or 0)
        cp = float(d.get("completeness") or 0)
        RPT.check("★ missingCount 与完整度同口径：缺 0 项 ⟺ 100%",
                  (mc == 0) == (cp >= 1.0),
                  f"missingCount={mc} completeness={cp}")

    return me


async def part_unnamed(client: httpx.AsyncClient, fid: int,
                       ids: dict[str, int], admin: Api) -> None:
    """「配某氏」—— 整个产品最有价值的一条路径。"""
    RPT.section("【4】配某氏：把被历史省略的人写回来")

    if not ids["unnamed"]:
        RPT.check("种子数据里存在无名者（配某氏）", False, "库里没有 name 为空的 person")
        return
    RPT.check("种子数据里存在无名者（配某氏）", True)
    RPT.note(f"无名者 person_id = {ids['unnamed']}，当前 name = {name_of(ids['unnamed'])!r}")

    st, body = await admin.call("GET", f"/v1/person/{ids['unnamed']}/missing")
    ok = RPT.check("无名者的待补全清单可返回", st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if not ok:
        return

    items = (body.get("data") or {}).get("items") or []
    keys = [i.get("key") for i in items]
    RPT.note(f"任务 key：{keys}")

    if not RPT.check("无名者自己产生「补全姓名」任务（self_name）",
                     "self_name" in keys,
                     "这是本产品最核心的补全路径，缺失意味着讲不出故事"):
        return

    item = next(i for i in items if i.get("key") == "self_name")
    RPT.check("任务带 displayBefore（前后对比是产品最强情绪点）",
              bool(item.get("displayBefore")),
              json.dumps(item, ensure_ascii=False)[:300])
    RPT.check("任务带 reason（用户不反感被问，反感莫名其妙被问）",
              bool(item.get("reason")),
              json.dumps(item, ensure_ascii=False)[:300])
    RPT.note(f"问句：{item.get('question')}")
    RPT.note(f"提示：{item.get('hint')}")

    # ---------- 完整度口径：未成年（2008 后出生）这一格 ----------
    # 陈思彤：在世、2016 年生、父母齐全有姓名、有出生日期。
    # 旧口径自算 6 项，配偶/生平两项她永远填不了 → 恒 67%，
    # 而 missingCount=0 → 端上显示「您这一格已经完整了 67%」（用户实报）。
    st, body = await admin.call("GET", f"/v1/person/{ids['sitong']}/missing")
    ok = RPT.check("未成年的待补全清单可返回（admin 视角）",
                   st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if ok:
        d = body.get("data") or {}
        mc = int(d.get("missingCount") or 0)
        cp = float(d.get("completeness") or 0)
        RPT.check("★ 未成年缺 0 项时完整度必须 100%（适用项才计分，"
                  "不拿她填不了的配偶/生平压分）",
                  mc == 0 and cp >= 1.0,
                  f"missingCount={mc} completeness={cp}")


async def part_review(client: httpx.AsyncClient, fid: int, ids: dict[str, int],
                      admin: Api, me: Api) -> None:
    """两条写入路径，行为不同，必须都测。

    改造后的口径（见 person.py `_resolve_write_mode`）：
      - 房长自己 / 已认领且目标在授权范围内 → **直接生效**（写 AUTO_MERGED 修订留痕）
      - 已认领但目标在授权范围外           → 进待审队列，房长批了才落库
      - 未认领                             → 40301

    只测一条就会漏：只测"先审后发"会以为免审没做，只测免审会以为审核链路断了。
    """
    RPT.section("【5】两条写入路径：范围免审 / 范围外先审后发")

    # ---------- A. 房长补写「配某氏」：立即生效 ----------
    RPT.note("A. 房长补上被历史省略的那一位（产品最强的情绪点）")
    target = ids["unnamed"]
    before = name_of(target)
    new_name = "张桂英"

    st, body = await admin.call(
        "POST",
        f"/v1/person/{target}/missing/submit",
        json={"key": "self_name", "values": {"name": new_name}},
    )
    data = body.get("data") or {}
    ok = RPT.check("房长提交补全任务成功", st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if ok:
        RPT.check("★ 房长的写入直接生效（autoMerged=true）—— 房长审批自己毫无意义",
                  data.get("autoMerged") is True,
                  json.dumps(data, ensure_ascii=False)[:300])
        RPT.check("落库生效", name_of(target) == new_name,
                  f"库里是 {name_of(target)!r}，期望 {new_name!r}")
        RPT.check("但仍然留下 AUTO_MERGED 修订（生效 ≠ 无痕，可回滚）",
                  data.get("status") == int(ReviewStatus.AUTO_MERGED),
                  f"status={data.get('status')}（4=AUTO_MERGED）")

        st, body = await admin.call("GET", f"/v1/person/{target}")
        RPT.check("详情页能看到补上的名字", new_name in json.dumps(body, ensure_ascii=False),
                  json.dumps(body, ensure_ascii=False)[:200])

    # ---------- B. 普通族人改范围外的节点：排队 ----------
    RPT.note("B. 族人改到授权范围之外（陈启明 → 陈德昌，一代之前的先祖）")
    out_target = ids["dechang"]
    occ_before = scalar("SELECT occupation FROM person WHERE id=:i", i=out_target)
    new_occ = "农夫"

    st, body = await me.call(
        "PATCH", f"/v1/person/{out_target}",
        json={"fields": {"occupation": new_occ}, "submitNote": "冒烟测试：范围外修改"},
    )
    data = body.get("data") or {}
    ok = RPT.check("范围外修改被受理（不阻塞，改为排队）",
                   st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if not ok:
        return

    RPT.check("★ 提交后 person 表【未】立即变更（先审后发）",
              scalar("SELECT occupation FROM person WHERE id=:i", i=out_target) == occ_before,
              f"库里变成 {scalar('SELECT occupation FROM person WHERE id=:i', i=out_target)!r} 了，"
              "说明范围外的写绕过了审核")

    rev_id = pending_revision_id(out_target)
    if not RPT.check("revision 表产生了待审记录", rev_id is not None,
                     "没有 revision 记录，审核链路断了"):
        return
    RPT.note(f"待审修订 id = {rev_id}")

    st, body = await admin.call("GET", f"/v1/admin/revisions?familyId={fid}")
    RPT.check("管理员待审列表可拉取", st == 200 and body.get("code") == 0,
              f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")

    st, body = await admin.call("GET", f"/v1/admin/dashboard?familyId={fid}")
    RPT.check("族看板可拉取（数据完整度进度条的数据源）",
              st == 200 and body.get("code") == 0,
              f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")

    # 注意：这个接口要求带一个（可为空的）JSON body。
    # FastAPI 把无默认值的 Pydantic body 参数视为必填，即使模型里没有必填字段。
    # 所以前端调这类"点一下"的写操作要统一 post(path, {})，别省略 body，
    # 否则会收到 pydantic 的「Field required」，而不是有业务含义的错误。
    st, body = await admin.call(
        "POST", f"/v1/admin/revision/{rev_id}/approve", json={}
    )
    ok = RPT.check("管理员批准修订", st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if not ok:
        return

    RPT.check("★ 批准后数据才落库（这才是「先审后发」的完整含义）",
              scalar("SELECT occupation FROM person WHERE id=:i", i=out_target) == new_occ,
              f"库里仍是 {scalar('SELECT occupation FROM person WHERE id=:i', i=out_target)!r}")


async def part_perm(client: httpx.AsyncClient, fid: int, ids: dict[str, int],
                    me: Api) -> None:
    RPT.section("【6】权限边界：宁可偏紧不可偏松")

    anon = Api(client, "anon")
    st, body = await anon.call(
        "POST", f"/v1/person/{ids['qiming']}/missing/submit",
        json={"key": "self_name", "values": {"name": "x"}},
    )
    RPT.check("未登录不能提交修改（40100）",
              body.get("code") == 40100, f"{body}")

    st, body = await me.call(
        "GET", f"/v1/person/{ids['guodong']}/missing",
    )
    RPT.check(
        "编辑范围内的亲属能看缺口清单（与详情页 pending 同一判据）",
        body.get("code") == 0 and (body.get("data") or {}).get("missingCount") is not None,
        f"{body}",
    )

    st, body = await me.call(
        "POST", f"/v1/person/{ids['wumin']}/missing/submit",
        json={"key": "self_name", "values": {"name": "x"}},
    )
    RPT.check(
        "编辑范围外的人不能替他提交（40300）——能补才看得到，看得到就能补",
        body.get("code") == 40300, f"{body}",
    )

    st, body = await me.call(
        "GET", f"/v1/person/{ids['wumin']}/missing",
    )
    RPT.check(
        "编辑范围外看缺口清单也 40300（不能只挡写不挡读）",
        body.get("code") == 40300, f"{body}",
    )

    # 底部条：数字属于锚点，就必须自报锚点是谁。
    # 端上原来拿 claimedPersonId 去「去补全」，于是"点吴敏还差一项、
    # 点进去说陈启华已补全"——同一条入口说两个人（2026-10-21 实报）。
    st, body = await me.call(
        "GET", f"/v1/tree/{fid}?view=mine&anchor={ids['guodong']}",
    )
    p = (body.get("data") or {}).get("pending") or {}
    RPT.check(
        "★ 底部条自报缺口属于谁（personId = 锚点，isSelf = false）",
        p.get("personId") == ids["guodong"] and p.get("isSelf") is False,
        f"pending={p}",
    )

    st, body = await me.call(
        "GET", f"/v1/tree/{fid}?view=mine&anchor={ids['wumin']}",
    )
    RPT.check(
        "★ 编辑范围外的锚点不返回 pending（底部条与角标一起消失，不留死按钮）",
        not ((body.get("data") or {}).get("pending")),
        f"pending={(body.get('data') or {}).get('pending')}",
    )

    st, body = await me.call(
        "POST", f"/v1/person/{ids['qiming']}/missing/submit",
        json={"key": "nonexistent_key", "values": {"name": "x"}},
    )
    RPT.check("脏 key 被拦（40001），前端传错不会污染数据",
              body.get("code") == 40001, f"{body}")

    # ---- 编辑码已下线（2026-10-22）----
    # 它试图让"链接带权限"，等于在认领之外开一条查不到人的写权限后路。
    # 现在只允许生成族级浏览码；个人分享带的是 anchor（视角），不是权限。
    st, body = await me.call(
        "POST",
        "/v1/invite/generate",
        json={"familyId": fid, "codeType": 2, "anchorPersonId": ids["qiming"],
              "inviteNote": "冒烟测试"},
    )
    RPT.check(
        "★ 个人编辑码已被拒绝（40001）—— 权限只能来自认领，不能由链接携带",
        body.get("code") == 40001,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}",
    )

    st, body = await me.call(
        "POST", "/v1/invite/generate", json={"familyId": fid, "codeType": 1}
    )
    RPT.check(
        "族人不能生成全族浏览码（40300）—— 扩散的开关留给族里",
        body.get("code") == 40300,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}",
    )

    # 分享能不能"点开就进"，取决于 /family/mine 有没有带回浏览码。
    # 码是懒生成的：普通族人只要在族里，第一次读这条接口就会补一个。
    st, body = await me.call("GET", "/v1/family/mine")
    data = body.get("data") or {}
    vcode = (data.get("family") or {}).get("viewCode")
    RPT.check(
        "★ /family/mine 必须带浏览码（否则分享出去的链接没有码，对方还得手输）",
        body.get("code") == 0 and bool(vcode),
        f"viewCode={vcode!r} {json.dumps(data, ensure_ascii=False)[:200]}",
    )

    if vcode:
        # 分享链接的锚点只影响落地页看谁，不应该带来任何写权限
        st, body = await anon.call(
            "GET", f"/v1/invite/{vcode}?previewPersonId={ids['qiming']}"
        )
        d = body.get("data") or {}
        RPT.check(
            "★ 分享链接带锚点：落地页预览以分享人为中心",
            body.get("code") == 0
            and ((d.get("anchor") or {}).get("id") == ids["qiming"]),
            f"anchor={(d.get('anchor') or {}).get('id')} expect={ids['qiming']}",
        )

        st, body = await me.call(
            "GET", f"/v1/invite/{vcode}/scope"
        )
        # 浏览码没有范围快照，这条接口对它应当给出空范围而不是 500
        RPT.check(
            "浏览码调范围接口不炸（历史编辑码才用得到）",
            body.get("code") in (0, 40302, 40001),
            f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
        )

        # 锚点要经服务端校验后再回来：链接里的 query 可能指到别的族的人，
        # 端上不能只信 URL（否则「我的一家」会拿一个不属于本族的 anchor 去请求）
        share_guest = Api(client, "share_guest")
        await share_guest.login("mock_share_anchor_guest")
        st, body = await share_guest.call(
            "POST", f"/v1/invite/{vcode}/accept",
            json={"anchorPersonId": ids["qiming"]},
        )
        d = body.get("data") or {}
        RPT.check(
            "★ 分享锚点随入族一起回来（端上不必只信 URL 参数）",
            body.get("code") == 0 and d.get("recommendAnchor") == ids["qiming"],
            f"{json.dumps(d, ensure_ascii=False)[:220]}",
        )

    st, body = await me.call("POST", "/v1/invite/999999/revoke")
    RPT.check("吊销不存在的邀请码返回 40401，不是 500",
              body.get("code") == 40401, f"HTTP {st} {body}")


async def part_spouse_regression(client: httpx.AsyncClient, fid: int,
                                 ids: dict[str, int], admin: Api) -> None:
    """回归：配偶任务必须挂成婚姻，不能挂成子女。

    这条路径踩过坑——「配偶」任务的 prefill 用 partnerAId，
    而关系推断函数当初不认识这个键，静默退化成 child_of，
    结果配偶被挂成了子女。这类错误在树上是"看起来正常"的，
    可能要过很久才被发现，代价是整支世系错位。

    所以这里不是静态检查字段名，而是真的提交一次、落库一次，
    再数一遍亲子关系有没有凭空多出来。用管理员跑，因为他能看任何人的清单，
    而且他提交的写入会直接生效（免审），不需要中间再插一步人工批准。
    """
    RPT.section("【7】回归：配偶必须挂成婚姻，不能挂成子女")

    st, probe = await admin.call("GET", "/v1/auth/me")
    who = ((probe.get("data") or {}).get("user") or {}) if probe.get("code") == 0 else {}
    RPT.check("管理员会话有效（role=3 为族管理员）",
              bool(who) and who.get("role") == 3,
              f"HTTP {st} {json.dumps(probe, ensure_ascii=False)[:200]}")

    # 陈国华：在世、成年（1972 年生）、无婚姻记录 —— 清单里必然有一条「配偶」任务
    target = ids["guohua"]
    st, body = await admin.call("GET", f"/v1/person/{target}/missing")
    if body.get("code") != 0:
        RPT.check("取到陈国华的待补全清单", False, f"{body}")
        return

    items = (body.get("data") or {}).get("items") or []
    RPT.note("陈国华的任务：" +
             "、".join(f"{i.get('key')}({i.get('type')})" for i in items))

    RPT.check("清单任务的 type 都在预期集合内",
              bool(items) and all(
                  i.get("type") in ("create_person", "create_union", "update_field")
                  for i in items),
              json.dumps(items, ensure_ascii=False)[:400])

    keys = [i.get("key") for i in items]
    if not RPT.check("陈国华有「配偶」任务", "spouse" in keys, f"实际 key：{keys}"):
        return

    item = next(i for i in items if i["key"] == "spouse")
    prefill = item.get("prefill") or {}
    RPT.check("「配偶」任务类型是 create_union",
              item.get("type") == "create_union",
              json.dumps(item, ensure_ascii=False)[:300])
    RPT.check("「配偶」任务的挂载键是 partnerAId（不是 childId）",
              "partnerAId" in prefill and "childId" not in prefill,
              json.dumps(prefill, ensure_ascii=False))

    unions_before = count_rows(FamilyUnion, fid)
    links_before = count_rows(ParentChild, fid)
    RPT.note(f"提交前：婚姻 {unions_before} 段，亲子 {links_before} 条")

    st, body = await admin.call(
        "POST", f"/v1/person/{target}/missing/submit",
        json={"key": "spouse", "values": {"partnerName": "王丽"}},
    )
    ok = RPT.check("提交「配偶」任务成功（partnerName 要能当成新人的姓名）",
                   st == 200 and body.get("code") == 0,
                   f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}")
    if not ok:
        return

    rev_id, payload, rev_status = latest_create_revision()
    RPT.check("房长的写入直接生效（AUTO_MERGED）—— 房长不需要审批自己",
              rev_status == int(ReviewStatus.AUTO_MERGED),
              f"status={rev_status}（4=AUTO_MERGED，1=待审）")
    if not RPT.check("修订里记下了挂载关系（生效与否都要留痕）",
                     bool(payload and payload.get("relation")),
                     json.dumps(payload, ensure_ascii=False)[:300]):
        return

    rel = payload.get("relation") or {}
    fields = payload.get("fields") or {}
    RPT.check("★ 挂载类型是 spouse_of（不是 child_of）",
              rel.get("type") == "spouse_of",
              f"实际 type={rel.get('type')}　payload={json.dumps(payload, ensure_ascii=False)}")
    RPT.check("partnerName 已转成新人的 name 字段（否则会被静默过滤掉）",
              fields.get("name") == "王丽",
              json.dumps(fields, ensure_ascii=False))

    unions_after = count_rows(FamilyUnion, fid)
    links_after = count_rows(ParentChild, fid)
    RPT.note(f"提交后：婚姻 {unions_after} 段，亲子 {links_after} 条")

    RPT.check("落库后多了一段婚姻",
              unions_after == unions_before + 1,
              f"{unions_before} → {unions_after}")
    RPT.check("★ 亲子关系没有凭空多出来（挂错就会在这里暴露）",
              links_after == links_before,
              f"{links_before} → {links_after}")


async def part_entry(client: httpx.AsyncClient, fid: int, ids: dict[str, int]) -> None:
    """入族链路 —— 「我明明已经在族里了，还让我输家族码」那个 bug 的回归。

    症状（启明在开发者工具里实跑时撞到的）：
        打开小程序，首页照常渲染出广信陈氏的全树，看着像早就进族了；
        去确认身份，却被回一句"请先通过族浏览码或邀请码进入某个宗族"。

    根因不在后端、也不在认领页，而在**首页把两种状态压成了一种**：
        后端不可达        —— 问不到（网络问题）
        后端答复：没入族  —— 问到了，明确不在任何族
    这两条以前都由 `if (!fid) fid = DEMO_FAMILY_ID` 兜成"渲染 1 号族全树"；
    可后端始终按 app_user.family_id 判断，于是页面和后端各说各话。

    修法是把这两条拆开。而拆开有个**前提**：后端必须能"明确答复没入族"，
    而不是含糊地报个错——报错的话端上根本分不清是网络问题还是真没入族。
    本段验证的正是这个前提，以及"输码入族"确实是解开那句报错的钥匙：

        新账号 → /family/mine 明确回 joined=false（HTTP 200，不是报错）
               → 此时答题自证 40001（复现原症状）
               → 用族浏览码入族
               → 答题自证立刻可用

    顺带钉住一条容易被"免审"心态带偏的边界：**浏览码只入族，不给任何编辑权**。
    """
    RPT.section("【8】入族链路：未入族 → 输家族码 → 答题自证才可用")

    db_drop_fixture(openids=(PROBE_OPENID,))
    reset_rate_limit()

    try:
        api = Api(client, PROBE_OPENID)
        await api.login(PROBE_OPENID)

        _, body = await api.call("GET", "/v1/auth/me")
        me = body.get("data") or {}
        RPT.check("新账号确实还没入任何族（familyId 为空）",
                  me.get("familyId") is None,
                  f"familyId={me.get('familyId')}")

        # ---------- ① 端上拆分状态的判据 ----------
        st, body = await api.call("GET", "/v1/family/mine")
        d = body.get("data") or {}
        RPT.check("★ /family/mine 对未入族者是【明确答复】而不是报错"
                  "（首页就是靠它区分「问不到」和「没入族」）",
                  st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")
        RPT.check("答复内容明确：family=null 且 joined=false",
                  d.get("family") is None and d.get("joined") is False,
                  json.dumps(d, ensure_ascii=False)[:200])

        # ---------- ② 复现原症状 ----------
        _, body = await api.call("GET", "/v1/auth/identify/quiz")
        msg = body.get("msg") or ""
        RPT.check("未入族时答题自证被拒 40001 —— 这就是原来弹给用户的那句话",
                  body.get("code") == 40001,
                  f"code={body.get('code')} msg={msg}")
        RPT.check("这句话提的是族浏览码/邀请码，而不是身份验证失败"
                  "（说明它和答题机制无关，是入族前置没满足）",
                  "浏览码" in msg or "邀请码" in msg,
                  msg)

        # ---------- ③ 输家族码入族 ----------
        st, body = await api.call("POST", f"/v1/invite/{VIEW_CODE}/accept")
        RPT.check(f"用族浏览码 {VIEW_CODE} 入族成功",
                  st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")

        row = db_first("SELECT family_id FROM app_user WHERE openid=:o",
                       o=PROBE_OPENID)
        RPT.check("入族后 app_user.family_id 真的落库了"
                  "（认领页读的就是这一列）",
                  row is not None and int(row[0]) == fid,
                  f"库里 family_id={row[0] if row else None}，期望 {fid}")

        # ---------- ④ 同一个人，入族之后立刻能答题 ----------
        st, body = await api.call("GET", "/v1/auth/identify/quiz")
        q = body.get("data") or {}
        RPT.check("★ 入族之后答题自证立刻可用 ——「输码入族」正是解开那句报错的钥匙",
                  st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")
        RPT.check("出的是刚入这个族的题（familyId 对得上）",
                  q.get("familyId") == fid,
                  f"题面 familyId={q.get('familyId')}，期望 {fid}")

        # ---------- ⑤ 入族 ≠ 能改 ----------
        scope_n = db_count(
            "SELECT COUNT(*) FROM edit_scope WHERE status=1 AND user_id="
            "(SELECT id FROM app_user WHERE openid=:o)",
            o=PROBE_OPENID,
        )
        RPT.check("★ 浏览码入族后仍然没有任何编辑授权（入族 ≠ 能改别人的）",
                  scope_n == 0,
                  f"浏览码不该授出 edit_scope，实际 {scope_n} 条")
    finally:
        # 解绑 + 清干净，让这一段可以反复跑
        db_drop_fixture(openids=(PROBE_OPENID,))
        RPT.note("一次性账号 probe_entry 已解绑（family_id / claimed_person_id 归零）")


def db_make_temp_family(view_code: str) -> int:
    """造一个只有名字、没有人的空族，专门给多族回归用。

    加入它必须走 family.view_code 这条分支（和主族同一个入口），
    不能直接往 user_family 插行 —— 那样测的就不是"入族"了。
    """
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM family WHERE view_code=:c"), {"c": view_code})
        db.commit()
        f = Family(
            surname="王",
            name="多族回归临时族",
            view_code=view_code,
            view_code_enabled=1,
            first_gen_label=1,
            gen_poem_enabled=0,
            show_external_in_overview=0,
            data_rev=0,
            status=1,
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        return int(f.id)
    finally:
        db.close()


def db_drop_temp_family(family_id: int) -> None:
    """连人一起拆干净。临时族里没有 person，所以不用走 db_drop_fixture。"""
    db = SessionLocal()
    try:
        for table in ("user_family", "invite_code", "edit_scope",
                      "tree_layout_cache", "branch", "revision"):
            db.execute(
                text(f"DELETE FROM {table} WHERE family_id=:i"), {"i": family_id}
            )
        db.execute(text("DELETE FROM family WHERE id=:i"), {"i": family_id})
        db.commit()
    finally:
        db.close()


async def part_multi_family(client: httpx.AsyncClient, fid: int,
                            ids: dict[str, int]) -> None:
    """多族：一个账号同属父系谱和母系谱。

    为什么需要这一段：
        app_user.family_id 原来只有一列，接受第二个族的码时被
        `if user.family_id is None` 挡回去 —— 从母系谱进来之后父系谱就再也回不去了，
        用户看到的是"我明明入过族，怎么又让我输码"。

        现在 user_family 记"我加入过哪些族"，app_user 那三列降级成"当前族快照"。
        全站鉴权读的都是那三列，所以切族必须真的回填 —— 只换端上的显示、
        不换库里的快照，结果就是"页面在 A 族、权限按 B 族算"。

    这段钉四件事：
      1. 已在一个族的人，还能加入第二个族
      2. 切族真的回填 app_user 那三列
      3. current 标记唯一且与 currentFamilyId 一致
      4. 没加入过的族切不过去（切族不能替代入族）

    自给自足：临时族和 probe_entry 都是本段自己造、自己拆。
    """
    RPT.section("【8.5】多族：父系谱 + 母系谱，同一账号两个族")

    TMP_CODE = "MFTST001"
    tmp_fid = db_make_temp_family(TMP_CODE)

    db_drop_fixture(openids=(PROBE_OPENID,))
    reset_rate_limit()

    try:
        api = Api(client, PROBE_OPENID)
        await api.login(PROBE_OPENID)

        # ---------- ① 先入主族 ----------
        st, body = await api.call("POST", f"/v1/invite/{VIEW_CODE}/accept")
        RPT.check("先加入主族", st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")

        # ---------- ② 再入第二个族：这一步原来会被静默跳过 ----------
        st, body = await api.call("POST", f"/v1/invite/{TMP_CODE}/accept")
        RPT.check("★ 已在一个族的人，仍然能加入第二个族"
                  "（原实现 if user.family_id is None 会把它静默挡掉）",
                  st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")

        # 只回 familyId 是不够的：切族会连 role / claimedPersonId 一起换掉。
        # 端上拿不到这两个值，就会以为"我在旧族认领过的人，在这个族也算数"，
        # 于是跳过认领，直到第一次写操作被 40301 挡回来才暴露。
        d2 = body.get("data") or {}
        RPT.check("★ accept 连角色和认领状态一起回传"
                  "（切族换的是整份快照，不只是 familyId）",
                  "claimedPersonId" in d2 and "role" in d2 and "needClaim" in d2,
                  f"实际字段 {sorted(d2.keys())}")
        RPT.check("新族的认领状态独立：没在这个族认领过 → needClaim=true",
                  d2.get("needClaim") is True and d2.get("claimedPersonId") is None,
                  f"claimedPersonId={d2.get('claimedPersonId')} needClaim={d2.get('needClaim')}")

        n = db_count(
            "SELECT COUNT(*) FROM user_family WHERE user_id="
            "(SELECT id FROM app_user WHERE openid=:o)",
            o=PROBE_OPENID,
        )
        RPT.check("user_family 里落了两行成员关系", n == 2, f"实际 {n} 行")
        # ---------- ③ 列表 ----------
        _, body = await api.call("GET", "/v1/family/list")
        d = body.get("data") or {}
        fams = d.get("families") or []
        RPT.check("★ /family/list 两个族都在",
                  {f.get("familyId") for f in fams} == {fid, tmp_fid},
                  f"实际 {[f.get('familyId') for f in fams]}，期望 {[fid, tmp_fid]}")
        RPT.check("刚输入码加入的族成为当前族（点码进来就是要看这个族）",
                  d.get("currentFamilyId") == tmp_fid,
                  f"currentFamilyId={d.get('currentFamilyId')}，期望 {tmp_fid}")
        RPT.check("列表里恰有一个 current=true",
                  sum(1 for f in fams if f.get("current")) == 1,
                  json.dumps(fams, ensure_ascii=False)[:300])

        # ---------- ④ 切回主族 ----------
        st, body = await api.call("POST", "/v1/family/switch", json={"familyId": fid})
        RPT.check("切回主族成功", st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")

        row = db_first("SELECT family_id FROM app_user WHERE openid=:o", o=PROBE_OPENID)
        RPT.check("★ 切族真的回填了 app_user.family_id"
                  "（鉴权读的就是这一列，不回填 = 页面换了权限没换）",
                  row is not None and int(row[0]) == fid,
                  f"库里 family_id={row[0] if row else None}，期望 {fid}")

        _, me_body = await api.call("GET", "/v1/auth/me")
        # /auth/me 把账号快照放在 data.user 里（data.familyId 不存在）。
        # 这条验的是「端上拿到的登录态」也跟着换了，和上一条直接查库互补：
        # 只改库不改返回，用户看到的名字还是旧族。
        me_user = (me_body.get("data") or {}).get("user") or {}
        RPT.check("登录态快照跟着变（/auth/me 返回主族）",
                  me_user.get("familyId") == fid,
                  f"familyId={me_user.get('familyId')}，familyName={me_user.get('familyName')}")

        # ---------- ⑤ 来回切不报错（快照可从 user_family 反复回填）----------
        await api.call("POST", "/v1/family/switch", json={"familyId": tmp_fid})
        st, body = await api.call("POST", "/v1/family/switch", json={"familyId": fid})
        RPT.check("来回切族各一次都不报错", body.get("code") == 0,
                  json.dumps(body, ensure_ascii=False)[:200])

        # ---------- ⑥ 切族不能替代入族 ----------
        st, body = await api.call("POST", "/v1/family/switch", json={"familyId": 999999})
        RPT.check("★ 没加入过的族切不过去 —— 切族不能替代入族",
                  body.get("code") in (40300, 40401),
                  f"code={body.get('code')} msg={body.get('msg')}")
    finally:
        db_drop_temp_family(tmp_fid)
        db_drop_fixture(openids=(PROBE_OPENID,))
        RPT.note(f"临时族 {tmp_fid} 与一次性账号已清理")


async def part_big_tree(client: httpx.AsyncClient) -> None:
    """【8.6】老谱规模下的呈现：默认视图收窄 + 同代折叠。

    真实场景不是"几十人慢慢填"：宗祠理事会把老谱拍成照片、OCR 转录进来，
    开局就是十几代、200 活在世。实测 15 代 / 564 人时，整幅全览字号 0.47px
    （画布 9528×2136px、2255 个 view），所以：

      1. 「我的一家」必须是以锚点为中心的小图（上 2 下 1）
      2. 同代超过 3 个男丁就折叠，否则光兄弟就能横铺十来个夫妻单元
      3. 折叠只是"这一屏不画"，传 siblings=99 能拿回全部
      4. 两种取法不能共用缓存（缓存键必须带 sibling 上限）

    自给自足：临时族 + 所有人 + 关系全部本段自己造自己拆。
    """
    RPT.section("【8.6】老谱规模：我的一家收窄 + 同代折叠")

    TMP_CODE = "BIGT01"
    tmp_fid = db_make_temp_family(TMP_CODE)
    created: list[int] = []

    try:
        founder = db_add_person(tmp_fid, name="王始祖", surname="王", gender=1,
                                generation=1, role=LineageRole.MALE,
                                living=LivingStatus.DECEASED)
        founder_w = db_add_person(tmp_fid, name=None, surname="李", gender=2,
                                  generation=1, role=LineageRole.SPOUSE_IN,
                                  living=LivingStatus.DECEASED)
        created += [founder, founder_w]
        root_union = db_add_union(tmp_fid, founder, founder_w)

        sons: list[int] = []
        for i in range(8):
            s = db_add_person(tmp_fid, name=f"王二{i}", surname="王", gender=1,
                              generation=2, role=LineageRole.MALE,
                              living=LivingStatus.LIVING)
            db_add_child(tmp_fid, root_union, s)
            sons.append(s)
            created.append(s)
            w = db_add_person(tmp_fid, name=None, surname="张", gender=2,
                              generation=2, role=LineageRole.SPOUSE_IN,
                              living=LivingStatus.LIVING)
            db_add_union(tmp_fid, s, w)
            created.append(w)

        anchor = sons[4]  # 中间那个，左右都有人，最能暴露"取前 N 个"的错误

        async def pull(**params):
            r = await client.get(f"/v1/tree/{tmp_fid}",
                                 params={"view": "mine", "anchor": anchor, **params})
            return r.status_code, r.json()

        st, body = await pull()
        d = body.get("data") or {}
        ids_now = {n["id"] for n in (d.get("nodes") or [])}

        RPT.check("默认（不传 siblings）能拉到树",
                  st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")
        RPT.check("★ 锚点本人永远在图上（折叠不能把自己折掉）",
                  anchor in ids_now, f"锚点 {anchor} in {sorted(ids_now)}")
        RPT.check("★ 同代超过 3 个男丁时折叠 —— 返回 siblingsHidden",
                  (d.get("collapsed") or {}).get("siblingsHidden") == 5,
                  f"collapsed={d.get('collapsed')}（8 个兄弟，显示 3 个 → 应藏 5 个）")

        # 只画 3 个男丁：8 个儿子里选 3 个（含锚点），加 3 位媳妇 + 父母
        RPT.check("★ 折叠后同代只画 3 个男丁（含自己）",
                  len([i for i in ids_now if i in sons]) == 3,
                  f"实际画了 {len([i for i in ids_now if i in sons])} 个兄弟")
        RPT.check("折叠掉的人连同配偶一起不画（不出现'媳妇挂在虚空里'）",
                  len([i for i in ids_now if i not in sons and i not in (founder, founder_w)]) == 3,
                  f"实际媳妇 {len([i for i in ids_now if i not in sons and i not in (founder, founder_w)])} 位")

        # ---------- 展开全部 ----------
        st, body2 = await pull(siblings=99)
        d2 = body2.get("data") or {}
        ids_all = {n["id"] for n in (d2.get("nodes") or [])}

        RPT.check("★ siblings=99 能把被折叠的兄弟拿回来",
                  len([i for i in ids_all if i in sons]) == 8,
                  f"实际 {len([i for i in ids_all if i in sons])} 个")
        RPT.check("展开后不再报 collapsed",
                  not d2.get("collapsed"),
                  f"collapsed={d2.get('collapsed')}")
        RPT.check("展开后的图确实比折叠版大",
                  len(ids_all) > len(ids_now),
                  f"折叠 {len(ids_now)} 节点 / 展开 {len(ids_all)} 节点")

        # ---------- 缓存隔离 ----------
        # 两次请求只差一个查询参数。缓存键要是没带 sibling 上限，
        # 第二次会命中第一次的布局，用户点"展开"却什么都没变。
        _, body3 = await pull()
        d3 = body3.get("data") or {}
        RPT.check("★ 折叠版与展开版不共用缓存（缓存键含 sibling 上限）",
                  (d3.get("collapsed") or {}).get("siblingsHidden") == 5,
                  f"再取默认版 collapsed={d3.get('collapsed')}，"
                  f"cached={d3.get('cached')}")

        # ---------- 全族总览用 lite：不返回姓名 ----------
        r = await client.get(f"/v1/tree/{tmp_fid}",
                             params={"view": "overview", "detail": "lite"})
        ov = (r.json().get("data") or {}).get("nodes") or []
        RPT.check("★ 全族总览（detail=lite）不下发姓名 —— Canvas 骨架不需要，"
                  "500+ 节点每次做脱敏投影是白花",
                  bool(ov) and all("displayName" not in n for n in ov),
                  f"节点 {len(ov)}，首个字段 {sorted(ov[0].keys()) if ov else []}")

        # ---------- 世代人数：只数丁口 ----------
        # 夹具：1 世 = 始祖(男) + 1 位媳妇；2 世 = 8 个儿子 + 8 位媳妇
        # 丁口口径 = role 1(男丁) + 2(女儿)，媳妇不算。
        r = await client.get(f"/v1/tree/{tmp_fid}",
                             params={"view": "overview", "detail": "lite"})
        d_ov = r.json().get("data") or {}
        stats = d_ov.get("generationStats") or []
        by_gen = {s["gen"]: s["count"] for s in stats}

        RPT.check("★ 全族总览下发每一代的丁口人数（含代数）",
                  by_gen.get(1) == 1 and by_gen.get(2) == 8,
                  f"实际 {stats}")
        RPT.check("★ 媳妇不计入丁口（2 世 8 男丁 + 8 媳妇，应报 8 而不是 16）",
                  by_gen.get(2) == 8, f"第 2 世 count={by_gen.get(2)}")

        # 男女分开报：理事会问的第一句就是"这一代几个男丁"，
        # 只给总数答不了。夹具里没有本族女儿，所以 female 全为 0。
        g2 = next((x for x in stats if x["gen"] == 2), {})
        g1 = next((x for x in stats if x["gen"] == 1), {})
        RPT.check("★ 每代带男/女拆分（format: 第4世 2人（男1 女1））",
                  g2.get("male") == 8 and g2.get("female") == 0
                  and g1.get("male") == 1 and g1.get("female") == 0,
                  f"1 世 {g1} / 2 世 {g2}")
        RPT.check("count = male + female（每一代都对得上）",
                  all(x["count"] == x["male"] + x["female"] for x in stats),
                  f"{stats}")
        RPT.check("汇总口径一致：lineageTotal = 各代之和，generationCount = 有人的代数",
                  d_ov.get("lineageTotal") == 9 and d_ov.get("generationCount") == 2
                  and d_ov.get("maleTotal") == 9 and d_ov.get("femaleTotal") == 0,
                  f"lineageTotal={d_ov.get('lineageTotal')} "
                  f"male={d_ov.get('maleTotal')} female={d_ov.get('femaleTotal')} "
                  f"generationCount={d_ov.get('generationCount')}")
        RPT.check("我的一家 / 支系视图不带世代统计（那是总览专用的信息）",
                  not (d.get("generationStats")),
                  f"mine.generationStats={d.get('generationStats')}")
    finally:
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM parent_child WHERE family_id=:i"), {"i": tmp_fid})
            db.execute(text("DELETE FROM family_union WHERE family_id=:i"), {"i": tmp_fid})
            db.execute(text("DELETE FROM person WHERE family_id=:i"), {"i": tmp_fid})
            db.commit()
        finally:
            db.close()
        db_drop_temp_family(tmp_fid)
        RPT.note(f"临时族 {tmp_fid}（{len(created)} 人）已清理")


async def part_polygamy(client: httpx.AsyncClient) -> None:
    """【8.8】多配偶：一夫多妻与离异再婚。

    2026-10-21 启明拍板的口径：
      · **配偶不因离异而隐藏**——离异只是婚姻边画虚线 + 标「已离异」。
        把母亲从孩子那一屏拿掉，等于让这件事在子女视角里变成"没有母亲"。
      · 子女按**生母分列**：谁所出一眼可辨，亲子线也不横穿别人的卡片。
      · 全族总览是静态骨架，**只画直系丁口**（媳妇不进）。
      · 焦点视图上 1 下 1，靠"点子女 → 焦点下移"逐代下钻。
    """
    RPT.section("【8.8】多配偶：一夫多妻 / 离异再婚")

    tmp_fid = db_make_temp_family("POLY01")
    try:
        man = db_add_person(tmp_fid, name="王承祖", surname="王", gender=1,
                            generation=2, role=LineageRole.MALE, living=LivingStatus.LIVING)
        w1 = db_add_person(tmp_fid, name="李元配", surname="李", gender=2,
                           generation=2, role=LineageRole.SPOUSE_IN, living=LivingStatus.DECEASED)
        w2 = db_add_person(tmp_fid, name="张继配", surname="张", gender=2,
                           generation=2, role=LineageRole.SPOUSE_IN, living=LivingStatus.LIVING)
        w3 = db_add_person(tmp_fid, name="王侧室", surname="王", gender=2,
                           generation=2, role=LineageRole.SPOUSE_IN, living=LivingStatus.LIVING)

        u1 = db_add_union(tmp_fid, man, w1, seq=1)
        u2 = db_add_union(tmp_fid, man, w2, seq=2,
                          end_reason=int(UnionEndReason.DIVORCED))
        u3 = db_add_union(tmp_fid, man, w3, seq=3)

        c1 = db_add_person(tmp_fid, name="王长子", surname="王", gender=1,
                           generation=3, role=LineageRole.MALE, living=LivingStatus.LIVING)
        c2 = db_add_person(tmp_fid, name="王次子", surname="王", gender=1,
                           generation=3, role=LineageRole.MALE, living=LivingStatus.LIVING)
        c3 = db_add_person(tmp_fid, name="王三子", surname="王", gender=1,
                           generation=3, role=LineageRole.MALE, living=LivingStatus.LIVING)
        c4 = db_add_person(tmp_fid, name="王四子", surname="王", gender=1,
                           generation=3, role=LineageRole.MALE, living=LivingStatus.LIVING)
        for uid, cid in ((u1, c1), (u2, c2), (u2, c3), (u3, c4)):
            db_add_child(tmp_fid, uid, cid)

        # 祖父：用来验证"主线全量"——焦点在 c1 时祖父、祖母也应在图上
        gp = db_add_person(tmp_fid, name="王高祖", surname="王", gender=1,
                           generation=1, role=LineageRole.MALE, living=LivingStatus.DECEASED)
        gpw = db_add_person(tmp_fid, name=None, surname="赵", gender=2,
                            generation=1, role=LineageRole.SPOUSE_IN, living=LivingStatus.DECEASED)
        ug = db_add_union(tmp_fid, gp, gpw, seq=1)
        db_add_child(tmp_fid, ug, man)

        # ---------- 焦点视图：以 man 为中心 ----------
        r = await client.get(f"/v1/tree/{tmp_fid}",
                             params={"view": "mine", "anchor": man})
        body = r.json()
        d = body.get("data") or {}
        nodes = {n["id"]: n for n in (d.get("nodes") or [])}
        edges = d.get("edges") or []
        ok = RPT.check("多配偶的焦点视图可返回",
                       r.status_code == 200 and body.get("code") == 0,
                       json.dumps(body, ensure_ascii=False)[:300])
        if ok:
            RPT.check("★ 三位夫人全部在图上（离异的那位也不例外）",
                      all(x in nodes for x in (w1, w2, w3)),
                      f"图上 {sorted(nodes)}，夫人 {[w1, w2, w3]}")

            marry = {(e["from"], e["to"]): e for e in edges if e.get("type") == 2}
            e2 = marry.get((man, w2)) or marry.get((w2, man))
            RPT.check("★ 离异那段婚姻边标成已结束（端上画虚线）",
                      bool(e2) and e2.get("ended") is True
                      and e2.get("endReason") == int(UnionEndReason.DIVORCED),
                      json.dumps(e2, ensure_ascii=False))

            seqs = {nodes[x].get("unionSeq") for x in (w1, w2, w3) if x in nodes}
            RPT.check("★ 婚序号 1/2/3 落库并下发（元配/继配/侧室靠它排）",
                      seqs == {1, 2, 3}, f"unionSeq 集合 {seqs}")

            # 端上给离异配偶的**卡片**画虚框，读的是节点自己的 endReason，
            # 不是边上那个。少下发这个字段，卡片看起来就还是个正常配偶。
            RPT.check("★ 配偶节点自带 endReason（离异卡片画虚框靠它）",
                      nodes[w2].get("endReason") == int(UnionEndReason.DIVORCED)
                      and nodes[w2].get("unionEnded") is True,
                      json.dumps(nodes[w2], ensure_ascii=False))

            kid_edge = next((e for e in edges
                             if e.get("to") == c2 and e.get("type") == 1), None)
            RPT.check("★ 子女的亲子边带 motherId（端上据此从生母下垂）",
                      bool(kid_edge) and kid_edge.get("motherId") == w2,
                      json.dumps(kid_edge, ensure_ascii=False))

            # 夫妻卡片必须零间距紧贴（2026-10-22）：
            # 以前配偶卡被放在"她那一列"的中心，而列被她的孩子撑宽，
            # 于是妻子被推开 220px——用户看到的是"夫妻离得很远"。
            # 卡片行是"一张接一张"排的，所以要跟**前一位**比右边界，
            # 不是每张都跟本人比（那样第二位的间距会算成 88，误报）。
            chain = [man] + sorted(
                (x for x in (w1, w2, w3) if x in nodes), key=lambda x: nodes[x]["x"]
            )
            gaps = [
                nodes[b]["x"] - (nodes[a]["x"] + nodes[a]["w"])
                for a, b in zip(chain, chain[1:])
            ]
            RPT.check("★ 夫妻卡片零间距紧贴（三位夫人依次贴在本人右侧）",
                      len(gaps) == 3 and all(g == 0 for g in gaps),
                      f"间距 {gaps}，x = {[nodes[x]['x'] for x in chain]}")

            # 同母子女成组、跨母不混排：按 x 排序后，母亲序号必须单调不减。
            # 不断言"孩子正对某个母亲"——卡片贴住之后列中心不再等于卡片中心，
            # 那条断言会把"夫妻紧贴"这个正确行为判成错。
            order = sorted([c1, c2, c3, c4], key=lambda c: nodes[c]["x"])
            mom = {c1: 1, c2: 2, c3: 2, c4: 3}
            seq_of_kids = [mom[c] for c in order]
            RPT.check("★ 同母子女成组、异母不混排（按 x 排序后母亲序号单调）",
                      seq_of_kids == sorted(seq_of_kids),
                      f"排列 {order} → 母亲序号 {seq_of_kids}")

        # ---------- 主线全量（2026-10-28） ----------
        # mine 改垂直卡片流后，"一屏塞不下"的约束消失：向上沿父链一路到始祖，
        # 用户预期"从我往上的直系先祖全部卡片式展示，可以上下滑动"。
        r = await client.get(f"/v1/tree/{tmp_fid}",
                             params={"view": "mine", "anchor": c1})
        d1 = (r.json().get("data") or {})
        ids1 = {n["id"] for n in (d1.get("nodes") or [])}
        RPT.check("★ mine 主线全量：锚点往上的直系先祖全部在图上（父、祖父都在）",
                  man in ids1 and gp in ids1 and gpw in ids1,
                  f"父亲在={man in ids1} 祖父在={gp in ids1} 祖母在={gpw in ids1}")

        # truncated 必须直接告诉端上"往下跳谁"：anchor 的孩子全在屏上时，
        # 旧版只给 up/down 布尔，端上取"流里最底下的卡"==锚点本人，点了没反应。
        tr1 = d1.get("truncated") or {}
        RPT.check("★ truncated 带 upAnchor/downAnchor（端上据此跳下一个中心）",
                  ("upAnchor" in tr1 or tr1.get("up") is not True)
                  and ("downAnchor" in tr1 or tr1.get("down") is not True),
                  f"truncated={json.dumps(tr1, ensure_ascii=False)}")

        # ---------- 全族总览：只画直系丁口 ----------
        r = await client.get(f"/v1/tree/{tmp_fid}", params={"view": "overview"})
        ov = (r.json().get("data") or {})
        ids_ov = {n["id"] for n in (ov.get("nodes") or [])}
        RPT.check("★ 总览是静态骨架：媳妇不进图（宽度不再随夫人数膨胀）",
                  not ({w1, w2, w3, gpw} & ids_ov),
                  f"总览里出现配偶：{sorted({w1, w2, w3, gpw} & ids_ov)}")
        RPT.check("总览仍然包含直系丁口（男丁与女儿）",
                  {man, c1, c2, c3, c4, gp} <= ids_ov,
                  f"缺：{sorted({man, c1, c2, c3, c4, gp} - ids_ov)}")

        # ---------- 写路径：多段婚姻时不再把孩子判给元配 ----------
        # 旧实现按 union_seq 取第一条（全为 1 时取 id 最小 = 元配），
        # 二房的孩子一进来生母就错位，认领自证"我母亲是张氏"永远反查不到。
        from app.services.revision_service import _ensure_union_for

        db = SessionLocal()
        try:
            u_new = _ensure_union_for(db, man, tmp_fid)
            RPT.check("★ 多段婚姻时新建亲子单元，不再默认挂到元配名下",
                      u_new.id not in (u1, u2, u3) and u_new.partner_b_id is None,
                      f"unionId={u_new.id} partner_b={u_new.partner_b_id}")
            db.rollback()

            u_w = _ensure_union_for(db, w2, tmp_fid)
            RPT.check("★ 以媳妇为父/母加子女时用她所嫁的那段（不会把她当 partner_a）",
                      u_w.id == u2, f"返回 {u_w.id}，期望 {u2}")
            db.rollback()
        finally:
            db.close()
    finally:
        db_drop_temp_family(tmp_fid)
        RPT.note(f"临时族 {tmp_fid} 已清理")


async def part_relation_link(admin: Api) -> None:
    """【8.7】父/母挂载：补父亲不能只建人、不挂关系。

    背景：`_infer_relation` 会为"父名待补"产出 {"type": "father_of"}，
    但 `_link_relation` 原来只有 child_of / spouse_of 两个分支 —— 于是
    **人建出来了、关系一条没挂**，界面上父子断开，还不报错。
    老谱里"配某氏""父名不详"的格子很多，这类静默孤儿只有对账才发现。
    """
    RPT.section("【8.7】补父/母：新建的人必须真的挂上亲子关系")

    from app.services.revision_service import _link_relation

    tmp_fid = db_make_temp_family("LINK01")
    db = SessionLocal()
    try:
        child = Person(family_id=tmp_fid, name="孤儿", surname="王", gender=1,
                       generation=2, lineage_role=int(LineageRole.MALE),
                       living_status=int(LivingStatus.LIVING), privacy_level=1,
                       status=1, source_type=2, confidence=2, verify_status=1)
        father = Person(family_id=tmp_fid, name="王父", surname="王", gender=1,
                        generation=1, lineage_role=int(LineageRole.MALE),
                        living_status=int(LivingStatus.DECEASED), privacy_level=1,
                        status=1, source_type=2, confidence=2, verify_status=1)
        stranger = Person(family_id=tmp_fid, name="另一位父", surname="王", gender=1,
                          generation=1, lineage_role=int(LineageRole.MALE),
                          living_status=int(LivingStatus.DECEASED), privacy_level=1,
                          status=1, source_type=2, confidence=2, verify_status=1)
        db.add_all([child, father, stranger])
        db.flush()

        res = _link_relation(db, father, {"type": "father_of", "childId": child.id}, tmp_fid)
        db.flush()

        link = db.execute(
            text("SELECT union_id FROM parent_child WHERE child_id=:c"), {"c": child.id}
        ).first()
        RPT.check("★ 补父亲：真的建了 parent_child（原来这里是空的）",
                  link is not None, f"parent_child={link}")

        union = db.get(FamilyUnion, int(link[0])) if link else None
        RPT.check("★ 父亲落在婚姻单元的 partner_a 位（男）",
                  union is not None and int(union.partner_a_id) == father.id,
                  f"partner_a={getattr(union, 'partner_a_id', None)}，期望 {father.id}")
        RPT.check("返回值说明挂上去了", (res or {}).get("type") == "father_of",
                  f"返回值 {res}")

        res2 = _link_relation(db, stranger, {"type": "father_of", "childId": child.id}, tmp_fid)
        db.flush()
        union2 = db.get(FamilyUnion, union.id) if union else None
        RPT.check("★ 位置上已经有人时不覆盖，且明说 linked=False"
                  "（静默改爹比不改更糟）",
                  (res2 or {}).get("linked") is False
                  and int(union2.partner_a_id) == father.id,
                  f"返回值 {res2}，partner_a={union2.partner_a_id if union2 else None}")
        db.rollback()
    finally:
        db.close()
        db_drop_temp_family(tmp_fid)
        RPT.note(f"临时族 {tmp_fid} 已清理")

    # ---------- 接口层：新增人物的锚点必须认 childId ----------
    #
    # 服务层挂得上，不等于接口层放得进来：`create_person` 原来只从
    # parentId / partnerId 里找锚点，于是"给某人补父亲"会被判成
    # "没指定挂载位置"——权限明明够，却在门口被挡。
    #
    # 这里刻意传空 fields：如果锚点这道门认了 childId，下一步会抱怨缺姓名；
    # 门没认就会抱怨缺挂载位置。断言精确到错误文案，而且不往演示族里塞垃圾数据。
    st, body = await admin.call(
        "POST", "/v1/person",
        json={"fields": {}, "relation": {"type": "father_of", "childId": 1}},
    )
    msg = (body or {}).get("msg") or ""
    RPT.check("★ 接口层：带 childId 时锚点校验放行（该说缺姓名，而不是缺挂载位置）",
              "姓名" in msg,
              f"HTTP {st} msg={msg}")

    st2, body2 = await admin.call(
        "POST", "/v1/person",
        json={"fields": {"name": "无锚点"}, "relation": {}},
    )
    msg2 = (body2 or {}).get("msg") or ""
    RPT.check("什么锚点都不给时仍然拒绝（否则等于随便往族里塞人）",
              "挂载位置" in msg2,
              f"HTTP {st2} msg={msg2}")

    # ---------- 新增人物不填存殁 = 在世 ----------
    #
    # 实报：给陈国栋添「陈老三」，只填了名字，卡片上却打了一个「故」。
    # 根因是 person.living_status 的模型默认值是 2（已故）——「没填」被
    # 静默解释成「已故」。这比漏填糟得多：人还活着，谱上先给他画了终点，
    # 而且是当着他家人的面。
    tmp_name = "冒烟临时子"
    st3, body3 = await admin.call(
        "POST", "/v1/person",
        json={"fields": {"name": tmp_name},
              "relation": {"type": "child_of", "parentId": 1}},
    )
    db = SessionLocal()
    new_person = db.execute(
        select(Person).where(Person.name == tmp_name)
    ).scalars().first()
    RPT.check("新增人物（只填名字）落库成功", new_person is not None,
              f"HTTP {st3} msg={(body3 or {}).get('msg')}")
    if new_person is not None:
        RPT.check("★ 没填存殁的人默认「在世」，不能默认已故",
                  int(new_person.living_status) == int(LivingStatus.LIVING),
                  f"living_status={new_person.living_status}")
        # 清理：这是演示族，不能留一个"冒烟临时子"（人 + 亲子行 + 空 union + 修订记录）
        pid = int(new_person.id)
        union_ids = [
            r[0] for r in db.execute(
                text("SELECT id FROM family_union WHERE partner_a_id=:i "
                     "OR partner_b_id=:i"), {"i": pid}
            ).fetchall()
        ]
        for uid in union_ids:
            db.execute(text("DELETE FROM parent_child WHERE union_id=:u"), {"u": uid})
        db.execute(text("DELETE FROM parent_child WHERE child_id=:i"), {"i": pid})
        for uid in union_ids:
            db.execute(text("DELETE FROM family_union WHERE id=:u"), {"u": uid})
        db.execute(text("DELETE FROM revision WHERE target_table='person' "
                        "AND target_id=:i"), {"i": pid})
        db.execute(text("DELETE FROM person WHERE id=:i"), {"i": pid})
        db.commit()
        RPT.note(f"临时人物 {pid} 已清理")
    db.close()


async def part_identify(client: httpx.AsyncClient, fid: int, ids: dict[str, int],
                        admin: Api) -> None:
    """答题自证 —— 现在身份确认的主通道，房长审批退成兜底。

    为什么要有这一段：这条链路把"认领"从**人工审批**换成了**自己答出来**，
    一次性引入了四件容易悄悄坏掉的事：
      1. 定位算法（同名歧义、谱里没我、已故祖先）
      2. 证据强度（在世=强、已故=弱）以及弱证据的追加门槛
      3. 范围内免审写入 / 范围外排队
      4. 房长撤销认领之后，授权真的收回了

    这四件事任何一件坏了，用户侧的表现都是"进不去"，而服务器侧一切正常。
    所以这里不测"接口能调通"，测的是**这四件事成立**。

    数据自给自足：本段需要个"母亲还没名字"的场景，但种子里那个「配某氏」
    已经被【4】用掉了。所以自己造一组夹具，用完拆掉，不去蹭别的段落的状态。
    """
    RPT.section("【9】答题自证：答出父母的名字就能进，不用等房长批")

    reset_rate_limit()
    created: list[int] = []
    openids = ("mock_a", "mock_b", "mock_c", "mock_d", "mock_e", "mock_f", "mock_g")

    async def newcomer(openid: str) -> Api:
        """新族人从群里点进来：登录 → 领族浏览码入族 → 拿 token。

        这里走的是冷启动真实路径。故意不用管理员身份，
        因为"答题的人就是普通新用户"才是这条链路的常态。

        注意两步缺一不可：登录只认人，**入族**才把 user.family_id 填上。
        少了 accept，后面的答题接口会回"请先通过族浏览码或邀请码进入某个宗族"——
        一个和身份验证毫无关系的报错，很容易被误判成答题机制坏了。
        """
        api = Api(client, openid)
        # 注意：Api.login 内部会拼 "dev:" 前缀，这里传裸 openid。
        # 再拼一次会得到 openid="dev:mock_a" 的另一个账号——登录照样成功、
        # 答题照样通过，只是所有按 openid 查库的断言全部落空，极难查。
        await api.login(openid)
        await api.call("POST", f"/v1/invite/{VIEW_CODE}/accept")
        reset_rate_limit()   # 限速是进程内的，测试之间要清，否则互相干扰
        return api

    async def answer(api: Api, **kw) -> dict:
        _, body = await api.call("POST", "/v1/auth/identify/verify", json=kw)
        return body.get("data") or {}

    async def answer_raw(api: Api, **kw) -> dict:
        _, body = await api.call("POST", "/v1/auth/identify/verify", json=kw)
        return body

    try:
        # ---------- 出题 ----------
        a = await newcomer("mock_a")
        st, body = await a.call("GET", "/v1/auth/identify/quiz")
        q = body.get("data") or {}
        RPT.check("出题接口可用（小程序认领页的第一屏）",
                  st == 200 and body.get("code") == 0,
                  f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}")
        RPT.check("题面带出族内人数（用户据此判断「是不是我们这个族」）",
                  q.get("personCount") == count_rows(Person, fid),
                  f"题面 {q.get('personCount')} vs 库里 {count_rows(Person, fid)}")
        RPT.check("刚好 4 个问题（关系 / 亲属名 / 本人名 / 另一位家长）",
                  len(q.get("questions") or []) == 4,
                  json.dumps(q.get("questions"), ensure_ascii=False)[:300])
        RPT.check("尚未认领时 alreadyClaimed 为空",
                  q.get("alreadyClaimed") is None)
        opts = next((x.get("options") or [] for x in (q.get("questions") or [])
                     if x.get("key") == "relation"), [])
        RPT.check("★ 有「我配偶」选项 —— 嫁进来的媳妇、入赘的女婿只能走这条",
                  any(o.get("value") == "spouse" for o in opts),
                  json.dumps(opts, ensure_ascii=False))

        # ---------- 命中已有节点 ----------
        r = await answer(a, relation="father",
                         relativeName="陈国栋", selfName="陈启华")
        RPT.check("父=陈国栋 我=陈启华 → 命中已有节点",
                  r.get("result") == "matched",
                  json.dumps(r, ensure_ascii=False)[:300])
        RPT.check("命中的是陈启华本人的节点",
                  r.get("personId") == ids["qihua"],
                  f"personId={r.get('personId')} 期望={ids['qihua']}")
        RPT.check("★ 证据强度 strong（父亲在世，姓名在树上是脱敏的，外人抄不到）",
                  r.get("evidenceStrength") == "strong",
                  f"strength={r.get('evidenceStrength')}")
        RPT.check("按「自己 + 直系上下两代」授出 9 个可编辑节点",
                  r.get("grantedNodeCount") == 9,
                  f"grantedNodeCount={r.get('grantedNodeCount')}")

        RPT.check("用户绑定到该节点（claimed_person_id）",
                  scalar("SELECT claimed_person_id FROM app_user WHERE openid='mock_a'")
                  == ids["qihua"])
        RPT.check("edit_scope 落库 9 行",
                  db_count("SELECT COUNT(*) FROM edit_scope WHERE status=1 AND user_id="
                           "(SELECT id FROM app_user WHERE openid='mock_a')") == 9)

        cl = scalar("SELECT id FROM claim WHERE user_id="
                    "(SELECT id FROM app_user WHERE openid='mock_a') ORDER BY id DESC")
        RPT.check("认领留痕：verify_method=1（答题自证）",
                  scalar("SELECT verify_method FROM claim WHERE id=:i", i=cl)
                  == int(VerifyMethod.ANSWER))
        RPT.check("认领留痕：status=2（直接通过，没有人工环节）",
                  scalar("SELECT status FROM claim WHERE id=:i", i=cl)
                  == int(ClaimStatus.APPROVED))
        RPT.check("evidence 记下原话（房长抽查时能看懂当时答了什么）",
                  "父亲是陈国栋" in (scalar("SELECT evidence FROM claim WHERE id=:i", i=cl) or ""),
                  scalar("SELECT evidence FROM claim WHERE id=:i", i=cl))
        # JSON_EXTRACT 出来是带引号的 JSON 标量（'"strong"'），得先 UNQUOTE
        RPT.check("verify_answers 存了证据强度（这条是事后追溯的唯一依据）",
                  scalar("SELECT JSON_UNQUOTE(JSON_EXTRACT(verify_answers,"
                         "'$.evidenceStrength')) FROM claim WHERE id=:i", i=cl) == "strong")
        RPT.check("verify_answers 存了用户填的原名（含繁简差异，便于比对）",
                  scalar("SELECT JSON_UNQUOTE(JSON_EXTRACT(verify_answers,"
                         "'$.selfName')) FROM claim WHERE id=:i", i=cl) == "陈启华")
        RPT.check("写进审计日志",
                  db_count("SELECT COUNT(*) FROM audit_log "
                           "WHERE action='claim.auto_verified'") >= 1)

        # ---------- 范围免审 ----------
        RPT.note("决策：自己直系上下两代内改，直接生效；改到别处，排队等房长")
        _, body = await a.call("PATCH", f"/v1/person/{ids['qihua']}",
                               json={"fields": {"occupation": "教师"}})
        d = body.get("data") or {}
        RPT.check("范围内修改返回成功", body.get("code") == 0,
                  json.dumps(body, ensure_ascii=False)[:300])
        RPT.check("★ 范围内 autoMerged=true（不再进待审队列）",
                  d.get("autoMerged") is True,
                  json.dumps(d, ensure_ascii=False)[:300])
        RPT.check("修订状态标成 4（AUTO_MERGED，可回滚但不是待审）",
                  d.get("status") == 4, f"status={d.get('status')}")
        RPT.check("库里的值确实变了（不是「显示成功、实际没写」）",
                  scalar("SELECT occupation FROM person WHERE id=:i",
                         i=ids["qihua"]) == "教师",
                  scalar("SELECT occupation FROM person WHERE id=:i", i=ids["qihua"]))

        _, body = await a.call("PATCH", f"/v1/person/{ids['wenhai']}",
                               json={"fields": {"occupation": "篾匠"}})
        d = body.get("data") or {}
        RPT.check("★ 范围外修改不再被 403 挡掉，改为排队",
                  body.get("code") == 0,
                  json.dumps(body, ensure_ascii=False)[:300])
        RPT.check("范围外 autoMerged=false",
                  d.get("autoMerged") is False)
        RPT.check("范围外修订状态=1（待审）", d.get("status") == 1,
                  f"status={d.get('status')}")

        fresh = Api(client, "mock_unclaimed")
        await fresh.login("mock_unclaimed")
        await fresh.call("POST", f"/v1/invite/{VIEW_CODE}/accept")
        reset_rate_limit()
        _, body = await fresh.call("PATCH", f"/v1/person/{ids['qihua']}",
                                   json={"fields": {"occupation": "医生"}})
        RPT.check("未认领的人仍然改不动任何东西（40301）—— 免审不等于全开",
                  body.get("code") == 40301,
                  json.dumps(body, ensure_ascii=False)[:300])

        # ---------- 冲突 ----------
        b = await newcomer("mock_b")
        body = await answer_raw(b, relation="father",
                                relativeName="陈国栋", selfName="陈启明")
        RPT.check("★ 节点已被他人认领 → 40901 转人工，不做先到先得",
                  body.get("code") == 40901,
                  json.dumps(body, ensure_ascii=False)[:300])

        # ---------- 谱里没我：主路径 ----------
        RPT.note("接力修谱的主力：谱上有父亲，谱上还没你")
        r = await answer(b, relation="father",
                         relativeName="陈国梁", selfName="陈启山")
        RPT.check("★ 父=陈国梁 我=陈启山 → new_member（谱上没我，直接接上去）",
                  r.get("result") == "new_member",
                  json.dumps(r, ensure_ascii=False)[:300])
        new_id = int(r.get("personId") or 0)
        created.append(new_id)
        RPT.check("建出了新节点", new_id > 0, json.dumps(r, ensure_ascii=False)[:200])
        RPT.check("挂在所填亲属（陈国梁）下面",
                  r.get("relativeId") == ids["guoliang"],
                  f"relativeId={r.get('relativeId')}")
        RPT.check("世次 = 父亲 + 1",
                  scalar("SELECT generation FROM person WHERE id=:i", i=new_id) == 4,
                  scalar("SELECT generation FROM person WHERE id=:i", i=new_id))
        RPT.check("confidence=1（高）—— 证据强时不降级",
                  scalar("SELECT confidence FROM person WHERE id=:i", i=new_id) == 1)
        RPT.check("source_note 留痕（日后清洗脏数据要知道它是怎么来的）",
                  bool(scalar("SELECT source_note FROM person WHERE id=:i", i=new_id)))
        # 挂到"两口子都在"的婚姻单元上，而不是给父亲单开一个。
        # 挂错的话孩子与母亲的关系就断了，而树上看起来只是"少了一个人"。
        pair = db_first(
            "SELECT u.partner_a_id, u.partner_b_id FROM parent_child pc "
            "JOIN family_union u ON u.id=pc.union_id WHERE pc.child_id=:c", c=new_id)
        RPT.check("★ 挂在完整婚姻单元上（父陈国梁 + 母周小燕），不是单亲挂载",
                  pair is not None and pair[0] == ids["guoliang"] and pair[1] is not None,
                  f"unit={pair}")

        # ---------- 弱证据：追加门槛 ----------
        cc = await newcomer("mock_c")
        r = await answer(cc, relation="father",
                         relativeName="陈文山", selfName="陈小龙")
        RPT.check("★ 父已故（名字在树上公开）→ 先不放行，要求补填另一位家长",
                  r.get("result") == "ambiguous" and r.get("needExtra") is True,
                  json.dumps(r, ensure_ascii=False)[:300])
        RPT.check("★ 这一步不泄漏任何候选（否则等于把树变成可选名单）",
                  r.get("candidates") == [],
                  json.dumps(r.get("candidates"), ensure_ascii=False))

        r = await answer(cc, relation="father", relativeName="陈文山",
                         selfName="陈小龙", extraName="李桂芳")
        weak_id = int(r.get("personId") or 0)
        created.append(weak_id)
        RPT.check("补填另一位家长后放行（门槛是加一道，不是拒绝）",
                  r.get("result") == "new_member",
                  json.dumps(r, ensure_ascii=False)[:300])
        RPT.check("confidence=2（中）—— 弱证据建出来的节点要能被识别出来",
                  scalar("SELECT confidence FROM person WHERE id=:i", i=weak_id) == 2,
                  scalar("SELECT confidence FROM person WHERE id=:i", i=weak_id))

        # ---------- 繁简 ----------
        RPT.note("老谱是繁体，用户输入是简体 —— 不做归一化的话，字辈用字整批对不上")
        dd = await newcomer("mock_d")
        r = await answer(dd, relation="father",
                         relativeName="陳文山", selfName="陳國梁")
        RPT.check("★ 繁体输入能命中简体节点",
                  r.get("result") == "matched",
                  json.dumps(r, ensure_ascii=False)[:300])
        RPT.check("命中的是陈国梁本人",
                  r.get("personId") == ids["guoliang"],
                  f"personId={r.get('personId')}")

        # ---------- 顺手把「配某氏」补回来 ----------
        RPT.note("自己造一组夹具：母亲一格是空的，模拟老谱的「配某氏」")
        dad = db_add_person(fid, name="陈启峰", surname="陈", gender=1,
                            generation=4, role=LineageRole.MALE,
                            living=LivingStatus.LIVING)
        blank = db_add_person(fid, name=None, surname="王", gender=2,
                              generation=4, role=LineageRole.SPOUSE_IN,
                              living=LivingStatus.DECEASED)
        kid = db_add_person(fid, name="陈思贤", surname="陈", gender=1,
                            generation=5, role=LineageRole.MALE,
                            living=LivingStatus.LIVING)
        created += [dad, blank, kid]
        db_add_child(fid, db_add_union(fid, dad, blank), kid)
        RPT.check("夹具就绪：母亲一格为空",
                  scalar("SELECT name FROM person WHERE id=:i", i=blank) is None)

        ee = await newcomer("mock_e")
        r = await answer(ee, relation="father", relativeName="陈启峰",
                         selfName="陈思贤", extraName="王丽")
        RPT.check("命中本人节点", r.get("result") == "matched",
                  json.dumps(r, ensure_ascii=False)[:300])
        filled = r.get("filledFields") or []
        RPT.check("★ 顺手把空着的那一位补上了（回答本身产生数据）",
                  len(filled) == 1, json.dumps(filled, ensure_ascii=False))
        RPT.check("补的是母亲那一格", (filled[0].get("relation") == "mother"
                                   if filled else False),
                  json.dumps(filled, ensure_ascii=False))
        RPT.check("★ 空位真的写进库了（这是本产品最强的情绪点：把人写回来）",
                  scalar("SELECT name FROM person WHERE id=:i", i=blank) == "王丽",
                  scalar("SELECT name FROM person WHERE id=:i", i=blank))
        RPT.check("补名同样留下了 AUTO_MERGED 修订（可回滚，不是凭空改写）",
                  db_count("SELECT COUNT(*) FROM revision WHERE target_id=:i AND status=4",
                           i=blank) >= 1)

        # ---------- 对不上 ----------
        body = await answer_raw(ee, relation="father",
                                relativeName="张三丰", selfName="张无忌")
        r = body.get("data") or {}
        RPT.check("完全对不上 → no_match", r.get("result") == "no_match",
                  json.dumps(body, ensure_ascii=False)[:300])
        RPT.check("★ 反馈里不出现所填人名（不区分「没这人」和「不是你父亲」，"
                  "否则等于把树变成字典）",
                  "张三丰" not in json.dumps(body, ensure_ascii=False),
                  json.dumps(body, ensure_ascii=False)[:300])
        RPT.check("给出兜底通道（人工核实 / 先浏览全族）",
                  bool((r.get("fallback") or {}).get("hint")))

        # ---------- 已故祖先 ----------
        ff = await newcomer("mock_f")
        r = await answer(ff, relation="father",
                         relativeName="陈德昌", selfName="陈文海")
        RPT.check("★ 已故先祖不可认领（答案就写在树上，抄一下就能过）",
                  r.get("result") == "no_match",
                  json.dumps(r, ensure_ascii=False)[:300])
        RPT.check("且说明了原因，不是让人一头雾水",
                  "已故" in ((r.get("fallback") or {}).get("hint") or ""),
                  json.dumps(r.get("fallback"), ensure_ascii=False))

        # ---------- 非过错连试：不该被限速（2026-10-29）----------
        # 同一个「已故先祖」答案连答 6 次：每次都记 neutral。
        # 旧逻辑把它记成"错"，连撞 5 次就冷却一小时——用户填的是真名，
        # 错在规则，不在他。节点被他人占用（40901）同理。
        neutral_codes = []
        for _ in range(6):
            b = await answer_raw(ff, relation="father",
                                 relativeName="陈德昌", selfName="陈文海")
            neutral_codes.append(b.get("code"))
        RPT.check("★ 非用户过错（已故祖先 / 节点已被占）连试 6 次不被限速",
                  neutral_codes == [0] * 6, f"codes={neutral_codes}")

        # ---------- 限速 ----------
        gg = await newcomer("mock_g")
        codes, bodies = [], []
        for i in range(6):
            body = await answer_raw(gg, relation="father",
                                    relativeName=f"查无此人{i}", selfName="无关人士")
            codes.append(body.get("code"))
            bodies.append(body)
        RPT.check("连续答错前 5 次都被受理（正常试错不该被卡）",
                  codes[:5] == [0] * 5, f"codes={codes}")
        RPT.check("★ 第 6 次被限速 42900（挡住「父亲名 × 自己名」的暴力枚举）",
                  codes[5] == 42900, f"codes={codes}")

        rl = bodies[5].get("data") or {}
        RPT.check("★ 限速响应带 retryAfterSeconds（用户要知道等多久，不是「请稍后再试」）",
                  0 < int(rl.get("retryAfterSeconds") or 0) <= 3600,
                  json.dumps(rl, ensure_ascii=False))
        RPT.check("限速响应标出可被理事会解禁",
                  rl.get("unlockable") is True and rl.get("reason") == "trailing_fails",
                  json.dumps(rl, ensure_ascii=False))

        # ---------- 理事会解禁 ----------
        me_body = (await gg.call("GET", "/v1/auth/me"))[1].get("data") or {}
        gid = (me_body.get("user") or {}).get("id")
        st, body = await admin.call("GET", f"/v1/admin/identify/locks?familyId={fid}")
        locks = (body.get("data") or {}).get("items") or []
        RPT.check("★ 理事会在工作台看得到被限速的成员（不用让技术改库）",
                  any(x.get("userId") == gid for x in locks),
                  json.dumps(body.get("data"), ensure_ascii=False)[:400])
        if locks:
            RPT.check("名单带出「还要等多久」的人话",
                      bool(locks[0].get("retryAfterText")),
                      json.dumps(locks[0], ensure_ascii=False))
        if gid:
            st, body = await admin.call(
                "POST", "/v1/admin/identify/unlock",
                json={"familyId": fid, "userId": gid, "note": "冒烟测试解禁"})
            RPT.check("解禁成功", body.get("code") == 0,
                      json.dumps(body, ensure_ascii=False)[:300])
            b = await answer_raw(gg, relation="father",
                                 relativeName="查无此人X", selfName="无关人士")
            RPT.check("★ 解禁后立刻能再答（不用等窗口滑过）",
                      b.get("code") == 0,
                      json.dumps(b, ensure_ascii=False)[:200])
            st, body = await admin.call("GET", f"/v1/admin/identify/locks?familyId={fid}")
            RPT.check("解禁后从名单里消失",
                      not any(x.get("userId") == gid
                              for x in ((body.get("data") or {}).get("items") or [])))

        # ---------- 房长撤销 ----------
        RPT.note("自动通过 ≠ 不可纠错：房长看到可疑认领，要能一键收回")
        st, body = await admin.call("GET", f"/v1/admin/claims?familyId={fid}&status=2")
        items = (body.get("data") or {}).get("items") or []
        target = next((x for x in items if x.get("personId") == ids["qihua"]), None)
        RPT.check("房长在认领队列里看得到这条自动通过的认领",
                  target is not None,
                  json.dumps(body, ensure_ascii=False)[:300])
        if target:
            RPT.check("队列标出验证方式（房长才知道该重点看哪种）",
                      target.get("verifyMethodText") == "答题自证",
                      json.dumps(target, ensure_ascii=False)[:300])
            RPT.check("队列带出证据强度", target.get("evidenceStrength") == "strong",
                      json.dumps(target, ensure_ascii=False)[:300])

            _, body = await admin.call(
                "POST", f"/v1/admin/claim/{target['claimId']}/revert",
                json={"reviewNote": "冒烟测试：撤销"},
            )
            RPT.check("撤销成功", body.get("code") == 0,
                      json.dumps(body, ensure_ascii=False)[:300])
            RPT.check("★ 连带收回了 9 行编辑授权（撤销要连权限一起收，不能只改个状态）",
                      (body.get("data") or {}).get("revokedScopeRows") == 9,
                      json.dumps(body.get("data"), ensure_ascii=False))
            RPT.check("用户已解绑",
                      scalar("SELECT claimed_person_id FROM app_user "
                             "WHERE openid='mock_a'") is None)
            RPT.check("有效授权清零",
                      db_count("SELECT COUNT(*) FROM edit_scope WHERE status=1 AND "
                               "user_id=(SELECT id FROM app_user WHERE openid='mock_a')")
                      == 0)
            RPT.check("claim 状态标成 4（已撤回）",
                      scalar("SELECT status FROM claim WHERE id=:i",
                             i=target["claimId"]) == 4)
            _, body = await a.call("PATCH", f"/v1/person/{ids['qihua']}",
                                   json={"fields": {"occupation": "农民"}})
            RPT.check("★ 撤销后确实改不动了（40301）—— 权限是真的收回了",
                      body.get("code") == 40301,
                      json.dumps(body, ensure_ascii=False)[:300])
    finally:
        db_drop_fixture(*created, openids=openids)
        reset_rate_limit()
        RPT.note(f"夹具已清理：{len(created)} 个临时节点 + {len(openids)} 个测试账号解绑")


async def part_skippable(admin: Api, fid: int, ids: dict[str, int]) -> None:
    """「没有」也是一种回答：未婚 / 暂无子女。

    原来配偶是必填项、没有"未婚"这个出口。用户只能二选一：
    编一个名字（谱里凭空多出一个不存在的人），或者永远卡在 96%。
    启明实报的就是后者。
    """
    RPT.section("【10】未婚 / 暂无子女：允许回答「没有」")

    pid = ids["qihua"]
    st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
    ok = RPT.check(
        "陈启华的待补全清单可返回",
        st == 200 and body.get("code") == 0,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}",
    )
    if not ok:
        return

    d = body.get("data") or {}
    items = d.get("items") or []
    item = next((i for i in items if i.get("key") == "spouse"), None)
    if not RPT.check(
        "陈启华卡在「配偶」这一问上（复现启明报的场景）",
        item is not None,
        f"当前任务：{[i.get('key') for i in items]}",
    ):
        return

    RPT.check(
        "配偶任务开放了「没有」这个回答（skippable + 明确文案）",
        item.get("skippable") is True and bool(item.get("skipLabel")),
        json.dumps(item, ensure_ascii=False)[:300],
    )

    rev_before = int(scalar("select data_rev from family where id = :fid", fid=fid) or 0)

    st, body = await admin.call(
        "POST",
        f"/v1/person/{pid}/missing/submit",
        json={"key": "spouse", "skip": True},
    )
    RPT.check(
        "点「未婚」能提交成功",
        st == 200 and body.get("code") == 0,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}",
    )

    # 树接口对带 rev 的请求会回 notModified。data_rev 不 bump 的话，
    # 端上（尤其别的族人）永远拿着旧 rev 换不到新数据——底部条与角标都不消失。
    rev_after = int(scalar("select data_rev from family where id = :fid", fid=fid) or 0)
    RPT.check(
        "写库后 data_rev 递增（否则端上带旧 rev 只能拿到 notModified）",
        rev_after > rev_before,
        f"rev {rev_before} → {rev_after}",
    )

    st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
    d = body.get("data") or {}
    keys = [i.get("key") for i in (d.get("items") or [])]
    mc = int(d.get("missingCount") or 0)
    cp = float(d.get("completeness") or 0)
    RPT.check("答过「未婚」之后不再追问配偶", "spouse" not in keys, f"任务：{keys}")
    RPT.check(
        "★ 未婚也能到 100%（原来永久卡在 96% 下不来）",
        mc == 0 and cp >= 1.0,
        f"missingCount={mc} completeness={cp}",
    )

    # ---------- 首页底部那条「还差 N 项」必须一起消失 ----------
    # 这里是**同一件事的第三个出口**：补全页读 /person/{id}/missing，
    # 首页底部条读 /tree/{fid} 的 pending。两处各算一遍的话，
    # 用户会看到"已补全"和"还差 1 项"同时挂在屏幕上（启明实报）。
    # 所以断言的不是"接口能调通"，而是**两个出口的数字相等**。
    st, body = await admin.call("GET", f"/v1/tree/{fid}?view=mine&anchor={pid}")
    td = body.get("data") or {}
    pending = td.get("pending") or {}
    RPT.check(
        "★ 首页底部条与补全页同一口径：补全页 0 项时，树接口的 pending 也是 0",
        int(pending.get("missingCount", -1)) == mc == 0,
        f"tree.pending={pending} vs /missing missingCount={mc}",
    )
    node = next((n for n in (td.get("nodes") or []) if n.get("id") == pid), None)
    RPT.check(
        "本人节点上的「待补全」角标同时消失",
        node is not None and not node.get("hasPending") and not node.get("badge"),
        f"node={json.dumps(node, ensure_ascii=False)[:240]}" if node else "锚点不在返回节点里",
    )

    ms = scalar("select marital_status from person where id = :pid", pid=pid)
    RPT.check(
        "落库的是事实字段 marital_status=1，不是一次性的「已跳过」标记",
        int(ms or 0) == 1,
        f"marital_status={ms}；写成跳过标记的话，将来查谱看不到「未婚」这句话",
    )

    st, body = await admin.call(
        "POST",
        f"/v1/person/{pid}/missing/submit",
        json={"key": "spouse", "skip": True},
    )
    RPT.check(
        "重复提交被拒（任务已完成，不该刷出第二条修订）",
        body.get("code") != 0,
        json.dumps(body, ensure_ascii=False)[:200],
    )

    # ---------- 暂无子女 ----------
    # 找一个"在世 + 有婚姻 + 没有子女"的人。演示族里不一定有，没有就明确记一笔，
    # 不要静默假装测过了。
    row = db_first(
        "select p.id, p.name from person p "
        "where p.family_id = :fid and p.living_status = 1 and p.childless = 0 "
        "  and exists (select 1 from family_union u "
        "              where (u.partner_a_id = p.id or u.partner_b_id = p.id) "
        "                and u.status = 1) "
        "  and not exists (select 1 from parent_child c "
        "                  join family_union u2 on u2.id = c.union_id "
        "                  where (u2.partner_a_id = p.id or u2.partner_b_id = p.id) "
        "                    and c.status = 1) "
        "limit 1",
        fid=fid,
    )
    if row is None:
        RPT.note("演示族里没有「已婚且无子女」的在世者，这一段没有断言可跑")
        return

    cid, cname = int(row[0]), row[1]
    st, body = await admin.call("GET", f"/v1/person/{cid}/missing")
    keys = [i.get("key") for i in ((body.get("data") or {}).get("items") or [])]
    RPT.check(f"{cname} 有婚姻、没子女 → 会被问子女", "children" in keys, f"任务：{keys}")

    st, body = await admin.call(
        "POST",
        f"/v1/person/{cid}/missing/submit",
        json={"key": "children", "skip": True},
    )
    RPT.check(
        "点「暂无子女」能提交成功",
        st == 200 and body.get("code") == 0,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}",
    )
    st, body = await admin.call("GET", f"/v1/person/{cid}/missing")
    keys = [i.get("key") for i in ((body.get("data") or {}).get("items") or [])]
    RPT.check(f"答过「暂无子女」之后不再追问 {cname}", "children" not in keys, f"任务：{keys}")
    RPT.check(
        "childless 落库为 1",
        int(scalar("select childless from person where id = :pid", pid=cid) or 0) == 1,
    )


async def part_inlaw(admin: Api, member: Api, fid: int) -> None:
    """媳妇 / 女婿不是本族世系节点：不该被问父母。

    启明实报：以陈启华的身份点李秀英（媳妇，第 3 世），卡片说"还差一项"，
    点进去那一问是"您父亲叫什么名字？"。

    这一项**永远补不掉**：答了就把外姓父亲挂进本族世系（全族树里凭空
    长出一支外姓人），跳过则任务不落库、下次照旧问。于是"还差一项"
    永久挂在卡片上，点进去又永远补不完。
    """
    RPT.section("【12】媳妇 / 女婿：不问父母（否则「还差一项」永远消不掉）")

    row = db_first(
        "select id, name, lineage_role from person "
        "where family_id = :fid and lineage_role in (3, 4) and status = 1 "
        "order by lineage_role, id limit 1",
        fid=fid,
    )
    if row is None:
        RPT.note("演示族里没有媳妇 / 女婿节点，这一段没有断言可跑")
        return

    pid, name, role = int(row[0]), row[1], int(row[2])
    label = "媳妇" if role == 3 else "女婿"
    parent_keys = {"father", "mother", "mother_name"}

    st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
    d = body.get("data") or {}
    keys = [i.get("key") for i in (d.get("items") or [])]
    mc = int(d.get("missingCount") or 0)
    cp = float(d.get("completeness") or 0)
    RPT.check(
        f"★ {label}「{name}」的清单里没有父母（父母在娘家 / 岳家的谱上）",
        not (parent_keys & set(keys)),
        f"role={role} 任务：{keys}",
    )
    RPT.check(
        "★ 媳妇的完整度与清单同步：0 项缺口时必须是 100%",
        (mc == 0) == (cp >= 1.0),
        f"missingCount={mc} completeness={cp}（分母漏排父母会永远停在 67%）",
    )

    # 第五个出口：详情页那张卡片上的"还差 N 项"，必须和补全页同一个数。
    # 差一个数，用户就会看到"卡片说还差一项、补全页说都补上了"。
    st, body = await admin.call("GET", f"/v1/person/{pid}")
    pending = (body.get("data") or {}).get("pending") or {}
    RPT.check(
        "★ 卡片上的「还差 N 项」与补全页同一口径",
        int(pending.get("missingCount", -1)) == mc,
        f"detail.pending={pending} vs /missing missingCount={mc}",
    )

    # 卡片上的数字必须与「能不能补」绑死。
    # 原来判据是"本人或管理员"，比写权限（edit_scope）窄：
    # 房长给直系签了编辑权，却不让他看缺什么，页面上还照样显示缺口数，
    # 点进去 40300。现在统一到 viewer.can_edit()，这条断言锁住两者等价。
    st, body = await member.call("GET", f"/v1/person/{pid}")
    data = body.get("data") or {}
    can_edit = bool((data.get("myPermissions") or {}).get("canEdit"))
    RPT.check(
        "★ 别人的卡片上有没有缺口数，只取决于我能不能编辑他",
        (data.get("pending") is not None) == can_edit,
        f"pending={data.get('pending')} canEdit={can_edit}",
    )


async def part_role_grant(admin: Api, me: Api, fid: int) -> None:
    """角色授予：理事会成员是**指定的**，不是认领的。

    启明定的口径（2026-10-22）：2 号角色定位「理事会成员（含房长）」——
    真正的房长年纪大、不操作智能机，实际用后台的是理事会里会用手机
    的年轻人。所以必须有接口让族管理员提人 / 降人，双写快照。
    这条链路原来只有 seed 直写数据库一条路。
    """
    RPT.section("【14】角色授予：理事会成员由族管理员指定")

    uid = scalar("select id from app_user where openid = 'mock_qiming'")

    st, body = await me.call(
        "POST", "/v1/admin/role", json={"userId": uid, "role": 2},
    )
    RPT.check(
        "普通族人调授予接口被拒（40300）",
        body.get("code") == 40300,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )

    # 自己降自己：族管理员手滑把自己降成族人，全族就没人能进后台了
    admin_uid = scalar("select id from app_user where openid = 'mock_family_admin'")
    st, body = await admin.call(
        "POST", "/v1/admin/role", json={"userId": admin_uid, "role": 1},
    )
    RPT.check(
        "族管理员不能降低自己的角色",
        body.get("code") == 40300,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )

    # 授予比自己高的角色也不行（这里是平台运营 4，直接参数拒绝）
    st, body = await admin.call(
        "POST", "/v1/admin/role", json={"userId": uid, "role": 4},
    )
    RPT.check(
        "不能授予 3 以上的角色（族管理员造不出平台运营）",
        body.get("code") not in (0, None),
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )

    # 还没入族的人不能被授角色。注意不能用 mock_new_member——它在入族
    # 测试段里已经加入了本族，必须现场造一个从未入族的账号。
    fresh = Api(me.c, "role_guest")
    await fresh.login("mock_role_guest_only")
    fresh_uid = scalar(
        "select id from app_user where openid = 'mock_role_guest_only'"
    )
    st, body = await admin.call(
        "POST", "/v1/admin/role", json={"userId": fresh_uid, "role": 2},
    )
    RPT.check(
        "非本族成员不能被授予角色",
        body.get("code") not in (0,),
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )

    st, body = await admin.call(
        "POST", "/v1/admin/role", json={"userId": uid, "role": 2},
    )
    RPT.check(
        "族管理员能把成员提为理事会成员",
        st == 200 and (body.get("data") or {}).get("role") == 2,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )

    # 快照必须同步：app_user.role 是"当前族"快照，陈启明就停在本族
    st, body = await me.call("GET", "/v1/auth/me")
    user = ((body.get("data") or {}).get("user")) or (body.get("data") or {})
    RPT.check(
        "★ 快照同步：/auth/me 立即能看到新角色（不然端上拿到旧身份，权限忽有忽无）",
        int(user.get("role") or 0) == 2,
        f"role={user.get('role')}",
    )

    # 新晋理事会成员能写谱文（自建自拆）
    st, body = await me.call(
        "POST", f"/v1/family/{fid}/articles",
        json={"category": 6, "title": "【冒烟】理事会成员可写", "content": "测试"},
    )
    RPT.check(
        "理事会成员能编辑谱文（这次是真的能）",
        st == 200 and body.get("code") == 0,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )
    art_id = (body.get("data") or {}).get("id")
    if art_id:
        await me.call("DELETE", f"/v1/article/{art_id}")

    st, body = await admin.call(
        "POST", "/v1/admin/role", json={"userId": uid, "role": 1},
    )
    RPT.check(
        "族管理员能把人降回族人（授权要能收回）",
        st == 200 and (body.get("data") or {}).get("role") == 1,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )
    st, body = await me.call(
        "POST", f"/v1/family/{fid}/articles",
        json={"category": 6, "title": "【冒烟】降级后不可写", "content": "测试"},
    )
    RPT.check(
        "降级后写谱文被拒",
        body.get("code") == 40300,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:200]}",
    )


async def part_external_optout(admin: Api, fid: int, ids: dict[str, int]) -> None:
    """「暂不记外孙」也能落库。

    外孙（external）是**唯一一类跳过却没地方落库**的任务：用户点了
    「以后再说」，端上本地跳过、服务端照旧记着，于是每次进来都问一遍，
    完成度永远上不去（陈启明卡在 0.8 就是这个）。

    修法和「未婚」同一条思路：落的不是"跳过"标记，而是**一个决定**——
    external_opt_out=1，「我这一支的外孙不入谱」。老谱里"外孙不列"
    本身就是一句正经记载。
    """
    RPT.section("【13】暂不记外孙：回答要能落库，不能永远复发")

    pid = ids["qiming"]
    st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
    d = body.get("data") or {}
    item = next((i for i in (d.get("items") or []) if i.get("key") == "external"), None)
    if item is None:
        RPT.note("演示族里这一位当前没有外孙任务，这一段没有断言可跑")
        return

    RPT.check(
        "外孙任务带 skippable + 明确的 skipLabel（端上按钮文案直接用这个字段）",
        item.get("skippable") is True and bool(item.get("skipLabel")),
        json.dumps(item, ensure_ascii=False)[:300],
    )

    mc_before = int(d.get("missingCount") or 0)

    # 回归：女儿自己答了「暂无子女」，父母这边的外孙任务必须跟着消失——
    # 否则就是「女儿说了没有，爸妈那边还挂着一项」（67% 同类病）。
    daughter_id = (item.get("prefill") or {}).get("parentId")
    if daughter_id:
        def _set_childless(val: int) -> None:
            db = SessionLocal()
            try:
                db.execute(
                    text("update person set childless = :v where id = :pid"),
                    {"v": val, "pid": daughter_id},
                )
                db.commit()
            finally:
                db.close()

        _set_childless(1)
        st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
        keys_now = [i.get("key") for i in ((body.get("data") or {}).get("items") or [])]
        RPT.check(
            "女儿本人答「暂无子女」后，父母的外孙任务跟着消失",
            "external" not in keys_now,
            f"女儿 {daughter_id} childless=1，任务：{keys_now}",
        )
        _set_childless(0)
        st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
        d = body.get("data") or {}
        item = next((i for i in (d.get("items") or []) if i.get("key") == "external"), None)
        if item is None:
            RPT.note("复原 childless=0 后外孙任务没有回来，后续断言没有对象，跳过")
            return
        mc_before = int(d.get("missingCount") or 0)

    st, body = await admin.call(
        "POST", f"/v1/person/{pid}/missing/submit",
        json={"key": "external", "skip": True},
    )
    RPT.check(
        "点「暂不记外孙」能提交成功",
        st == 200 and body.get("code") == 0,
        f"HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}",
    )

    RPT.check(
        "落库的是事实字段 external_opt_out=1，不是一次性的跳过标记",
        int(scalar("select external_opt_out from person where id = :pid", pid=pid) or 0) == 1,
        "不落库的话，用户说了「不记」，下次进来照旧被问",
    )

    st, body = await admin.call("GET", f"/v1/person/{pid}/missing")
    d = body.get("data") or {}
    keys = [i.get("key") for i in (d.get("items") or [])]
    mc = int(d.get("missingCount") or 0)
    cp = float(d.get("completeness") or 0)
    RPT.check("答过之后不再追问外孙", "external" not in keys, f"任务：{keys}")
    RPT.check(
        "★ 缺口真的少了一项（不是只在端上消失）",
        mc == mc_before - 1 or (mc == 0 and mc_before <= 1),
        f"{mc_before} → {mc}",
    )
    RPT.check(
        "★ 完整度同步：0 缺口 ⟺ 100%",
        (mc == 0) == (cp >= 1.0),
        f"missingCount={mc} completeness={cp}",
    )

    # 第五、第六个出口：详情卡片、树上角标，都得跟着消失
    st, body = await admin.call("GET", f"/v1/person/{pid}")
    pending = (body.get("data") or {}).get("pending") or {}
    RPT.check(
        "详情卡片上的「还差 N 项」跟着归零",
        int(pending.get("missingCount", -1)) == mc,
        f"detail.pending={pending} vs /missing {mc}",
    )

    st, body = await admin.call("GET", f"/v1/tree/{fid}?view=mine&anchor={pid}")
    td = body.get("data") or {}
    node = next((n for n in (td.get("nodes") or []) if n.get("id") == pid), None)
    if mc == 0:
        RPT.check(
            "树上这个人节点的角标同时消失",
            node is not None and not node.get("hasPending") and not node.get("badge"),
            f"node={json.dumps(node, ensure_ascii=False)[:240]}",
        )

    st, body = await admin.call(
        "POST", f"/v1/person/{pid}/missing/submit",
        json={"key": "external", "skip": True},
    )
    RPT.check(
        "重复提交被拒（任务已完成，不该刷出第二条修订）",
        body.get("code") != 0,
        json.dumps(body, ensure_ascii=False)[:200],
    )


async def part_articles(admin: Api, member: Api, fid: int) -> None:
    """谱文：编修历史 / 迁徙源流的查看与编辑。

    权限口径（启明 2026-10-21 拍板）：读 = 本族成员；写 = 房长及以上、
    直接生效不走审核。删除是软删——「家族撤回权」在审计上要有据可查。
    每次跑完把自己建的都删干净，保证下轮仍从空列表开始（幂等）。
    """
    RPT.section("【11】谱文：编修历史 / 迁徙源流的查看编辑")

    # ---------- 空列表（上一轮的已被软删，不该再出现） ----------
    st, body = await admin.call("GET", f"/v1/family/{fid}/articles")
    RPT.check(
        "列表可拉取且初始为空（软删的不算）",
        st == 200 and (body.get("data") or {}).get("items") == [],
        json.dumps(body, ensure_ascii=False)[:300],
    )

    # ---------- 外人不可读 ----------
    stranger = Api(admin.c, "stranger")
    await stranger.login("mock_stranger_no_family")
    st, body = await stranger.call("GET", f"/v1/family/{fid}/articles")
    RPT.check(
        "未入族的访客读谱文被拒（族内资料，浏览码通道以后再说）",
        body.get("code") == 40300,
        json.dumps(body, ensure_ascii=False)[:200],
    )

    # ---------- 族人只读 ----------
    st, body = await member.call(
        "POST",
        f"/v1/family/{fid}/articles",
        json={"category": 3, "title": "族人越权", "content": "x"},
    )
    RPT.check(
        "普通族人写谱文被拒（房长门禁）",
        body.get("code") == 40300,
        json.dumps(body, ensure_ascii=False)[:200],
    )

    # ---------- 房长建两篇，列表按分类排序 ----------
    st, a1 = await admin.call(
        "POST",
        f"/v1/family/{fid}/articles",
        json={"category": 3, "title": "从饶州府迁广信", "content": "明初自饶州府鄱阳瓦屑坝迁入。\n\n二世祖分居灵溪。", "sortOrder": 1},
    )
    RPT.check(
        "房长能新建「迁徙源流」",
        st == 200 and a1.get("code") == 0
        and (a1.get("data") or {}).get("categoryLabel") == "迁徙源流",
        json.dumps(a1, ensure_ascii=False)[:300],
    )
    st, a2 = await admin.call(
        "POST",
        f"/v1/family/{fid}/articles",
        json={"category": 2, "title": "民国十二年重修记", "content": "民国十二年合族重修，历时三载乃成。", "sortOrder": 2},
    )
    if not RPT.check(
        "房长能新建「编修历史」",
        a2.get("code") == 0,
        json.dumps(a2, ensure_ascii=False)[:300],
    ):
        return

    id1 = (a1.get("data") or {})["id"]
    id2 = (a2.get("data") or {})["id"]

    st, body = await admin.call("GET", f"/v1/family/{fid}/articles")
    items = (body.get("data") or {}).get("items") or []
    RPT.check(
        "列表按分类排序（编修历史 cat=2 排在迁徙源流 cat=3 前，老谱目录的顺序）",
        [i["id"] for i in items] == [id2, id1],
        f"实际顺序：{[i['id'] for i in items]}",
    )
    RPT.check(
        "摘要已压成单行（列表里不该出现整段正文）",
        all("\n" not in i["excerpt"] and len(i["excerpt"]) <= 61 for i in items),
        json.dumps([i["excerpt"] for i in items], ensure_ascii=False)[:300],
    )

    # ---------- 详情 ----------
    st, body = await member.call("GET", f"/v1/article/{id1}")
    d = body.get("data") or {}
    RPT.check(
        "普通族人能读到全文",
        body.get("code") == 0 and "瓦屑坝" in d.get("content", ""),
        json.dumps(d, ensure_ascii=False)[:200],
    )

    # ---------- 参数校验 ----------
    st, body = await admin.call(
        "POST",
        f"/v1/family/{fid}/articles",
        json={"category": 9, "title": "x", "content": "y"},
    )
    RPT.check(
        "分类超出枚举被拒（自由填分类名三个月后就乱了，枚举是拍过板的）",
        body.get("code") == 40001,
        json.dumps(body, ensure_ascii=False)[:200],
    )

    # ---------- 更新 ----------
    st, body = await admin.call(
        "PUT",
        f"/v1/article/{id1}",
        json={"category": 3, "title": "从饶州府迁广信（增补）", "content": "增补：康熙年间一支迁玉山。", "sortOrder": 1},
    )
    d = body.get("data") or {}
    RPT.check(
        "房长能更新谱文（直接生效，不走修订审核）",
        body.get("code") == 0 and d.get("title") == "从饶州府迁广信（增补）",
        json.dumps(d, ensure_ascii=False)[:300],
    )

    # ---------- 软删 ----------
    st, body = await admin.call("DELETE", f"/v1/article/{id2}")
    RPT.check("房长能删除谱文", body.get("code") == 0, json.dumps(body, ensure_ascii=False)[:200])
    st, body = await member.call("GET", f"/v1/article/{id2}")
    RPT.check(
        "删后详情 404（族人视角立即消失）",
        body.get("code") == 40401,
        json.dumps(body, ensure_ascii=False)[:200],
    )
    st, body = await admin.call("GET", f"/v1/family/{fid}/articles")
    items = (body.get("data") or {}).get("items") or []
    RPT.check(
        "删后列表只剩 1 条",
        len(items) == 1 and items[0]["id"] == id1,
        f"列表：{[i['id'] for i in items]}",
    )

    # 清场：软删剩余的，保证下轮"初始为空"仍成立
    await admin.call("DELETE", f"/v1/article/{id1}")


# ============================================================


EXPECTED_PERSON_COUNT = 19

def preflight(fid: int, ids: dict[str, int]) -> list[str]:
    """跑之前先确认数据是「刚灌完」的状态。

    为什么必须有这一步：本脚本会真的改数据（给无名者填名字、给陈国华配配偶），
    所以它**不幂等**——连跑第二次必然失败。而失败的样子极具误导性：

        ❌ 种子数据里存在无名者（配某氏）
        ❌ 陈国华有「配偶」任务

    看着像"种子里没造这条数据"，实际是"上次跑完没复原"。不拦的话，
    排查方向会被带偏到 seed_demo.py 上去，而真正要做的只是重灌一次数据。
    """
    problems: list[str] = []
    db = SessionLocal()
    try:
        total = db.query(Person).filter(Person.family_id == fid).count()
        if total != EXPECTED_PERSON_COUNT:
            problems.append(
                f"人物数 {total}，期望 {EXPECTED_PERSON_COUNT}"
                "（多出来的是上次运行新增的人物，通常是「配偶」回归那段配的）"
            )
        if not ids["unnamed"]:
            problems.append(
                "找不到姓名为空的人物（配某氏）——上次运行已经把名字补上了"
            )
        gh = ids["guohua"]
        unions = (
            db.query(FamilyUnion)
            .filter(
                FamilyUnion.family_id == fid,
                or_(
                    FamilyUnion.partner_a_id == gh,
                    FamilyUnion.partner_b_id == gh,
                ),
            )
            .count()
        )
        if unions:
            problems.append(
                f"陈国华名下已有 {unions} 段婚姻——上次运行已给他配了配偶，"
                "「配偶不能挂成子女」这段回归没有可测的场景了"
            )
    finally:
        db.close()
    return problems


async def main() -> int:
    fid, ids = load_fixture()
    print("=" * 68)
    print("饶信谱脉 · 端到端冒烟")
    print(f"宗族 id = {fid}　启明 = {ids['qiming']}　无名者 = {ids['unnamed']}")
    print("=" * 68)

    problems = preflight(fid, ids)
    if problems:
        print("\n数据不是「刚灌完」的状态，本次冒烟无法开始。")
        for p in problems:
            print(f"  - {p}")
        print("\n本脚本会真的改数据，所以它**不幂等**：连跑第二次必然失败。")
        print("请先复原数据，再跑本脚本：")
        print("    .venv/Scripts/python.exe scripts/seed_demo.py")
        print("=" * 68)
        return 2

    # ---- 进程内打开 dev 登录（只影响本进程，一个字节都不动线上配置）----
    #
    # 线上 WX_MOCK_LOGIN=false 之后，code="dev:mock_x" 会被当成真 code 去调
    # jscode2session，必然失败；但冒烟测的是**业务机制**，不是线上配置。
    # 所以这里改的是本进程里的 settings 对象——请求走 ASGITransport 也在这个
    # 进程内，改了就生效；服务器上那份 .env 完全没被碰过。
    #
    # 「线上确实不认 dev: 后门」由 check_live.py 单独断言，不要在这里自证，
    # 否则就是拿"自己给自己开的锁"当证据。
    from app.core.config import settings as _settings

    _settings.wx_mock_login = True

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=60.0
    ) as client:
        admin = Api(client, "admin")
        await admin.login("mock_family_admin")

        await part_guest(client, fid, ids)
        # 【2】段自己造"未认领"账号（probe_entry），不再借用 part_guest 的登录态：
        # 演示账号的状态会被手工测试改掉，靠它做断言迟早变成假绿。
        await part_mask(client, fid, ids)
        me = await part_member(client, fid, ids)
        await part_unnamed(client, fid, ids, admin)
        # me 要传进去：范围外"先审后发"那条路径必须由一个**已认领的普通族人**触发
        await part_review(client, fid, ids, admin, me)
        await part_perm(client, fid, ids, me)
        # 用管理员跑这一段：普通人只能看自己的清单，看不到别人的补全任务
        await part_spouse_regression(client, fid, ids, admin)
        # 先验"进得来"：未入族 → 输码 → 答题自证才可用。
        # 放前面是因为它是整条链路的前置——前置不通，后面的答题自证
        # 在真实用户手里根本走不到。
        await part_entry(client, fid, ids)
        # 多族：父系谱 + 母系谱。放在入族链路之后——它复用同一个一次性账号，
        # 且自己造/拆一个空族，不碰别人的数据。
        await part_multi_family(client, fid, ids)
        await part_big_tree(client)
        await part_polygamy(client)
        await part_relation_link(admin)
        # 放最后：这一段会改陈启华的职业、并自建/自拆一组夹具，
        # 让前面的段落看到的是"刚灌完"的数据
        await part_identify(client, fid, ids, admin)
        # 再往后：这一段会把陈启华标成「未婚」，同样属于"跑完就得重新灌"的改动
        await part_skippable(admin, fid, ids)
        # 媳妇 / 女婿不该被问父母——启明实报"李秀英还差一项，点进去补不完"就是这条
        await part_inlaw(admin, me, fid)
        # 外孙：唯一一类"跳过却没地方落库"的任务，也要有个正经回答
        await part_external_optout(admin, fid, ids)
        # 谱文：自建自拆，跑完即复原
        await part_articles(admin, me, fid)
        # 角色授予：理事会成员由族管理员指定（房长本人多不操作智能机）
        await part_role_grant(admin, me, fid)

    print("\n" + "=" * 68)
    total = RPT.passed + RPT.failed
    if RPT.failed:
        print(f"结果：{RPT.passed}/{total} 通过，{RPT.failed} 项失败")
        for f in RPT.failures:
            print(f"  ✗ {f}")
    else:
        print(f"结果：{total} 项全部通过")
    print("提示：本脚本会真的改数据，需要复原请重跑 scripts/seed_demo.py")
    print("=" * 68)
    return 1 if RPT.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
