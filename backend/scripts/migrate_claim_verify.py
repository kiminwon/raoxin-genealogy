"""给 claim 表补 verify_method / verify_answers 两列。

用法：

    cd backend
    .venv\\Scripts\\python.exe scripts/migrate_claim_verify.py

为什么单独写一个迁移脚本而不直接重跑 init_db.py：
    init_db.py 走的是 schema.sql，里面每条 CREATE TABLE 前面都有 DROP TABLE——
    那是"重置"路径，会把演示数据和已认领的用户一起清掉。
    改表结构时用这个脚本，幂等，可以反复跑。

改完**必须**跑 `scripts/check_schema.py` 确认模型与表结构零漂移。
"""

from __future__ import annotations

import pathlib
import sys

import pymysql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

COLUMNS = [
    (
        "verify_method",
        "ALTER TABLE `claim` ADD COLUMN `verify_method` TINYINT NOT NULL DEFAULT 1 "
        "COMMENT '1答题自证 2房长审核 3邀请码' AFTER `evidence_files`",
    ),
    (
        "verify_answers",
        "ALTER TABLE `claim` ADD COLUMN `verify_answers` JSON NULL "
        "COMMENT '答案快照（含命中节点与证据强度），供申诉/追溯/审计' AFTER `verify_method`",
    ),
]


def main() -> None:
    conn = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset=settings.db_charset,
        autocommit=True,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'claim'",
            (settings.db_name,),
        )
        existing = {row[0] for row in cur.fetchall()}

        if not existing:
            print("claim 表不存在，请先跑 scripts/init_db.py")
            sys.exit(1)

        for name, ddl in COLUMNS:
            if name in existing:
                print(f"  [跳过] {name} 已存在")
                continue
            cur.execute(ddl)
            print(f"  [新增] {name}")

        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'claim' ORDER BY ORDINAL_POSITION",
            (settings.db_name,),
        )
        print("\nclaim 表当前结构：")
        for col, ctype, nullable, comment in cur.fetchall():
            print(f"  {col:<20} {ctype:<16} {'NULL' if nullable == 'YES' else 'NOT NULL':<9} {comment}")

    conn.close()
    print("\n下一步：.venv/Scripts/python.exe scripts/check_schema.py")


if __name__ == "__main__":
    main()
