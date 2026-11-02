"""用户与宗族的成员关系：加入、切换、列出。

设计要点（和 app_user 那三列的关系，别搞混）：

    user_family            = 我加入过哪些族，每族各自的角色/认领谁  ← **唯一真相**
    app_user.family_id     = 我此刻正在看哪个族                    ← 只是快照
    app_user.role          = 我在此刻这个族里是什么角色            ← 只是快照
    app_user.claimed_person_id = 我在此刻这个族认领了谁            ← 只是快照

全站鉴权（scope_service.build_viewer、person 写权限、admin 房长门禁）
读的都是 app_user 那三列的当前值。所以只要保证**切族时把三列回填正确**，
加多族就不用碰任何鉴权代码 —— 这是这套设计的全部价值所在。

反过来说：任何直接写 app_user.family_id 的地方都是 bug，
一律走 join() / activate() / set_claimed()。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BizError, ErrCode
from app.models.user import AppUser, UserFamily


def get_member(db: Session, user_id: int, family_id: int) -> UserFamily | None:
    """查某人在某族的成员记录（不区分 status）。"""
    return db.execute(
        select(UserFamily).where(
            UserFamily.user_id == user_id,
            UserFamily.family_id == family_id,
        )
    ).scalar_one_or_none()


def list_for_user(db: Session, user_id: int) -> list[UserFamily]:
    """我加入的全部族，当前族排最前，其余按最近活跃倒序。"""
    return list(
        db.execute(
            select(UserFamily)
            .where(UserFamily.user_id == user_id, UserFamily.status == 1)
            .order_by(UserFamily.last_active_at.desc(), UserFamily.id.desc())
        ).scalars()
    )


def join(
    db: Session,
    user: AppUser,
    family_id: int,
    *,
    role: int | None = None,
) -> UserFamily:
    """把用户记录为该族成员（幂等）。**不切当前族** —— 切族是 activate() 的事。

    为什么把"加入"和"切换"拆开：加入是既成事实（他确实在这个族的谱里），
    切换是"我现在想看哪个"。认领别人家族的一个节点不该顺手把当前族改掉，
    但点了这个族的码进来又确实该切过去 —— 两种意图，两个函数。
    """
    m = get_member(db, user.id, family_id)
    if m is None:
        m = UserFamily(user_id=user.id, family_id=family_id, role=role or 1, status=1)
        db.add(m)
        db.flush()
        return m

    if m.status != 1:
        # 曾经退出过又回来了：恢复成员身份，但保留原来的角色和认领记录
        m.status = 1
    # 角色只升不降：邀请码/后台可以把他提成房长，但重新入族不该把房长降回普通用户
    if role is not None and role > int(m.role):
        m.role = role
    return m


def activate(db: Session, user: AppUser, family_id: int) -> UserFamily:
    """切到该族：把 app_user 三个快照列回填成该族的值。

    调用方必须先 join() 过 —— 没入过的族不能切，否则等于靠切族绕过入族。
    """
    m = get_member(db, user.id, family_id)
    if m is None or m.status != 1:
        raise BizError(ErrCode.FORBIDDEN, "你还没有加入这个宗族")

    user.family_id = family_id
    user.role = int(m.role)
    user.claimed_person_id = m.claimed_person_id
    m.last_active_at = datetime.now()
    return m


def join_and_activate(db: Session, user: AppUser, family_id: int) -> UserFamily:
    """加入并切过去。用于"用户主动走进了这个族"的场合：接受邀请码、认领成功。"""
    m = join(db, user, family_id)
    activate(db, user, family_id)
    return m


def set_claimed(db: Session, user: AppUser, family_id: int, person_id: int) -> UserFamily:
    """认领成功后落库：本族成员记录 + 当前族快照（仅当该族正是当前族）。"""
    m = join(db, user, family_id)
    m.claimed_person_id = person_id
    if user.family_id == family_id:
        user.claimed_person_id = person_id
    return m


def family_ids_of(db: Session, user_id: int) -> set[int]:
    """我加入的族 id 集合。给需要批量判断的场合用。"""
    return {
        int(x)
        for x in db.execute(
            select(UserFamily.family_id).where(
                UserFamily.user_id == user_id, UserFamily.status == 1
            )
        ).scalars()
    }
