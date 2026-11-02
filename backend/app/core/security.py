"""JWT 签发与校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import settings
from app.core.errors import unauthorized

ALGORITHM = "HS256"


def create_access_token(user_id: int, extra: dict[str, Any] | None = None) -> tuple[str, int]:
    """签发 token。返回 (token, expires_in_seconds)。"""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=settings.access_token_expire_days)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, settings.access_token_expire_days * 86400


def decode_token(token: str) -> dict[str, Any]:
    """解析 token，失败抛 40100。"""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise unauthorized("登录已过期，请重新登录") from None
    except jwt.PyJWTError:
        raise unauthorized("登录凭证无效") from None


def token_subject(payload: dict[str, Any]) -> int:
    sub = payload.get("sub")
    if sub is None:
        raise unauthorized()
    try:
        return int(sub)
    except (TypeError, ValueError):
        raise unauthorized() from None
