"""数据库引擎与会话。

注意：schema.sql 明确不建物理外键，一致性由应用层保证。
这里也不启用任何隐式外键级联。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,       # 断线自动重连，避免 MySQL 8 小时空闲断连
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
