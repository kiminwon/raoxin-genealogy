"""远程执行：把命令丢到云服务器上跑，把输出带回来。

## 认证方式

优先用密钥（`~/.ssh/id_ed25519_raoxin`）。密钥还没装好时，把密码放进
环境变量 `DEPLOY_PASS`，脚本会自动用密码登录。

**密码不落盘。** 这个文件里没有任何凭据，它只从环境变量读。

## 用法

    export DEPLOY_PASS='...'          # 仅首次（装公钥）需要
    python _deploy/remote.py 'uname -a'
    python _deploy/remote.py 'cmd1' 'cmd2' 'cmd3'

每条命令在服务器上独立执行（不共享 shell 状态），所以要写在一行里用 && 串联。

    python _deploy/remote.py 'cd /srv/x && git pull'

## 为什么不用 sshpass

Windows 上没有 sshpass，而 Git Bash 的 ssh 在非交互场景下无法喂密码。
paramiko 是唯一能可靠自动化的路子。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import paramiko
except ModuleNotFoundError:
    # 这台机器上装了三套解释器（WorkBuddy 托管的 3.13、envs/default 的 3.13.14、
    # 系统 PATH 里的 3.14），paramiko 只装在项目 venv 里。裸 `python` 撞上别的
    # 那套就会走到这里。裸的 ModuleNotFoundError 看不出该换哪条命令，所以拦住。
    sys.exit(
        "缺少 paramiko。_deploy 的脚本要用**项目虚拟环境**跑：\n"
        "  backend\\.venv\\Scripts\\python.exe _deploy\\deploy.py --config\n"
        "（venv 里还没装过的话：\n"
        "  backend\\.venv\\Scripts\\python.exe -m pip install -r _deploy/requirements.txt）\n"
    )

HOST = os.environ.get("DEPLOY_HOST", "150.158.176.220")
USER = os.environ.get("DEPLOY_USER", "ubuntu")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
PASSWORD = os.environ.get("DEPLOY_PASS") or None
KEY_FILE = Path.home() / ".ssh" / "id_ed25519_raoxin"

# `命令 <<<FILE 本地文件` —— 把本地文件内容喂给远端命令的 stdin
#
# 用 `<<<FILE` 而不是别的符号：最初用的是 `@@`，结果和 MySQL 的系统变量
# 写法（SELECT @@collation_database）撞车，把正常命令误判成了文件输入。
STDIN_MARK = " <<<FILE "


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # 首次连接没有 known_hosts 记录。开发机场景下接受即可；
    # 若在意中间人风险，可改为 warning policy 并手工核对指纹。
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if PASSWORD:
        client.connect(
            hostname=HOST, port=PORT, username=USER,
            password=PASSWORD, timeout=20, banner_timeout=20, auth_timeout=20,
        )
    else:
        if not KEY_FILE.exists():
            sys.exit(
                f"找不到密钥 {KEY_FILE}，也没有 DEPLOY_PASS。\n"
                f"先跑：DEPLOY_PASS='密码' python _deploy/setup_key.py"
            )
        client.connect(
            hostname=HOST, port=PORT, username=USER,
            key_filename=str(KEY_FILE), timeout=20,
        )
    return client


def run(client: paramiko.SSHClient, command: str, stdin_file: str | None = None,
        timeout: int | None = None) -> int:
    timeout = timeout or int(os.environ.get("DEPLOY_TIMEOUT", "900"))
    print(f"\n\033[36m$ {command}\033[0m")

    # 关键：不加 pipefail 的话，`apt install | tail` 的退出码是 tail 的 0，
    # 安装失败会被报成成功。这个坑真实踩过一次（MySQL 装挂却显示"全部成功"）。
    wrapped = f"set -o pipefail; {command}"
    stdin, stdout, stderr = client.exec_command(wrapped, timeout=timeout)

    if stdin_file:
        data = Path(stdin_file).read_bytes()
        print(f"\033[90m[stdin] {stdin_file} → {len(data)} 字节\033[0m")
        stdin.write(data)
        stdin.flush()

    # 用 shutdown_write 而不是 close()：close() 会直接断开通道，
    # 远端可能还没读完就报 broken pipe（导 34KB 的 schema 会踩到）。
    stdin.channel.shutdown_write()

    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()

    if out.strip():
        print(out.rstrip())
    if err.strip():
        # stderr 不一定是错误（很多工具把进度写这里），但必须可见
        print("\033[33m[stderr]\033[0m")
        print(err.rstrip())
    print(f"\033[{'32' if rc == 0 else '31'}m[exit {rc}]\033[0m")
    return rc


def main() -> None:
    commands = sys.argv[1:]
    if not commands:
        sys.exit(__doc__)

    client = connect()
    try:
        failures = 0
        for raw in commands:
            # 约定：`命令 <<<FILE 本地文件` 表示把该文件内容喂给远端命令的 stdin。
            # 导 SQL、传大段配置都靠它，避免把内容塞进命令行。
            #
            # ⚠️ 这个标记必须放在命令**末尾**：标记之后的内容全都会被当成文件路径，
            # 所以不能写成 `mysql db <<<FILE x.sql; echo done`（`; echo done` 会被
            # 当成路径的一部分）。要串联就写成两条命令。
            if STDIN_MARK in raw:
                cmd, local = raw.split(STDIN_MARK, 1)
                cmd = cmd.strip()
                # 路径允许加引号（路径含空格时必需）
                local = local.strip().strip('"').strip("'")
                if not Path(local).exists():
                    sys.exit(f"找不到待输入的文件：{local}")
                rc = run(client, cmd, stdin_file=local)
            else:
                rc = run(client, raw)
            if rc != 0:
                failures += 1
    finally:
        client.close()

    if failures:
        print(f"\n\033[31m{failures}/{len(commands)} 条命令返回非零\033[0m")
        sys.exit(1)
    print(f"\n\033[32m全部 {len(commands)} 条命令成功\033[0m")


if __name__ == "__main__":
    main()
