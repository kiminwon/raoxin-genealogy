"""全局枚举常量。

取值必须与 schema.sql 的字段注释、以及《家谱小程序-数据库设计.md》保持一致。
改这里等于改契约，改完要同步设计文档。
"""

from __future__ import annotations

from enum import Enum, IntEnum, StrEnum


class LineageRole(IntEnum):
    """谱系角色。决定这个人出现在哪张视图，而不是判断"是不是这家人"。"""

    MALE = 1            # 本族男丁（承袭世系）
    FEMALE = 2          # 本族女儿
    SPOUSE_IN = 3       # 媳妇（嫁入本族）
    SPOUSE_OUT = 4      # 女婿（本族女儿之夫）
    EXTERNAL_DESC = 5   # 外姓后代（外孙及其子孙）


# 决策第 2 条：外孙仅"我的一家"可见，全族树不展示。
# 2026-10-21 再收窄：全族总览改为**静态骨架，只画直系丁口**（不含媳妇）。
# 总览的用途是"知道这个族有多大、点进哪一支"，不是"看清每一家"；
# 媳妇进了骨架，横向宽度就被每个男丁的妻室线性撑开（3 妻 = 4 格），
# 而这一层的读者根本不需要看见她们——要看完整家庭，点进焦点视图即可。
OVERVIEW_VISIBLE_ROLES: tuple[int, ...] = (
    LineageRole.MALE,
    LineageRole.FEMALE,
)


class UnionType(IntEnum):
    FIRST_MARRIAGE = 1
    REMARRIAGE = 2
    CONCUBINE = 3        # 纳妾 / 第二配偶（只记录，不做价值判断，界面用中性词）
    UXORILOCAL_IN = 4    # 入赘（男方进女方家）
    UXORILOCAL_OUT = 5   # 招赘（女方招婿，孩子入本族）
    UNWED_BIRTH = 6      # 未婚生育
    UNKNOWN = 7


class UnionEndReason(IntEnum):
    """婚姻为什么结束。

    存在意义不是统计，而是**让"前任"有标记**：离异配偶照样进树
    （否则子女在自己那一屏里看不到母亲），但婚姻边要画虚线、
    卡片上要标「已离异」。没有这个枚举时只能靠 status 软删，
    而软删等于把母亲从孩子眼前抹掉。
    """

    DIVORCED = 1     # 离异
    WIDOWED = 2      # 丧偶
    ANNULLED = 3     # 婚姻无效 / 撤销


class MaritalStatus(IntEnum):
    """本人自述的婚姻状态。

    只解决一件事：**"没有配偶"要能回答**。原来配偶是必填项，
    未婚的人被永久卡在 96%，进度条永远到不了头。
    注意它和 family_union 不是一回事——union 记的是"和谁结的婚"，
    这里记的是"有没有婚姻"。有 union 时按 union 算，这一列只用来
    表达"明确没有"，所以 NULL 表示未确认（会继续问）。
    """

    SINGLE = 1       # 未婚
    MARRIED = 2      # 已婚
    WIDOWED = 3      # 丧偶
    DIVORCED = 4     # 离异


class ArticleCategory(IntEnum):
    """谱文分类（固定枚举，启明 2026-10-21 拍板）。

    为什么不用自由分类：老谱的正文就这几篇，固定枚举让"历次修谱各一篇谱序"
    同分类可并存，同时检索页能按传统谱的目录排——自由填分类名三个月后就乱了。
    """

    PREFACE = 1          # 谱序
    REVISION_HISTORY = 2 # 编修历史
    MIGRATION = 3        # 迁徙源流
    EXAMPLES = 4         # 凡例
    GENERATION_POEM = 5  # 字辈派语
    OTHER = 6            # 其他


class ParentRelation(IntEnum):
    BIOLOGICAL = 1       # 亲生
    ADOPTED = 2          # 过继 / 嗣
    FOSTER = 3           # 收养
    STEP = 4             # 继子女
    MATRILINEAL = 5      # 随母
    UNKNOWN = 6


class LivingStatus(IntEnum):
    LIVING = 1
    DECEASED = 2
    UNKNOWN = 3


class PrivacyLevel(IntEnum):
    PUBLIC = 1           # 公开
    LINEAGE = 2          # 族人可见（默认）
    CLOSE = 3            # 近亲可见
    PRIVATE = 4          # 私密：仅本人 + 管理员，近亲也不返回


class ViewerScope(StrEnum):
    """访问者视角。决定脱敏强度，由服务端算出后随响应头返回。

    这四个值本质是**身份等级**（不是关系远近），由 `build_viewer` 按
    「是否管理员 / 是否认领 / 是否入族」三档判定，顺序即优先级：

    - GUEST   未登录；或拿着族浏览码进来但还没入族
    - LINEAGE 已登录、已入族，但**还没认领节点**（不知道自己是谱上哪一位）
    - CLOSE   已认领（答题自证通过）—— **姓名脱敏的分界线就在这一档**
    - ADMIN   房长及以上

    别被 CLOSE 这个名字误导：它曾经表示"近亲"，改版答题自证之后它的实际
    含义就是"已认领"。姓名放开看的是这一档，而生日精度、职业、生平看的是
    `close_person_ids`（真正的近亲集合）——两者现在不是一回事了。
    """

    GUEST = "guest"
    LINEAGE = "lineage"
    CLOSE = "close"
    ADMIN = "admin"


class DatePrecision(IntEnum):
    UNKNOWN = 0
    YEAR = 1
    MONTH = 2
    DAY = 3


class Gender(IntEnum):
    UNKNOWN = 0
    MALE = 1
    FEMALE = 2


class ClaimType(IntEnum):
    CREATE_SELF = 1      # 新建本人节点
    CLAIM_EXISTING = 2   # 认领已有节点


class ClaimStatus(IntEnum):
    PENDING = 1
    APPROVED = 2
    REJECTED = 3
    WITHDRAWN = 4
    CONFLICT = 5         # 多人认领同一节点，不先到先得，标冲突


class VerifyMethod(IntEnum):
    """claim.verify_method —— 这个人是怎么证明「我是我」的。

    默认 ANSWER：房长不会用审批界面，人工审核不能是主路径。
    REVIEW 保留为兜底（答题失败、同名歧义、申诉）。
    """

    ANSWER = 1           # 答题自证：答出父母/配偶姓名
    REVIEW = 2           # 房长人工审核（兜底通道）
    INVITE = 3           # 节点级编辑码带入（签发人已确认过身份）


class IdentifyResult(StrEnum):
    """答题自证的结果。端上按它决定跳哪儿。"""

    MATCHED = "matched"            # 唯一命中，直接通过
    NEW_MEMBER = "new_member"      # 谱里没有我，但父亲在 —— 建新节点（接力修谱主路径）
    AMBIGUOUS = "ambiguous"        # 同名多人，需补答
    NO_MATCH = "no_match"          # 对不上，模糊提示 + 转人工


class ReviewStatus(IntEnum):
    """revision.status。"""

    PENDING = 1
    APPROVED = 2
    REJECTED = 3
    AUTO_MERGED = 4
    WITHDRAWN = 5


class RevisionOp(IntEnum):
    CREATE = 1
    UPDATE = 2
    DELETE = 3


class InviteCodeType(IntEnum):
    """决策第 5 条 方案 C：两种码。"""

    VIEW = 1             # 族级浏览码：只看不改
    EDIT = 2             # 节点级编辑码：可改锚点周围指定范围


class ScopeSource(IntEnum):
    CLAIM_SELF = 1       # 认领自己
    INVITE_CODE = 2      # 邀请码
    BRANCH_HEAD = 3      # 房长授权
    ADMIN = 4            # 管理员


class SourceType(IntEnum):
    """史料来源。"""

    OLD_BOOK = 1         # 老谱录入
    SELF_REPORTED = 2    # 族人填报
    OCR = 3              # OCR 导入
    ORAL = 4             # 口述
    AI_INFERRED = 5      # AI 推断


class Confidence(IntEnum):
    HIGH = 1
    MEDIUM = 2
    QUESTIONABLE = 3


class UserRole(IntEnum):
    NORMAL = 1
    BRANCH_HEAD = 2      # 理事会成员（含房长授权——房长本人多不操作智能机，实际由理事会里的年轻人代管）
    FAMILY_ADMIN = 3     # 族管理员
    PLATFORM = 4         # 平台运营


class TreeView(StrEnum):
    MINE = "mine"            # 我的一家：WXML 渲染
    LINEAGE = "lineage"      # 我的支系：虚拟列表
    OVERVIEW = "overview"    # 全族总览：Canvas


class EdgeType(IntEnum):
    """树的边类型，端上按此决定线型。"""

    PARENT_CHILD = 1     # 父子：实线
    MARRIAGE = 2         # 婚姻：横向双线
    ADOPTED = 3          # 过继：虚线
    EXTERNAL = 4         # 外姓 / 外孙：淡色虚线


class NotificationType(IntEnum):
    CLAIM_RESULT = 1
    REVISION_REVIEWED = 2
    NEARBY_CLAIMED = 3
    FIELD_CONFLICT = 4
    INVITE_ACCEPTED = 5
    MISSING_REMINDER = 6


class MissingTaskType(StrEnum):
    """待补全任务的类型，决定前端用哪种表单。"""

    CREATE_PERSON = "create_person"
    UPDATE_FIELD = "update_field"
    CREATE_UNION = "create_union"


# 编辑码默认授权范围（决策第 5 条方案 C）
#   up          锚点向上几代
#   down        锚点向下几代（不含外孙）
#   spouse      是否含各代配偶
#   siblings    是否含兄弟姐妹
#   externalDesc 外孙及外姓后代向下几层
DEFAULT_SCOPE_UP_DOWN: dict[str, object] = {
    "up": 3,
    "down": 2,
    "spouse": True,
    "siblings": False,
    "externalDesc": 2,
}
