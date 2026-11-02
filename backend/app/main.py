"""FastAPI 应用入口。

启动：
    cd backend
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

接口文档： http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.errors import BizError, ErrCode

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("raoxin")

app = FastAPI(
    title="饶信谱脉 · 家谱 API",
    description="上饶广信区家谱数字化小程序后端",
    version="1.0.0",
)

# 开发期允许任意来源；上线时收紧为小程序 request 合法域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_dev else [],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------
# 统一响应：{code, msg, data}
# 模型把异常和参数错误都转成这个形状，小程序端只需判断 code === 0
# ---------------------------------------------------------------

@app.exception_handler(BizError)
async def handle_biz_error(request: Request, exc: BizError) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"code": exc.code, "msg": exc.msg, "data": exc.data},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse(
        status_code=200,
        content={
            "code": int(ErrCode.PARAM_INVALID),
            "msg": f"参数错误：{loc} {first.get('msg', '')}".strip(),
            "data": {"errors": exc.errors()[:5]},
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("未捕获异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=200,
        content={
            "code": int(ErrCode.SERVER_ERROR),
            "msg": "服务端开小差了，请稍后再试",
            "data": {"detail": str(exc)} if settings.debug else None,
        },
    )


# ---------------------------------------------------------------
# 路由
# ---------------------------------------------------------------

@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict:
    return {"code": 0, "msg": "ok", "data": {"env": settings.app_env, "version": app.version}}


app.include_router(api_router, prefix="/v1")


