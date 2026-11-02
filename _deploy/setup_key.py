"""一次性：把本机公钥装到云服务器上，之后部署就不用再传密码。

## 为什么值得先做这一步

后面还有几十条命令要往服务器上跑（装依赖、建库、传代码、起服务）。
每次都在命令行里带密码，既容易出错，也容易在日志/历史里留下痕迹。
装一次公钥，后面全部走密钥。

## 用法

    export DEPLOY_PASS='密码'
    python _deploy/setup_key.py

脚本是幂等的：公钥已存在就跳过，不会重复追加。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("DEPLOY_HOST", "150.158.176.220")
USER = os.environ.get("DEPLOY_USER", "ubuntu")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
PASSWORD = os.environ.get("DEPLOY_PASS")

KEY = Path.home() / ".ssh" / "id_ed25519_raoxin"
PUB = KEY.with_suffix(".pub")

# 必须用 Windows 原生的 ssh-keygen：Git Bash 那个是 MSYS 版，
# 从 Windows Python 的 subprocess 调用会因为路径格式不通而 FileNotFoundError。
SSH_KEYGEN = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh-keygen.exe"


def ensure_keypair() -> str:
    """本机没有密钥就生成一对。返回公钥内容。"""
    KEY.parent.mkdir(parents=True, exist_ok=True)

    if not KEY.exists():
        print(f"生成密钥对：{KEY}")
        if not SSH_KEYGEN.exists():
            sys.exit(f"找不到 ssh-keygen：{SSH_KEYGEN}")
        proc = subprocess.run(
            [str(SSH_KEYGEN), "-t", "ed25519", "-f", str(KEY), "-N", "",
             "-C", f"raoxin-deploy@{os.environ.get('COMPUTERNAME', 'dev')}"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.exit(f"生成密钥失败：{proc.stderr or proc.stdout}")
    else:
        print(f"复用已有密钥：{KEY}")

    return PUB.read_text(encoding="utf-8").strip()


def install(pubkey: str) -> None:
    if not PASSWORD:
        sys.exit("需要 DEPLOY_PASS 环境变量（首次装公钥必须用密码）。")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"连接 {USER}@{HOST}:{PORT} …")
    client.connect(hostname=HOST, port=PORT, username=USER, password=PASSWORD,
                   timeout=20, banner_timeout=20, auth_timeout=20)

    try:
        # 用 repr 包一层，避免公钥里的特殊字符被 shell 解释
        script = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"grep -qxF {shell_quote(pubkey)} ~/.ssh/authorized_keys "
            f"|| echo {shell_quote(pubkey)} >> ~/.ssh/authorized_keys"
        )
        _, stdout, stderr = client.exec_command(script, timeout=30)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        if rc != 0:
            sys.exit(f"写入 authorized_keys 失败（exit {rc}）：{err or out}")

        _, stdout, _ = client.exec_command(
            "wc -l < ~/.ssh/authorized_keys && hostname && (lsb_release -ds 2>/dev/null || cat /etc/os-release | head -2)",
            timeout=20,
        )
        print("服务器反馈：")
        print(stdout.read().decode("utf-8", "replace").strip())
    finally:
        client.close()


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main() -> None:
    pubkey = ensure_keypair()
    print(f"公钥指纹：{pubkey[:60]}…")
    install(pubkey)
    print("\n✅ 公钥已装好。验证密钥登录：")
    print(f"   ssh -i {KEY} {USER}@{HOST}")


if __name__ == "__main__":
    main()
