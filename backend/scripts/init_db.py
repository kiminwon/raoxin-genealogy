"""建库建表。

用法（确保 MySQL 已启动，且 .env 里的连接信息正确）：

    cd backend
    .venv\\Scripts\\python.exe scripts/init_db.py

脚本会：
  1. CREATE DATABASE IF NOT EXISTS（字符集 utf8mb4）
  2. 执行 sql/schema.sql（含 DROP TABLE，可反复运行做重置）
"""

from __future__ import annotations

import pathlib
import sys

import pymysql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

SQL_FILE = pathlib.Path(__file__).resolve().parents[1] / "sql" / "schema.sql"


def split_sql(text: str) -> list[str]:
    """按分号切句。先剔除整行 `--` 注释，避免注释里的分号造成误切。"""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        kept.append(line)
    body = "\n".join(kept)
    return [s.strip() for s in body.split(";") if s.strip()]


def main() -> None:
    print(f"连接 MySQL {settings.db_host}:{settings.db_port} 用户 {settings.db_user}")

    conn = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        charset=settings.db_charset,
        autocommit=True,
    )

    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{settings.db_name}` "
            f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        )
    conn.select_db(settings.db_name)
    print(f"数据库 `{settings.db_name}` 就绪")

    if not SQL_FILE.exists():
        print(f"找不到 {SQL_FILE}")
        sys.exit(1)

    statements = split_sql(SQL_FILE.read_text(encoding="utf-8"))
    print(f"待执行语句：{len(statements)} 条")

    created: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for i, stmt in enumerate(statements, 1):
            try:
                cur.execute(stmt)
            except pymysql.MySQLError as exc:
                head = stmt.strip().splitlines()[0][:70]
                print(f"  [{i}] 执行失败：{head}")
                print(f"      {exc}")
                raise
            low = stmt.lower()
            if low.startswith("create table"):
                name = stmt.split("`")[1] if "`" in stmt else "?"
                created.append(name)
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        cur.execute("SHOW TABLES")
        tables = [row[0] for row in cur.fetchall()]

    conn.close()

    print(f"\n建表完成，共 {len(tables)} 张：")
    for t in tables:
        print(f"  - {t}")
    print("\n下一步：python scripts/seed_demo.py  （灌入演示数据）")


if __name__ == "__main__":
    main()
