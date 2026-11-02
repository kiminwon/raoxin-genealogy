"""灌入演示数据：广信陈氏（六代 26 人 + 3 位用户）。

用法：
    cd backend
    .venv\\Scripts\\python.exe scripts/seed_demo.py

刻意覆盖的场景：
  - 长房 / 二房两个支系
  - "配某氏"：只知道姓、不知道名字（老谱常态，会进待补全清单）
  - 过继：陈国华既是文海之子，又是文山之嗣
  - 招赘 + 外孙：女儿陈雨欣的孩子林子轩（决策第 2 条：只在我的一家显示）
  - 已故与在世混合，用于验证脱敏规则
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.core.constants import (  # noqa: E402
    ClaimStatus,
    Confidence,
    DatePrecision,
    Gender,
    InviteCodeType,
    LineageRole,
    LivingStatus,
    ParentRelation,
    PrivacyLevel,
    ScopeSource,
    SourceType,
    UnionType,
    UserRole,
)
from app.db.session import SessionLocal  # noqa: E402
from app.models.family import Branch, Family  # noqa: E402
from app.models.person import FamilyUnion, ParentChild, Person  # noqa: E402
from app.models.system import (  # noqa: E402
    AuditLog,
    Notification,
    Revision,
    TreeLayoutCache,
)
from app.models.user import AppUser, Claim, EditScope, InviteCode, UserFamily  # noqa: E402
from app.services.scope_service import RelationGraph  # noqa: E402


def D(s: str | None) -> date | None:
    if not s:
        return None
    parts = [int(x) for x in s.split("-")]
    while len(parts) < 3:
        parts.append(1)
    return date(parts[0], parts[1], parts[2])


def main() -> None:
    db = SessionLocal()
    try:
        _wipe(db)
        family = _family(db)
        branches = _branches(db, family)
        people = _persons(db, family, branches)
        _unions_and_children(db, family, people)
        _users(db, family, people)
        family.data_rev = 1
        db.commit()

        graph = RelationGraph.load(db, family.id)
        print("\n演示数据灌入完成")
        print(f"  宗族：{family.name}（{family.settle_place}）")
        print(f"  人物：{len(graph.person_of)} 人")
        print(f"  婚姻：{len(graph.unions)} 段")
        print(f"  亲子：{len(graph.links)} 条")
        _report(graph)
        print("\n可用的演示入口：")
        print(f"  全族浏览码：{family.view_code}")
        print("  权限只来自答题认领与角色（编辑码已下线，POST /v1/invite/generate 只签发浏览码）")
        print(f"\n  试试：curl http://127.0.0.1:8000/v1/family/{family.id}/summary")
    finally:
        db.close()


# ============================================================


WIPE_MODELS = (
    Notification, Revision, TreeLayoutCache, AuditLog,
    # UserFamily 必须排在 AppUser 前面：它记的是账号与族的成员关系，
    # 先删账号再删关系，若中途失败会留下指不到人的孤儿行。
    InviteCode, EditScope, Claim, UserFamily, AppUser,
    ParentChild, FamilyUnion, Person, Branch, Family,
)


def _wipe(db) -> None:
    for model in WIPE_MODELS:
        db.query(model).delete()
    db.commit()
    # 重置自增。DELETE 不重置 AUTO_INCREMENT，反复灌数据会让 family.id 变成 2、3、4……
    # 而文档、编译模式、日志里到处都写着具体 id，所以这里强制回到 1，保证可复现。
    for model in WIPE_MODELS:
        db.execute(text(f"ALTER TABLE `{model.__tablename__}` AUTO_INCREMENT = 1"))
    db.commit()


def _family(db) -> Family:
    f = Family(
        surname="陈",
        name="广信陈氏",
        tang_hao="颍川堂",
        jun_wang="颍川",
        origin_place="福建建阳",
        settle_place="上饶市广信区茶亭镇",
        founder_name="陈公讳德昌",
        founder_era="明永乐年间",
        generation_poem="文章华国诗礼传家",
        generation_poem_note="自第十五世起，男女通用；后二十字待族老补录",
        first_gen_label=15,
        gen_poem_enabled=1,
        view_code="CHEN2026",
        view_code_enabled=1,
        data_rev=0,
        status=1,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _branches(db, family: Family) -> dict[str, Branch]:
    out = {}
    for i, name in enumerate(["长房", "二房", "三房"], start=1):
        b = Branch(family_id=family.id, name=name, sort_no=i, status=1,
                   created_at=datetime.now())
        db.add(b)
        out[name] = b
    db.commit()
    return out


def _persons(db, family: Family, branches: dict[str, Branch]) -> dict[str, Person]:
    fid = family.id
    long_, second, third = branches["长房"].id, branches["二房"].id, branches["三房"].id

    rows = [
        # key, 名, 姓, 性别, 世, 角色, 在世, 承袭, 房, 生, 卒, 葬, 学历, 职业, 生平, 籍贯
        ("dechang", "陈德昌", "陈", 1, 1, LineageRole.MALE, 2, 1, long_,
         "1403-01-01", "1478-01-01", "茶亭镇陈家坞后山", None, "耕读",
         "明永乐年间由福建建阳迁居广信，为陈家坞开基之祖。", "广信区茶亭镇"),
        ("wangshi", "王秀英", "王", 2, 1, LineageRole.SPOUSE_IN, 2, 0, long_,
         "1408-01-01", "1490-01-01", "与夫合葬", None, None, None, None),

        ("wenshan", "陈文山", "陈", 1, 2, LineageRole.MALE, 2, 1, long_,
         "1940-05-01", "2011-03-12", "茶亭镇陈家坞祖坟山", "私塾", "农民",
         "生于民国二十九年，一生务农，育有三子一女。", "广信区茶亭镇"),
        ("liguifang", "李桂芳", "李", 2, 2, LineageRole.SPOUSE_IN, 2, 0, long_,
         "1943-08-20", "2018-11-02", "与夫合葬", None, "农民", None, None),
        ("wenhai", "陈文海", "陈", 1, 2, LineageRole.MALE, 2, 1, second,
         "1946-02-11", "2020-06-30", "茶亭镇南山", "小学", "木匠", None, "广信区茶亭镇"),
        # 老谱典型：只记姓氏，不知名字 —— 这条会进待补全清单
        ("zhangshi", None, "张", 2, 2, LineageRole.SPOUSE_IN, 2, 0, second,
         None, None, None, None, None, None, None),

        ("guodong", "陈国栋", "陈", 1, 3, LineageRole.MALE, 1, 1, long_,
         "1965-03-12", None, None, "高中", "教师",
         "生于茶亭镇陈家坞，1984 年起任教于茶亭镇中心小学。", "广信区茶亭镇"),
        ("lixiuying", "李秀英", "李", 2, 3, LineageRole.SPOUSE_IN, 1, 0, long_,
         "1967-09-05", None, None, "初中", "农民", None, "广信区茶亭镇"),
        ("guoliang", "陈国梁", "陈", 1, 3, LineageRole.MALE, 1, 1, long_,
         "1968-11-23", None, None, "初中", "个体经营", None, "广信区茶亭镇"),
        ("zhouxiaoyan", "周小燕", "周", 2, 3, LineageRole.SPOUSE_IN, 1, 0, long_,
         "1970-04-18", None, None, "初中", "个体经营", None, "广信区茶亭镇"),
        # 过继：文海之子，出继给长房文山为嗣
        ("guohua", "陈国华", "陈", 1, 3, LineageRole.MALE, 1, 1, long_,
         "1972-07-14", None, None, "大专", "工程师", None, "广信区茶亭镇"),

        ("qiming", "陈启明", "陈", 1, 4, LineageRole.MALE, 1, 1, long_,
         "1988-06-15", None, None, "本科", "软件工程师",
         "生于茶亭镇，现在杭州工作，2026 年发起本族家谱数字化。", "广信区茶亭镇"),
        ("wumin", "吴敏", "吴", 2, 4, LineageRole.SPOUSE_IN, 1, 0, long_,
         "1990-02-08", None, None, "本科", "会计", None, "上饶市信州区"),
        ("qihua", "陈启华", "陈", 1, 4, LineageRole.MALE, 1, 1, long_,
         "1991-10-30", None, None, "本科", "医生", None, "广信区茶亭镇"),
        ("yuxin", "陈雨欣", "陈", 2, 4, LineageRole.FEMALE, 1, 0, long_,
         "1994-08-21", None, None, "本科", "教师", None, "广信区茶亭镇"),
        ("linzhiqiang", "林志强", "林", 1, 4, LineageRole.SPOUSE_OUT, 1, 0, long_,
         "1992-12-03", None, None, None, None, None, None),

        ("siyuan", "陈思远", "陈", 1, 5, LineageRole.MALE, 1, 1, long_,
         "2010-05-09", None, None, None, None, None, None),
        ("sitong", "陈思彤", "陈", 2, 5, LineageRole.FEMALE, 1, 0, long_,
         "2016-11-11", None, None, None, None, None, None),
        # 招赘所出，孩子随母入本族谱
        ("zixuan", "林子轩", "林", 1, 5, LineageRole.EXTERNAL_DESC, 1, 0, long_,
         "2020-03-28", None, None, None, None, None, None),
    ]

    people: dict[str, Person] = {}
    for (key, name, surname, gender, gen, role, living, carrier, branch_id,
         birth, death, burial, edu, occ, bio, native) in rows:
        p = Person(
            family_id=fid,
            branch_id=branch_id,
            generation=gen,
            lineage_role=role,
            is_lineage_carrier=carrier,
            name=name,
            surname=surname,
            gender=gender,
            birth_date=D(birth),
            birth_date_precision=DatePrecision.DAY if birth and len(birth) > 4 else DatePrecision.UNKNOWN,
            death_date=D(death),
            death_date_precision=DatePrecision.DAY if death else DatePrecision.UNKNOWN,
            living_status=living,
            burial_place=burial,
            education=edu,
            occupation=occ,
            biography=bio,
            native_place=native,
            source_type=SourceType.OLD_BOOK if gen <= 3 else SourceType.SELF_REPORTED,
            source_note="光绪十八年陈氏宗谱 卷三" if gen <= 2 else "族人填报",
            confidence=Confidence.HIGH if gen <= 3 else Confidence.MEDIUM,
            privacy_level=PrivacyLevel.PUBLIC if living == LivingStatus.DECEASED else PrivacyLevel.LINEAGE,
            status=1,
        )
        db.add(p)
        people[key] = p

    db.commit()
    for p in people.values():
        db.refresh(p)
    return people


def _unions_and_children(db, family: Family, people: dict[str, Person]) -> None:
    fid = family.id
    U = UnionType

    unions = [
        ("u1", "dechang", "wangshi", U.FIRST_MARRIAGE),
        ("u2", "wenshan", "liguifang", U.FIRST_MARRIAGE),
        ("u3", "wenhai", "zhangshi", U.FIRST_MARRIAGE),
        ("u4", "guodong", "lixiuying", U.FIRST_MARRIAGE),
        ("u5", "guoliang", "zhouxiaoyan", U.FIRST_MARRIAGE),
        # 招赘：partner_a 是承袭世系的一方（女儿），孩子随母入本族谱
        ("u6", "yuxin", "linzhiqiang", U.UXORILOCAL_OUT),
        ("u7", "qiming", "wumin", U.FIRST_MARRIAGE),
    ]

    umap: dict[str, FamilyUnion] = {}
    for key, a, b, utype in unions:
        u = FamilyUnion(
            family_id=fid,
            partner_a_id=people[a].id,
            partner_b_id=people[b].id,
            union_type=utype,
            union_seq=1,
            start_date=D("1990-01-01") if people[a].generation >= 4 else None,
            start_date_precision=DatePrecision.UNKNOWN,
            status=1,
        )
        db.add(u)
        umap[key] = u
    db.commit()
    for u in umap.values():
        db.refresh(u)

    R = ParentRelation
    links = [
        # 第 2 世
        ("u1", "wenshan", R.BIOLOGICAL, 1, 1, 1),
        ("u1", "wenhai", R.BIOLOGICAL, 1, 2, 1),
        # 第 3 世
        ("u2", "guodong", R.BIOLOGICAL, 1, 1, 1),
        ("u2", "guoliang", R.BIOLOGICAL, 1, 2, 1),
        # 陈国华：本生父是文海（is_primary=0）
        ("u3", "guohua", R.BIOLOGICAL, 0, 1, 1),
        # 同时是文山之嗣（is_primary=1，树上显示在这一支）
        ("u2", "guohua", R.ADOPTED, 1, 3, 0),
        # 第 4 世
        ("u4", "qiming", R.BIOLOGICAL, 1, 1, 1),
        ("u4", "qihua", R.BIOLOGICAL, 1, 2, 1),
        ("u4", "yuxin", R.BIOLOGICAL, 1, 3, 1),
        # 第 5 世
        ("u6", "zixuan", R.BIOLOGICAL, 1, 1, 1),   # 外孙
        ("u7", "sitong", R.BIOLOGICAL, 1, 1, 1),
        ("u7", "siyuan", R.BIOLOGICAL, 1, 2, 1),   # 注：思远实为启明之侄，此处仅作演示
    ]

    for ukey, ckey, rel, primary, seq, biological in links:
        db.add(
            ParentChild(
                family_id=fid,
                union_id=umap[ukey].id,
                child_id=people[ckey].id,
                relation=rel,
                is_primary=primary,
                is_biological=biological,
                seq=seq,
                status=1,
            )
        )
    db.commit()


def _users(db, family: Family, people: dict[str, Person]) -> None:
    fid = family.id

    guodong = AppUser(
        openid="mock_family_admin",
        nickname="国栋",
        role=UserRole.FAMILY_ADMIN,
        family_id=fid,
        claimed_person_id=people["guodong"].id,
        status=1,
    )
    qiming = AppUser(
        openid="mock_qiming",
        nickname="启明",
        role=UserRole.NORMAL,
        family_id=fid,
        claimed_person_id=people["qiming"].id,
        status=1,
    )
    guest = AppUser(openid="mock_new_member", nickname="新族人", role=UserRole.NORMAL, status=1)
    db.add_all([guodong, qiming, guest])
    db.commit()
    for u in (guodong, qiming, guest):
        db.refresh(u)

    # 成员关系表。app_user 上那三列只是"当前族快照"，真正"我属于哪些族"记在这里。
    # 两边必须同时写，否则 seed 完第一次切族，快照就会和成员关系脱节。
    # guest（mock_new_member）刻意不建行：它是"还没入族"的场景，用来测落地页输码。
    now = datetime.now()
    for u, role, claimed in (
        (guodong, UserRole.FAMILY_ADMIN, people["guodong"].id),
        (qiming, UserRole.NORMAL, people["qiming"].id),
    ):
        db.add(
            UserFamily(
                user_id=u.id,
                family_id=fid,
                role=role,
                claimed_person_id=claimed,
                status=1,
                joined_at=now,
                last_active_at=now,
            )
        )
    db.commit()

    # 授权：国栋是族管理员（全权），启明可编辑自己和直系上下两代
    for uid, keys in (
        (guodong.id, ["guodong", "qiming", "qihua", "yuxin", "lixiuying"]),
        (qiming.id, ["qiming", "guodong", "lixiuying", "qihua", "yuxin", "zixuan", "sitong"]),
    ):
        for k in keys:
            db.add(
                EditScope(
                    user_id=uid,
                    family_id=fid,
                    person_id=people[k].id,
                    source=ScopeSource.CLAIM_SELF,
                    granted_at=datetime.now(),
                    status=1,
                )
            )

    db.add(
        Claim(
            family_id=fid,
            user_id=qiming.id,
            person_id=people["qiming"].id,
            claim_type=2,
            evidence="我父亲是陈国栋，我生于1988年，现居杭州。",
            status=ClaimStatus.APPROVED,
            reviewer_user_id=guodong.id,
            reviewed_at=datetime.now(),
            created_at=datetime.now(),
        )
    )

    # 给启明预置一张编辑码（发给二叔用）
    graph = RelationGraph.load(db, fid)
    scope = graph.resolve_scope(people["qiming"].id)
    db.add(
        InviteCode(
            family_id=fid,
            code="K7M2P9QT",
            code_type=InviteCodeType.EDIT,
            anchor_person_id=people["qiming"].id,
            anchor_user_id=qiming.id,
            scope_rule={"up": 3, "down": 2, "spouse": True, "siblings": False, "externalDesc": 2},
            scope_person_ids=scope,
            scope_node_count=len(scope),
            inviter_user_id=qiming.id,
            invite_note="给二叔",
            max_uses=0,
            used_count=0,
            status=1,
        )
    )
    db.commit()


def _report(graph: RelationGraph) -> None:
    from app.services.missing import build_missing_tasks, count_missing_for_family

    stats = count_missing_for_family(graph)
    print("\n  数据完整度：", f"{stats['completeness'] * 100:.1f}%")
    print(f"    缺配偶 {stats['missingSpouseCount']}　"
          f"缺母亲姓名 {stats['missingMotherCount']}　"
          f"缺出生 {stats['missingBirthCount']}　"
          f"缺葬地 {stats['missingBurialCount']}")

    # 找一个有待补全任务的人演示接力修谱的入口
    for pid, person in graph.person_of.items():
        if not person.name:
            continue
        tasks = build_missing_tasks(graph, pid, max_items=3)
        if tasks["missingCount"] >= 2:
            print(f"\n  待补全示例 —— {person.name}：还差 {tasks['missingCount']} 项")
            for t in tasks["items"]:
                print(f"    · {t['question']}   （{t['reason']}）")
            break


if __name__ == "__main__":
    main()
