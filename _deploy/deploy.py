"""一键部署：把本地改动推到云服务器并重启。

## 它做了什么

    1. 渲染配置      gen_configs.py   → _deploy/.gen/
    2. 上传代码      upload.py        → /srv/raoxin-genealogy/backend
    3. 上传配置       .env / systemd unit / nginx 站点
    4. 重启并校验     raoxin-api、nginx
    5. 跑契约巡检     check_live.py 直接打公网地址

第 5 步是**唯一能发现"服务器跑着旧代码"的手段**——本机的冒烟测试用的是
进程内 app，测的是本地磁盘上的代码，跟服务器上跑什么没关系。

## 用法

    python _deploy/deploy.py                # 全量：代码 + 配置 + 重启 + 巡检
    python _deploy/deploy.py --code         # 只更新代码（改业务逻辑时的日常）
    python _deploy/deploy.py --config       # 只更新配置（改 .env / nginx / unit）
    python _deploy/deploy.py --verify       # 什么都不改，只巡检
    python _deploy/deploy.py --site         # 首次启用域名 + HTTPS（见下）

## --site 为什么要单独一个命令

因为证书是鸡生蛋：**开 443 需要证书，拿证书需要域名能通过 80 访问**。
`--site` 把这个循环按顺序走完，并且**每一步都要过了才走下一步**：

    1. 铺 SITE_TLS=off 的配置（只有 80，留出 ACME 验证目录）
    2. 从本机打 http://域名/health —— 这是 DNS / 备案 / 防火墙三件事的试金石
    3. 装 certbot
    4. certbot certonly --webroot 签证书（幂等：已有证书就跳过）
    5. 翻 SITE_TLS=on，铺带 443 的配置，reload，再从本机打 https 验一遍

第 5 步之后会把 `deploy.env` 里的 `SITE_TLS` 自动改成 `on`——不这么做的话，
下次跑 `--config` 会按 `off` 重新渲染，把线上的 443 覆盖掉，HTTPS 就没了。

## 部署不会碰的东西

数据库。`init_db.sql` 只建库建账号，不导表结构也不灌数据——那是两个明确
分开的动作（见 README）。这样部署脚本随时可以跑，不会把你的数据洗掉。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
GEN = HERE / ".gen"
BACKEND = ROOT / "backend"

sys.path.insert(0, str(HERE))

from remote import HOST, connect, run  # noqa: E402


# ============================================================
# 小工具
# ============================================================


def load_env() -> dict[str, str]:
    path = HERE / "deploy.env"
    if not path.exists():
        sys.exit("缺少 _deploy/deploy.env。先看 _deploy/README.md 的「首次部署」。")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def site_tls_on(env: dict[str, str]) -> bool:
    return env.get("SITE_TLS", "off").strip().lower() in ("on", "1", "true", "yes")


def public_base(env: dict[str, str]) -> str:
    """对外地址。证书就位后用域名，否则退回 IP。"""
    domain = env.get("SITE_DOMAIN", "").strip()
    if domain and site_tls_on(env):
        return f"https://{domain}"
    return f"http://{HOST}"


def local_venv_python() -> str | None:
    """本机 backend 虚拟环境的 python，用来跑契约巡检。"""
    for rel in ("Scripts/python.exe", "bin/python"):
        p = BACKEND / ".venv" / rel
        if p.exists():
            return str(p)
    return None


def step(title: str) -> None:
    print(f"\n\033[1;35m{'=' * 62}\n{title}\n{'=' * 62}\033[0m")


def put_remote(client, local: pathlib.Path, dest: str, *,
               use_sudo: bool = False, mode: str | None = None) -> None:
    """把本地文件写到服务器指定位置（内容走 stdin，避免命令行转义地狱）。"""
    data = local.read_bytes()
    print(f"  ↑ {local.name} → {dest}  ({len(data)} 字节)")

    cmd = f"sudo tee {dest} > /dev/null" if use_sudo else f"cat > {dest}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=90)
    stdin.write(data)
    stdin.flush()
    stdin.channel.shutdown_write()
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        sys.exit(f"  写入 {dest} 失败：{stderr.read().decode('utf-8', 'replace')}")

    if mode:
        run(client, f"sudo chmod {mode} {dest}")


def host_resolves(host: str) -> bool:
    """本机能不能解析这个域名。"""
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def probe(url: str, *, resolve: tuple[str, str] | None = None) -> tuple[bool, str]:
    """从本机（开发机）打一个 URL，走完整公网链路。

    刻意**不走代理**：这台机器的环境变量里有 http_proxy，httpx / urllib
    默认会读它，结果"服务器通不通"取决于本机代理进程活着没有——这个坑
    check_live.py 上已经踩过一次了。

    `resolve=(域名, IP)` 是本机 DNS 没跟上时的兜底，语义等价于
    `curl --resolve`：**只把底层 connect 的 IP 换掉**，URL 里的域名不变，
    于是 SNI 和证书校验用的仍然是真正的域名。为什么需要它：DNS 记录刚加上
    的那几十分钟，本机或它的上游递归往往还缓存着 NXDOMAIN——域名全球都通、
    就这台开发机不通，不兜底就会在 --site 的验证闸门处误报"DNS 没配好"。
    注意它只影响本机这一侧的探测，**Let's Encrypt 的验证走它自己的公网
    解析，跟这里无关**。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    orig_getaddrinfo = socket.getaddrinfo

    if resolve:
        host, ip = resolve

        def patched(hostname, *args, **kwargs):  # noqa: ANN001
            return orig_getaddrinfo(ip if hostname == host else hostname,
                                    *args, **kwargs)

        socket.getaddrinfo = patched  # type: ignore[assignment]
    try:
        with opener.open(url, timeout=15) as resp:
            return resp.status == 200, resp.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 —— 网络错误形态太多，统一展示
        return False, f"{type(e).__name__}: {e}"
    finally:
        socket.getaddrinfo = orig_getaddrinfo  # type: ignore[assignment]


def probe_domain(url: str, domain: str) -> tuple[bool, str]:
    """打域名，本机解析不出来时自动降级为「域名 + 强制 IP」。"""
    resolve = None
    if not host_resolves(domain):
        resolve = (domain, HOST)
        print(f"  注：本机暂时解析不出 {domain}（DNS 负缓存还没过期），"
              f"已按 {HOST} 直连探测，域名照旧参与证书校验。")
    return probe(url, resolve=resolve)


def flip_site_tls(on: bool) -> None:
    """把 deploy.env 的 SITE_TLS 改成 on/off。

    这是 `--site` 最后一步要做的收尾：让"当前是哪个阶段"这件事只存在一处，
    而不是靠人记住"我还没改那一行"。
    """
    path = HERE / "deploy.env"
    text = path.read_text(encoding="utf-8")
    value = "on" if on else "off"
    new = re.sub(r"^SITE_TLS=.*$", f"SITE_TLS={value}", text, count=1, flags=re.M)
    if new == text:
        print(f"  提示：deploy.env 里没找到 SITE_TLS 行，请手动设为 {value}")
        return
    path.write_text(new, encoding="utf-8")
    print(f"  ✓ 已把 deploy.env 的 SITE_TLS 改为 {value}（下次 --config 会跟着它走）")


# ============================================================
# 各阶段
# ============================================================


def render_configs(tls: str | None = None, title: str = "渲染配置") -> None:
    step(title)
    cmd = [sys.executable, str(HERE / "gen_configs.py")]
    if tls:
        cmd += ["--tls", tls]
    subprocess.run(cmd, check=True)


def push_code() -> None:
    step("上传后端代码")
    subprocess.run([sys.executable, str(HERE / "upload.py")], check=True)


def push_configs(client, env: dict[str, str]) -> None:
    step("上传配置（.env / systemd / nginx）")
    root = env["DEPLOY_PATH"]

    put_remote(client, GEN / "backend.env", f"{root}/backend/.env", mode="600")
    put_remote(client, GEN / "raoxin-api.service",
               "/etc/systemd/system/raoxin-api.service", use_sudo=True)
    put_remote(client, GEN / "nginx-raoxin.conf",
               "/etc/nginx/sites-available/raoxin", use_sudo=True)

    # 站点启用 + 干掉默认站点（默认站点占着 default_server，会抢走请求）
    rc = run(client, "sudo rm -f /etc/nginx/sites-enabled/default && "
                     "sudo ln -sf /etc/nginx/sites-available/raoxin "
                     "/etc/nginx/sites-enabled/raoxin && "
                     "sudo nginx -t")
    if rc != 0:
        # 停在这里，绝不往下走 reload。语法错的配置一旦 reload，nginx 会拒绝
        # 加载新配置（旧配置继续跑），但下次重启就起不来了 —— 那是个更糟的状态。
        sys.exit("  配置没通过 nginx -t，已停住（没有 reload，线上仍跑旧配置）。\n"
                 "  按上面的报错定位 _deploy/.gen/nginx-raoxin.conf。")


def restart(client) -> None:
    step("重启服务")
    run(client, "sudo systemctl daemon-reload")
    run(client, "sudo systemctl restart raoxin-api && sleep 4 && "
                "sudo systemctl is-active raoxin-api")
    run(client, "sudo systemctl reload nginx && echo 'nginx 已重载'")
    run(client, f"curl -s -m 10 http://127.0.0.1:8000/health")


def enable_site(client, env: dict[str, str]) -> None:
    """首次启用域名 + HTTPS。幂等，可以重复跑。"""
    domain = env.get("SITE_DOMAIN", "").strip()
    if not domain:
        sys.exit("deploy.env 里没填 SITE_DOMAIN，--site 无从下手。")
    email = env.get("CERT_EMAIL", "").strip()
    webroot = "/var/www/certbot"
    cert = f"/etc/letsencrypt/live/{domain}/fullchain.pem"

    # ---------- 1/5 ----------
    render_configs(tls="off", title="1/5 铺 HTTP 版配置（certbot 要靠它做验证）")
    push_configs(client, env)
    run(client, f"sudo mkdir -p {webroot} && "
                f"sudo chmod 755 {webroot} && "
                f"sudo chown -R www-data:www-data {webroot}")
    run(client, "sudo systemctl reload nginx && echo 'nginx 已重载'")

    # ---------- 2/5 ----------
    # 这一步是整个流程的**闸门**。DNS 没解析、腾讯云防火墙没开 80、
    # 或者备案接入商不是腾讯云（会被返回一个 HTML 备案提示页），
    # 都会在这里暴露出来，而不是拖到 certbot 报一堆看不懂的验证失败。
    step("2/5 从本机打域名，验证 DNS / 备案 / 防火墙")
    url = f"http://{domain}/health"
    ok, detail = probe_domain(url, domain)
    if not ok:
        print(f"""
\033[31m✗ {url} 不通（{detail}）\033[0m

先别急着申请证书，按这个顺序查 —— 每一步的症状都不一样：

  1. \033[1mDNS\033[0m：阿里云那边加了 api 的 A 记录指向 {HOST} 吗？
     本机核对：nslookup {domain}
     解析没生效 → 报「Name or service not known」

  2. \033[1m腾讯云防火墙\033[0m：改过轻量服务器的防火墙规则吗？
     80 是之前就放行的，443 也要一起放（下一步就要用）。
     没放行 → 报「Connection refused」或超时

  3. \033[1m备案\033[0m：备案是在腾讯云做的吗？
     如果接入商不是腾讯云，腾讯云会返回一个 HTML 备案提示页
     而不是你的后端 —— 那就得先在腾讯云做「新增接入备案」。
     症状：HTTP 200 但响应体是网页，或者被 302 到备案页

改完任何一项，重跑 `python _deploy/deploy.py --site` 即可（幂等）。
""")
        sys.exit(1)
    print(f"  ✓ {url} → {detail}")

    # ---------- 3/5 ----------
    step("3/5 装 certbot")
    run(client, "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq")
    run(client, "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y certbot")
    run(client, "certbot --version")

    # ---------- 4/5 ----------
    step("4/5 申请证书")
    if run(client, f"sudo test -f {cert}") == 0:
        print(f"  已有证书，跳过申请：{cert}")
    else:
        # --deploy-hook：certbot 会把这条 hook 写进续期配置，90 天后自动续期
        # 时执行。不加的话证书悄悄换了、nginx 还拿旧的，到期才发现。
        # 邮箱留空则用 --register-unsafely-without-email：续期失败时收不到通知，
        # 但自动续期本身照常跑。
        email_arg = (f"--email {email}" if email
                     else "--register-unsafely-without-email")
        run(client,
            f"sudo certbot certonly --webroot -w {webroot} -d {domain} "
            f"{email_arg} --agree-tos --non-interactive "
            f"--deploy-hook 'systemctl reload nginx'")
        run(client, f"sudo certbot certificates -d {domain}")

    # ---------- 5/5 ----------
    step("5/5 切到 HTTPS 配置")
    flip_site_tls(True)
    render_configs(tls="on", title="  渲染 SITE_TLS=on")
    push_configs(client, env)
    run(client, "sudo nginx -t && sudo systemctl reload nginx && echo 'nginx 已重载'")

    url = f"https://{domain}/health"
    ok, detail = probe_domain(url, domain)
    if not ok:
        print(f"\n\033[31m✗ {url} 不通（{detail}）\033[0m")
        print("  证书在，但 HTTPS 没起来。在服务器上查：")
        print("    sudo nginx -t")
        print("    sudo tail -30 /var/log/nginx/raoxin.error.log")
        print("  常见原因：443 没在腾讯云防火墙放行。")
        sys.exit(1)
    print(f"  ✓ {url} → {detail}")

    print(f"""
\033[1;32mHTTPS 就绪。\033[0m 还差两件人为操作：

  1. \033[1m微信小程序后台\033[0m → 开发管理 → 开发设置 → 服务器域名
     在 \033[1mrequest 合法域名\033[0m里加上：\033[1mhttps://{domain}\033[0m
     （不加的话真机上照样报 url not in domain list）

  2. \033[1mminiprogram/utils/env.js\033[0m 的 cloud 地址改成
     \033[1mhttps://{domain}/v1\033[0m

续期是自动的：apt 装的 certbot 会带一个 systemd timer，每天跑两次
`certbot renew`，续完自动 reload nginx。可以自己验一下：
    sudo systemctl list-timers certbot.timer
""")


def verify(env: dict[str, str]) -> int:
    step("契约巡检（直接打公网地址）")
    base = public_base(env)
    domain = env.get("SITE_DOMAIN", "").strip()

    # 本机还缓存着 NXDOMAIN 时，https://域名 从这台开发机打不通——但那是本机
    # 递归缓存的问题，不是服务器的问题（域名 + 证书这一层，enable_site 里的
    # probe_domain 已经单独验过了）。退回 IP 巡检：打的是同一个后端，
    # 「服务在跑的是不是最新代码」这个结论完全一样。
    if domain and domain in base and not host_resolves(domain):
        base = f"http://{HOST}"
        print(f"  注：本机暂时解析不出 {domain}（DNS 负缓存还没过期），"
              f"退回 IP 巡检。缓存过期后重跑 `--verify` 即可打域名。")

    py = local_venv_python()
    if not py:
        print("  跳过：本机没找到 backend/.venv。")
        print(f"  可手动跑：cd backend && python scripts/check_live.py {base}")
        return 0

    print(f"  目标：{base}")
    proc = subprocess.run(
        [py, str(BACKEND / "scripts" / "check_live.py"), base],
        cwd=str(BACKEND),
    )
    return proc.returncode


# ============================================================


def main() -> None:
    # 让本脚本的 print 立即落盘。否则 subprocess（check_live）的输出会整段插到
    # 前面去——Python 的 print 在管道里是块缓冲，而子进程直接写 fd，报告顺序会乱。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="部署到云服务器")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--code", action="store_true", help="只更新代码并重启")
    g.add_argument("--config", action="store_true", help="只更新配置并重启")
    g.add_argument("--verify", action="store_true", help="只巡检，不做任何改动")
    g.add_argument("--site", action="store_true",
                   help="首次启用域名 + HTTPS（DNS 必须先配好）")
    args = ap.parse_args()

    env = load_env()

    if args.verify:
        sys.exit(verify(env))

    client = connect()
    try:
        if args.site:
            # 不调 restart()：--site 只动 nginx，后端进程不用重启
            enable_site(client, env)
        else:
            if args.config:
                render_configs()
                push_configs(client, env)
            elif args.code:
                push_code()
            else:
                render_configs()
                push_code()
                push_configs(client, env)
            restart(client)
    finally:
        client.close()

    # 重新读一次：--site 可能刚把 SITE_TLS 翻成 on，这里要跟着换地址
    env = load_env()
    rc = verify(env)
    print(f"\n\033[1;32m部署完成\033[0m  小程序后端：{public_base(env)}/v1")
    sys.exit(rc)


if __name__ == "__main__":
    main()
