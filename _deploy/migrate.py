"""在云服务器上执行一份增量 SQL（改表结构专用）。

## 为什么不并进 deploy.py

`deploy.py` 的注释里写死了一条边界：**部署不碰数据库**。
那条边界是有意留的 —— 配置或代码写错时重跑部署是安全的，
一旦它顺手改了表结构，「重跑一次」就变成有风险的动作了。
所以改表单独一条命令、单独看输出。

## 它做了什么

    1. 连服务器，确认 mysql 客户端可用
    2. mysqldump 一份整库到 <部署目录>/backups/（改表前先留退路）
    3. 上传 SQL 到 /tmp，用临时 my.cnf（600，密码不进命令行）执行
    4. SQL 里的 SELECT 会原样打印出来 —— 迁移脚本的自检查询就靠这个
    5. 删掉上传的临时文件

## 用法

    cd D:\\code\\raoxin-genealogy
    backend\\.venv\\Scripts\\python.exe _deploy\\migrate.py backend/sql/migrate_2026-10-17_user_family.sql

必须用项目 venv 的解释器跑（paramiko 只装在那里）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))

from remote import connect, run  # noqa: E402


def load_env() -> dict[str, str]:
    path = HERE / "deploy.env"
    if not path.exists():
        sys.exit(f"找不到 {path}")
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def sftp_put(client, local: Path, remote: str, mode: int = 0o600) -> None:
    sftp = client.open_sftp()
    try:
        sftp.put(str(local), remote)
        sftp.chmod(remote, mode)
    finally:
        sftp.close()
    print(f"\033[90m[put] {local.name} → {remote}（{local.stat().st_size} 字节，mode {oct(mode)}）\033[0m")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    sql_path = Path(sys.argv[1])
    if not sql_path.is_absolute():
        sql_path = (ROOT / sql_path).resolve()
    if not sql_path.exists():
        sys.exit(f"找不到 SQL 文件：{sql_path}")

    env = load_env()
    db_name = env.get("DB_NAME", "")
    db_user = env.get("DB_USER", "")
    db_pass = env.get("DB_PASSWORD", "")
    deploy_path = env.get("DEPLOY_PATH", "/srv/raoxin-genealogy")
    if not (db_name and db_user):
        sys.exit("deploy.env 里缺 DB_NAME / DB_USER")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    remote_sql = f"/tmp/raoxin_migrate_{stamp}.sql"
    remote_cnf = f"/tmp/raoxin_migrate_{stamp}.cnf"

    client = connect()
    try:
        # ---------- 0. 环境确认 ----------
        if run(client, "command -v mysql && mysql --version") != 0:
            sys.exit("远端没有 mysql 客户端，先 apt install mysql-client")

        # ---------- 1. 备份 ----------
        # my.cnf 走 SFTP 上传而不是写进命令行：密码不出现在 ps / shell 历史里
        cnf_local = HERE / ".gen" / f"tmp_{stamp}.cnf"
        cnf_local.parent.mkdir(parents=True, exist_ok=True)
        cnf_local.write_text(
            "[client]\n"
            "host=127.0.0.1\n"
            f"user={db_user}\n"
            f"password={db_password_escape(db_pass)}\n",
            encoding="utf-8",
            newline="\n",
        )
        try:
            sftp_put(client, cnf_local, remote_cnf)
        finally:
            cnf_local.unlink(missing_ok=True)

        backup = f"{deploy_path}/backups/{db_name}-{stamp}.sql.gz"
        rc = run(
            client,
            f"mkdir -p {deploy_path}/backups && "
            f"mysqldump --defaults-extra-file={remote_cnf} --single-transaction "
            f"--routines --triggers {db_name} | gzip > {backup} && "
            f"ls -lh {backup}",
        )
        if rc != 0:
            sys.exit("备份失败，已停住 —— 没有退路就不改表")

        # ---------- 2. 执行 ----------
        sftp_put(client, sql_path, remote_sql)
        rc = run(client, f"mysql --defaults-extra-file={remote_cnf} {db_name} < {remote_sql}")
        if rc != 0:
            sys.exit(f"迁移执行失败（rc={rc}）。备份在 {backup}，需要回滚时用它。")

        # ---------- 3. 复核 ----------
        run(
            client,
            f"mysql --defaults-extra-file={remote_cnf} {db_name} -e "
            f"'SHOW TABLES LIKE \"user_family\"; "
            f"SELECT COUNT(*) AS user_family_rows FROM user_family; "
            f"SELECT COUNT(*) AS users_with_family FROM app_user WHERE family_id IS NOT NULL;'",
        )
    finally:
        # 临时凭据一定要删，哪怕中途失败
        run(client, f"rm -f {remote_cnf} {remote_sql}")
        client.close()

    print(f"\n\033[1;32m迁移完成\033[0m  回滚可用：{backup}")


def db_password_escape(password: str) -> str:
    """my.cnf 里的值可以用双引号包，双引号内的 \\ 和 " 需要转义。"""
    return '"' + password.replace("\\", "\\\\").replace('"', '\\"') + '"'


if __name__ == "__main__":
    main()
