"""核对 ORM 模型定义与数据库实际表结构是否一致。

为什么需要这个脚本：
    DDL（sql/schema.sql）和 ORM 模型（app/models/）是两份手写的定义，
    它们会漂移。漂移的后果分两种，都很隐蔽：
      - 模型多一列 → 所有 SELECT 报 Unknown column，接口直接 500
      - 模型少一列 → 该字段静默丢失，写入时数据无声无息没存进去
    第二种尤其危险，因为不报错。所以每次改表结构后都该跑一次。

用法：
    cd backend
    .venv\\Scripts\\python.exe scripts/check_schema.py
"""

from __future__ import annotations

import pathlib
import sys

import pymysql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  触发所有模型注册到 Base.metadata


def main() -> int:
    conn = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset=settings.db_charset,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s",
            (settings.db_name,),
        )
        db_cols: dict[str, set[str]] = {}
        for table, col in cur.fetchall():
            db_cols.setdefault(table, set()).add(col)
    conn.close()

    model_cols = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}

    problems = 0

    missing_table = sorted(set(model_cols) - set(db_cols))
    if missing_table:
        problems += len(missing_table)
        print("【表在模型里有、数据库里没有】")
        for t in missing_table:
            print(f"  ✗ {t}")

    extra_table = sorted(set(db_cols) - set(model_cols))
    if extra_table:
        print("【表在数据库里有、模型里没有】")
        for t in extra_table:
            print(f"  · {t}（可能是正常的辅助表）")

    print(f"\n共比对 {len(model_cols)} 张表：")
    for table in sorted(model_cols):
        if table not in db_cols:
            continue
        m, d = model_cols[table], db_cols[table]
        only_model = sorted(m - d)   # 会导致 Unknown column → 500
        only_db = sorted(d - m)      # 该字段静默丢失

        if not only_model and not only_db:
            print(f"  ✅ {table:<20} {len(m)} 列，一致")
            continue

        problems += len(only_model) + len(only_db)
        print(f"  ❌ {table}")
        for c in only_model:
            print(f"       模型有、库里没有：{c}   ← 会让查询直接 500")
        for c in only_db:
            print(f"       库有、模型没有：  {c}   ← 该字段会静默丢失")

    print()
    if problems:
        print(f"发现 {problems} 处不一致，建议修掉再继续。")
        return 1
    print("模型与数据库完全一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
