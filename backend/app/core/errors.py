"""统一错误码与业务异常。

所有接口返回 {"code": int, "msg": str, "data": any}。
code == 0 表示成功。错误码表见《家谱小程序-接口定义.md》第 0.3 节。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrCode(IntEnum):
    OK = 0

    PARAM_INVALID = 40001          # 参数错误
    UNAUTHORIZED = 40100           # 未登录 / token 失效

    FORBIDDEN = 40300              # 无权限访问该资源
    NO_EDIT_SCOPE = 40301          # 无编辑权限（不在 edit_scope 内）
    INVITE_INVALID = 40302         # 邀请码无效 / 过期 / 耗尽

    NOT_FOUND = 40401              # 资源不存在
    CLAIM_CONFLICT = 40901         # 认领冲突：该节点已被他人认领
    EDIT_CONFLICT = 40902          # 编辑冲突：同一字段存在其他待审修订

    RATE_LIMITED = 42900           # 频率限制
    SERVER_ERROR = 50000           # 服务端错误


class BizError(Exception):
    """业务异常。在 api 层被统一捕获并转成标准响应体。"""

    def __init__(self, code: ErrCode, msg: str = "", data: Any = None) -> None:
        self.code = int(code)
        self.msg = msg or code.name
        self.data = data
        super().__init__(self.msg)

    def __str__(self) -> str:  # pragma: no cover
        return f"[{self.code}] {self.msg}"


# ---------- 常用快捷构造 ----------

def not_found(what: str = "资源") -> BizError:
    return BizError(ErrCode.NOT_FOUND, f"{what}不存在")


def forbidden(msg: str = "您没有权限访问该资源") -> BizError:
    return BizError(ErrCode.FORBIDDEN, msg)


def no_edit_scope(msg: str = "您没有编辑该节点的权限，请先申请扩展授权") -> BizError:
    return BizError(ErrCode.NO_EDIT_SCOPE, msg)


def unauthorized(msg: str = "未登录或登录已过期") -> BizError:
    return BizError(ErrCode.UNAUTHORIZED, msg)


def param_invalid(msg: str = "参数错误") -> BizError:
    return BizError(ErrCode.PARAM_INVALID, msg)


def ok(data: Any = None) -> dict:
    return {"code": 0, "msg": "ok", "data": data}
