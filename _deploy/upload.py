"""把后端代码同步到云服务器。

## 为什么用 SFTP 而不是 git

项目还不是 git 仓库，也还没有远端。等以后接了仓库，这步换成 `git pull` 即可，
但 SFTP 这条路不依赖任何外部服务，随时能用。

## 排除规则

服务器上要重建的东西一律不传，否则会拿开发机的环境去污染服务器：

| 排除 | 原因 |
|---|---|
| `.venv/` | Windows 的虚拟环境到 Linux 上完全不可用，服务器上重建 |
| `.env` | 里面是**本机开发**的数据库密码，传上去会连错库 |
| `__pycache__/` | 编译产物，且跨平台不兼容 |
| `_t_*.py` | 调试用的临时探针脚本 |

`.env.example` 是**要**传的——服务器上的 `.env` 由部署脚本按它生成。

## 用法

    python _deploy/upload.py
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remote import HOST, USER, connect  # noqa: E402

LOCAL_ROOT = Path(__file__).resolve().parent.parent
SRC = LOCAL_ROOT / "backend"

EXCLUDE_DIRS = {
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".git", "node_modules", ".idea", ".vscode",
}
EXCLUDE_FILES = {".env", ".DS_Store", "Thumbs.db"}
EXCLUDE_GLOBS = ["_t_*.py", "*.pyc", "*.pyo", "*.log"]


def load_env() -> dict[str, str]:
    path = Path(__file__).resolve().parent / "deploy.env"
    if not path.exists():
        sys.exit("缺少 _deploy/deploy.env，请先跑 _deploy/setup_key.py 那一套准备工作。")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def excluded(name: str, is_dir: bool) -> bool:
    if is_dir:
        return name in EXCLUDE_DIRS
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS)


def collect() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if not excluded(d, True)]
        for f in files:
            if excluded(f, False):
                continue
            full = Path(root) / f
            items.append((full, full.relative_to(SRC).as_posix()))
    return sorted(items, key=lambda x: x[1])


def ensure_remote_dir(client, path: str) -> None:
    """建目录并交给 ubuntu 用户。/srv 只有 root 能写，所以这里必须用 sudo。"""
    cmd = (
        f"sudo mkdir -p {path} && "
        f"sudo chown -R {USER}:{USER} {posixpath.dirname(path)}"
    )
    _, stdout, stderr = client.exec_command(cmd, timeout=60)
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        sys.exit(f"创建远端目录失败：{stderr.read().decode('utf-8', 'replace')}")


def main() -> None:
    env = load_env()
    dest = posixpath.join(env["DEPLOY_PATH"], "backend")

    items = collect()
    total = sum(f.stat().st_size for f, _ in items)
    print(f"源目录   {SRC}")
    print(f"目标     {USER}@{HOST}:{dest}")
    print(f"待上传   {len(items)} 个文件，{total / 1024:.1f} KB\n")

    client = connect()
    try:
        ensure_remote_dir(client, dest)

        sftp = client.open_sftp()
        try:
            # 先建齐所有子目录（SFTP 不会自动建父目录）
            made = {dest}
            for _, rel in items:
                parent = posixpath.dirname(rel)
                if not parent:
                    continue
                acc = dest
                for seg in parent.split("/"):
                    acc = posixpath.join(acc, seg)
                    if acc not in made:
                        try:
                            sftp.stat(acc)
                        except FileNotFoundError:
                            sftp.mkdir(acc)
                        made.add(acc)

            for i, (full, rel) in enumerate(items, 1):
                sftp.put(str(full), posixpath.join(dest, rel))
                if i % 25 == 0 or i == len(items):
                    print(f"  已传 {i}/{len(items)}")
        finally:
            sftp.close()
    finally:
        client.close()

    print(f"\n✅ 上传完成：{len(items)} 个文件 → {dest}")
    print("   下一步：在服务器上建 venv 装依赖（_deploy/deploy.py）")


if __name__ == "__main__":
    main()
