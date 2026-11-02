"""生成服务器要用的配置文件（本地渲染，再上传）。

生成到 `_deploy/.gen/`（已在 .gitignore 中排除，因为含数据库密码）：

| 文件 | 去处 |
|---|---|
| `backend.env` | `/srv/raoxin-genealogy/backend/.env` |
| `raoxin-api.service` | `/etc/systemd/system/raoxin-api.service` |
| `nginx-raoxin.conf` | `/etc/nginx/sites-available/raoxin` |
| `init_db.sql` | 用 root 执行一次（建库建账号，幂等） |

## 为什么要渲染而不是直接写死在项目里

数据库密码和 JWT 密钥来自 `deploy.env`，它们**必须**与开发环境不同，
也不该出现在任何提交进版本库的文件里。渲染出来的产物只存在于本机 .gen/。

## nginx 为什么分「无 TLS / 有 TLS」两版

因为证书这件事是鸡生蛋：**开 443 需要证书，拿证书又需要域名能通过 80 访问**
（Let's Encrypt 要从公网取一个随机文件来核对域名所有权）。

所以：

- `SITE_TLS=off` —— 只生成 80 的域名 server，直接反代，并留出
  `/.well-known/acme-challenge/` 给 certbot 写验证文件
- `SITE_TLS=on`  —— 再加上 443 的 server，并把 80 改成 301 跳转

`deploy.py --site` 会自动走完「off → 申请证书 → on」这套流程，跑完把
`deploy.env` 里的开关翻成 `on`。

## 用法

    python _deploy/gen_configs.py            # 按 deploy.env 里的 SITE_TLS
    python _deploy/gen_configs.py --tls off  # 强制生成无 TLS 版（--site 第一步用）
"""

from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
GEN = HERE / ".gen"

WEBROOT = "/var/www/certbot"


def load_env() -> dict[str, str]:
    path = HERE / "deploy.env"
    if not path.exists():
        sys.exit("缺少 _deploy/deploy.env")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


BACKEND_ENV = """\
# ============================================================
# 饶信谱脉后端环境变量（生产 / 云服务器）
# ------------------------------------------------------------
# 本文件由 _deploy/gen_configs.py 自动生成，不要手工编辑：
# 下次部署会覆盖。要改配置请改本地 _deploy/deploy.env。
# ============================================================

# ---------- 应用 ----------
APP_ENV=prod
DEBUG=false

# JWT 签名密钥。与开发环境**不同**，泄露等于任何人都能伪造登录令牌。
SECRET_KEY={SECRET_KEY}
ACCESS_TOKEN_EXPIRE_DAYS=7

# ---------- MySQL ----------
# 只连本机，数据库不对外暴露（已确认 3306 在腾讯云防火墙上不通）
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER={DB_USER}
DB_PASSWORD={DB_PASSWORD}
DB_NAME={DB_NAME}
DB_CHARSET=utf8mb4

# ---------- 微信小程序 ----------
WX_APPID={WX_APPID}
WX_SECRET={WX_SECRET}

# ⚠️ 开发期模拟登录。开着它，任何知道演示 openid 的人都能冒充该用户。
# 小程序已创建、AppID 已就位，但还没有 Secret，所以暂时保持 true。
# 拿到 Secret 并填进上面那行后，必须改成 false。
WX_MOCK_LOGIN={WX_MOCK_LOGIN}
"""

SYSTEMD_UNIT = """\
[Unit]
Description=饶信谱脉 后端 API（FastAPI / uvicorn）
Documentation=file://__ROOT__/README.md
After=network-online.target mysql.service
Wants=network-online.target
Requires=mysql.service

[Service]
Type=simple
User={USER}
Group={USER}
WorkingDirectory=__ROOT__/backend

# 单 worker 是**有意为之**，不是省事：
# 答题自证的限速桶是进程内的 dict，多 worker 会各记一份，
# 攻击者只要轮询就能绕过限速。等以后接了 Redis 再谈横向扩展。
ExecStart=__ROOT__/backend/.venv/bin/python -m uvicorn app.main:app \\
    --host 127.0.0.1 --port 8000 \\
    --workers 1 \\
    --proxy-headers --forwarded-allow-ips=127.0.0.1 \\
    --log-level info

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=raoxin-api

# ---------- 加固 ----------
# 服务只读代码、只在 /srv 下写；即使应用被攻破也难以上升到系统
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
"""

# ============================================================
# nginx
# ============================================================

# 反代 + 日志，三个 server 块共用。抽出来是为了避免同一段配置抄三遍
# ——抄三遍就意味着以后改超时时间要记得改三处。
PROXY_BLOCK = """\
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 家谱树的布局是服务端算的，大族首次渲染可能偏慢
        proxy_connect_timeout 10s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;

        # 长连接复用，省掉每次请求的握手
        proxy_set_header Connection "";
    }

    # 健康检查不进访问日志，否则日志会被心跳刷满
    location = /health {
        access_log off;
        proxy_pass http://127.0.0.1:8000;
    }

    access_log /var/log/nginx/raoxin.access.log;
    error_log  /var/log/nginx/raoxin.error.log;
"""

ACME_LOCATION = f"""\
    # certbot 用 webroot 模式验证域名所有权：它把随机文件写进这个目录，
    # Let's Encrypt 从公网取回来核对。
    # ⚠️ 不管有没有证书，这个 location 都要留着 —— 90 天后的自动续期还靠它。
    # 选 webroot 而不是 standalone：续期时不用停 nginx，服务零中断。
    location /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type "text/plain";
    }}
"""

TLS_BLOCK = """\
server {
    # ③ 域名的正式入口。
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name __DOMAIN__;

    ssl_certificate         /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key     /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
    ssl_trusted_certificate /etc/letsencrypt/live/__DOMAIN__/chain.pem;

    # 微信小程序基础库早就支持 TLS 1.2，1.0/1.1 已无必要且不安全，直接关掉
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

    # 刻意不加 HSTS：这是纯 API 域名，防降级的收益有限；而一旦写进浏览器
    # 就是 max-age 那么久，将来想用 http 调试会很别扭。

    client_max_body_size 10m;

__PROXY__
}
"""


def render_nginx(domain: str, tls: bool) -> str:
    """生成 nginx 站点配置。

    三个 server 块各管一件事：
      ① 80 default_server —— IP 直连，保留开发联调能力
      ② 80 域名          —— ACME 验证；有证书后变成 301 跳转
      ③ 443 域名         —— 正式入口（仅 tls=True 时生成）
    """
    head = """\
# ============================================================
# 饶信谱脉 · nginx 站点
# ------------------------------------------------------------
# 由 _deploy/gen_configs.py 生成，**不要手工编辑**：下次部署会覆盖。
# 要改配置请改本地 _deploy/deploy.env。
# ============================================================
"""

    # ① IP 直连。开发阶段 PC 上打 http://<IP>/v1 就靠它。
    #   default_server 表示"Host 不匹配任何其它 server 块时落到这里"，
    #   同一个端口只能有一个 default_server。
    ip_server = """\

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 10m;

__PROXY__
}
"""

    if not domain:
        return head + ip_server.replace("__PROXY__", PROXY_BLOCK.rstrip())

    # ② 域名的 80 端口
    if tls:
        http_root = """\
    # 有证书了：80 只留 ACME 验证，其余一律跳 HTTPS。
    # 微信要求 request 域名必须是 HTTPS，所以这里不存在"用 http 访问"的正常路径。
    location / {
        return 301 https://$host$request_uri;
    }
"""
    else:
        http_root = """\
    # 证书还没申请下来，这个阶段 80 直接反代 —— 一是让 certbot 的验证
    # 一定能过，二是能借 http://域名/health 立刻验证 DNS、备案、防火墙
    # 这三件事是否都通了（这是申请证书前唯一的试金石）。
__PROXY__
"""

    domain_http = """\

server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN__;

    client_max_body_size 10m;

__ACME__

__ROOT__
}
"""

    out = head + ip_server + domain_http
    if tls:
        out += "\n" + TLS_BLOCK

    # 统一在最后替换。顺序有讲究：__ROOT__ 展开出来的内容里可能还带着
    # __PROXY__（无证书版就是这样），所以 __PROXY__ 必须排在 __ROOT__ 之后。
    return (out
            .replace("__ROOT__", http_root.rstrip())
            .replace("__ACME__", ACME_LOCATION.rstrip())
            .replace("__DOMAIN__", domain)
            .replace("__PROXY__", PROXY_BLOCK.rstrip()))


# ============================================================


def main() -> None:
    ap = argparse.ArgumentParser(description="渲染服务器配置")
    ap.add_argument("--tls", choices=["on", "off"],
                    help="覆盖 deploy.env 里的 SITE_TLS（--site 分两步跑时会用到）")
    args = ap.parse_args()

    env = load_env()
    GEN.mkdir(exist_ok=True)
    root = env["DEPLOY_PATH"]

    domain = env.get("SITE_DOMAIN", "").strip()
    if args.tls is not None:
        tls = args.tls == "on"
    else:
        tls = env.get("SITE_TLS", "off").strip().lower() in ("on", "1", "true", "yes")

    # ⚠️ newline="\n" 不能省：Windows 上 write_text 会把 \n 翻成 \r\n，
    # 生成给 Linux 用的 .env / unit / conf 里就带着 \r。
    # .env 侥幸没事（python-dotenv 会剥掉），但被 shell `source` 时会报
    # `$'\r': command not found`——2026-10-16 已经在服务器上见过一次。
    (GEN / "backend.env").write_text(
        BACKEND_ENV.format(**env), encoding="utf-8", newline="\n"
    )
    (GEN / "raoxin-api.service").write_text(
        SYSTEMD_UNIT.replace("__ROOT__", root).format(USER=env["DEPLOY_USER"]),
        encoding="utf-8",
        newline="\n",
    )
    (GEN / "nginx-raoxin.conf").write_text(
        render_nginx(domain, tls), encoding="utf-8", newline="\n"
    )

    init_sql = f"""-- 建库 + 建应用账号（幂等，可重复执行）
CREATE DATABASE IF NOT EXISTS `{env['DB_NAME']}`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS '{env['DB_USER']}'@'localhost' IDENTIFIED BY '{env['DB_PASSWORD']}';
ALTER USER '{env['DB_USER']}'@'localhost' IDENTIFIED BY '{env['DB_PASSWORD']}';
GRANT ALL PRIVILEGES ON `{env['DB_NAME']}`.* TO '{env['DB_USER']}'@'localhost';
FLUSH PRIVILEGES;
"""
    (GEN / "init_db.sql").write_text(init_sql, encoding="utf-8", newline="\n")

    for f in sorted(GEN.iterdir()):
        print(f"  {f.name:24} {f.stat().st_size:>6} 字节")

    print(f"\n  域名：{domain or '（未配，只用 IP）'}")
    print(f"  TLS ：{'on  → 80 跳转 + 443' if tls else 'off → 只有 80'}")
    print(f"\n✅ 已生成到 {GEN}")


if __name__ == "__main__":
    main()
