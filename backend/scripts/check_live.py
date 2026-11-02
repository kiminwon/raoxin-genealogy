"""运行中服务的契约巡检：直接打真实 HTTP 端口，确认服务在跑最新代码。

## 为什么需要这个脚本

smoke_e2e.py 用的是 ASGITransport（进程内直调），好处是快、不用起服务，
但它有个致命盲区：**它验证的是"磁盘上的代码"，不是"跑着的服务"**。

uvicorn 不带 --reload 启动后，磁盘上改了代码、测试全绿，而对外服务的
仍是旧代码。这次就真实踩过一次：`w`/`h` 字段的修复在源码里、smoke 也过，
但线上响应里根本没有 w/h —— 前端拿到 undefined，所有连线消失、锚点偏位，
而 HTTP 状态码是 200，非常难查。

更隐蔽的是**加了 --reload 也可能失效**：uvicorn 的 WatchFiles 监听线程会
静默死掉（日志里既没有报错、也不再打印 Reloading），进程活着、HTTP 200、
旧路由照样应答。这类故障在源码层面完全看不出来，只有拿"跑着的服务"去比
"磁盘上的源码"才暴露。

所以这个脚本做两件事：
1. 路由表比对 —— 把运行中服务的 /openapi.json 与磁盘源码导入出的 app 路由
   逐条对比。源码有、服务没有 = 服务陈旧；服务有、源码没有 = 跑了已删的代码。
   这是唯一能自动发现"陈旧服务"的手段。
2. 关键契约断言 —— 对真实端口校验响应字段（含 w/h）、脱敏规则、参数校验。

## 用法

    cd backend
    .venv\\Scripts\\python.exe scripts/check_live.py            # 默认 127.0.0.1:8000
    .venv\\Scripts\\python.exe scripts/check_live.py http://127.0.0.1:8001

只读，不改任何数据，可随时重复跑。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"

VIEW_CODE = "CHEN2026"

# 节点必备字段。x/y 定位，w/h 决定连线的锚点与终点——
# 少了 w/h，端上算不出锚点，连线会整片消失。
REQUIRED_NODE_FIELDS = {"id", "x", "y", "w", "h", "gen", "gender", "role", "living"}

# FastAPI 自带的路由，不在 /openapi.json 里，比对时剔除
FRAMEWORK_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

# 这两类方法在 openapi 里时有时无，不参与比对（避免假失败）
IGNORED_METHODS = {"HEAD", "OPTIONS", "PARAMETERS"}

Route = tuple[str, str]  # (path, METHOD)


def disk_routes() -> set[Route]:
    """从磁盘源码导入 app.main，取出全部 (路径, 方法)。

    注意：这是"源码认为该有哪些路由"，与实际跑着的服务无关。
    config.py 的 env_file=".env" 是相对 cwd 的，所以导入前先切到 backend 根。
    """
    backend = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(backend))
    cwd = os.getcwd()
    os.chdir(backend)
    try:
        from app.main import app  # noqa: PLC0415  延迟导入：脚本其余部分不需要它
    finally:
        os.chdir(cwd)

    # 副作用：app.main 里的 logging.basicConfig(level=DEBUG) 会打开全局 DEBUG，
    # httpx/httpcore 随即把每个字节都打出来，把巡检报告淹掉。这里压回去——
    # 巡检报告是给人看的，不是给日志看的。
    logging.getLogger().setLevel(logging.WARNING)

    out: set[Route] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or path in FRAMEWORK_PATHS or not methods:
            continue
        for m in methods:
            m = m.upper()
            if m in IGNORED_METHODS:
                continue
            out.add((path, m))
    return out


def live_routes(client: httpx.Client) -> set[Route]:
    """运行中服务自报的路由表。"""
    doc = get(client, "/openapi.json")
    out: set[Route] = set()
    for path, ops in (doc.get("paths") or {}).items():
        for m in ops:
            m = m.upper()
            if m in IGNORED_METHODS:
                continue
            out.add((path, m))
    return out


def _fmt(routes: set[Route], limit: int = 12) -> str:
    items = sorted(f"{m} {p}" for p, m in routes)
    head = items[:limit]
    tail = f" …（共 {len(items)} 条）" if len(items) > limit else ""
    return "\n           ".join(head) + tail


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def ok(self, cond: bool, label: str, hint: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            self.failed += 1
            self.failures.append(label)
            print(f"  [FAIL] {label}")
            if hint:
                print(f"         -> {hint}")
        return cond


def get(client: httpx.Client, path: str) -> dict:
    r = client.get(BASE + path)
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"_status": r.status_code, "_text": r.text[:200]}


def main() -> int:
    rep = Report()
    print(f"巡检目标：{BASE}\n")

    # trust_env=False：**不要**读本机的 http_proxy / HTTPS_PROXY。
    #
    # 踩过一次：开发机上常驻代理软件，环境变量里带着 http_proxy，httpx 默认会用它。
    # 于是这个脚本的成败取决于「代理进程当时活着没有」——
    #   代理活着 → 请求绕道代理走，你看到的是代理的行为，不是真实链路
    #   代理一关 → 报「连接被积极拒绝」，看着像服务器挂了
    # 自检脚本必须看真实链路，所以显式关掉环境变量代理。
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        # ---------- 服务可达 ----------
        print("【1】服务可达")
        try:
            health = get(client, "/health")
        except httpx.HTTPError as exc:
            print(f"\n无法连接 {BASE}：{exc}")
            print("按这个顺序排查：")
            print("  1) 本机代理：环境变量里的 http_proxy 会接管请求，代理一关就报"
                  "「连接被积极拒绝」，看着像服务挂了")
            print("  2) 本机后端没起：cd backend && .venv\\Scripts\\python.exe -m uvicorn "
                  "app.main:app --host 127.0.0.1 --port 8000 --reload")
            print("  3) 云端没起：ssh 上去 sudo systemctl status raoxin-api")
            return 1
        rep.ok(health.get("code") == 0, "健康检查返回 code=0")
        rep.ok(bool(health.get("data", {}).get("version")), "健康检查带版本号")

        # ---------- 身份安全：dev: 后门 ----------
        #
        # `code="dev:xxx"` 是开发期的后门，只有后端 WX_MOCK_LOGIN=true 时才认。
        # 线上如果还认，**任何人知道演示 openid 就能冒充那个族人**——而且这种事故
        # 不会有任何报错，只会表现为"一切正常"。所以在这里钉一条断言。
        #
        # 期望值随目标变：本机后端开着 mock 是对的，云端必须关着。
        is_local = any(h in BASE for h in ("127.0.0.1", "localhost"))
        print("\n【1.5】身份安全（dev: 后门）")
        try:
            probe = client.post(BASE + "/v1/auth/login",
                                json={"code": "dev:probe_check_live"}).json()
        except Exception as exc:  # noqa: BLE001
            probe = {"code": -1, "msg": str(exc)}
        dev_accepted = probe.get("code") == 0
        rep.ok(
            dev_accepted == is_local,
            f"dev: 后门状态正确（本机应开 / 线上应关，实际{'开' if dev_accepted else '关'}）",
            "云端还认 dev: 前缀 —— 把服务器 .env 的 WX_MOCK_LOGIN 改成 false 并重启。"
            "否则知道演示 openid 的人可以冒充该族人。",
        )

        # ---------- 陈旧服务检测 ----------
        print("\n【2】运行中服务 = 磁盘源码（陈旧服务检测）")
        try:
            want = disk_routes()
        except Exception as exc:  # 源码都导不进来，先别慌，报出来
            rep.ok(False, "磁盘源码可导入（app.main:app）", f"{type(exc).__name__}: {exc}")
            want = set()
        got = live_routes(client)

        if want:
            missing = want - got          # 源码有 → 服务没有：服务在跑旧代码
            extra = got - want            # 服务有 → 源码没有：服务在跑已删的代码
            rep.ok(
                not missing,
                f"服务覆盖源码全部路由（源码 {len(want)} / 服务 {len(got)}）",
                "以下路由源码里有、跑着的服务没有响应，说明进程是旧的。"
                "重启：先 taskkill 掉旧 uvicorn，再 --reload 启动。\n"
                f"           {_fmt(missing)}",
            )
            rep.ok(
                not extra,
                "服务没有源码里已删除的路由",
                "服务自报的路由源码里找不到，八成跑着旧代码或改错了注册处。\n"
                f"           {_fmt(extra)}",
            )
            if not missing and not extra:
                print(f"         服务自报 {len(got)} 条路由，与源码逐条一致")

            # 本次改造的入口，单独点名，别被"整体一致"掩盖
            for path in (
                "/v1/auth/identify/quiz",
                "/v1/auth/identify/verify",
                "/v1/family/list",
                "/v1/family/switch",
            ):
                rep.ok(
                    any(p == path for p, _ in got),
                    f"关键入口已上线：{path}",
                    "没有这条说明服务陈旧，小程序调过去会 404。",
                )

        # ---------- 病毒入口 ----------
        print("\n【3】家族浏览码（冷启动的核心入口）")
        prev = get(client, f"/v1/invite/{VIEW_CODE}")
        rep.ok(prev.get("code") == 0, f"浏览码 {VIEW_CODE} 可解析",
               "读的是 family.view_code 字段，不是 invite_code 表——两处存储")
        fam = (prev.get("data") or {}).get("family") or {}
        rep.ok(bool(fam.get("name")), "带出家族名")
        rep.ok((fam.get("personCount") or 0) > 0, "带出人数")

        # ---------- 多族 ----------
        #
        # 一个账号可以同属父系谱和母系谱：/family/list 列出全部，/family/switch 换当前族。
        # 未登录必须被拒 —— 族列表是私人数据，匿名能拉就等于告诉别人"这个 openid 在哪些族里"。
        print("\n【3.5】多族（family/list · family/switch）")
        anon = get(client, "/v1/family/list")
        rep.ok(
            anon.get("code") == 40100,
            "family/list 未登录返回 40100",
            f"实际 code={anon.get('code')} —— 族列表不能匿名拉",
        )

        # 登录态断言只在 dev 后门开着时跑。云端没有可用的测试身份，
        # 也不该为了巡检去造账号、动线上数据。
        if dev_accepted:
            login_doc = client.post(
                BASE + "/v1/auth/login", json={"code": "dev:mock_qiming"}
            ).json()
            tok = ((login_doc.get("data") or {}).get("token")) or ""
            if tok:
                hdr = {"Authorization": "Bearer " + tok}
                fl = client.get(BASE + "/v1/family/list", headers=hdr).json()
                d = fl.get("data") or {}
                items = d.get("families") or []
                rep.ok(fl.get("code") == 0, "family/list 登录后返回成功",
                       f"msg={fl.get('msg')}")
                rep.ok(isinstance(items, list), f"families 是数组（{len(items)} 个族）")
                cur = [x for x in items if x.get("current")]
                rep.ok(
                    len(cur) <= 1,
                    "最多一个族标记为 current",
                    f"有 {len(cur)} 个 —— 切换/回填逻辑有问题",
                )
                rep.ok(
                    not cur or d.get("currentFamilyId") == cur[0].get("familyId"),
                    "currentFamilyId 与列表里 current 的族一致",
                )
            else:
                print("  [SKIP] 拿不到 dev token，跳过登录态断言")
        else:
            print("  [SKIP] dev 后门关着（云端），跳过登录态断言")

        # ---------- 树契约 ----------
        print("\n【4】树接口字段契约")
        tree = get(client, "/v1/tree/1?view=overview")
        rep.ok(tree.get("code") == 0, "overview 返回成功",
               f"msg={tree.get('msg')}")
        data = tree.get("data") or {}
        nodes = data.get("nodes") or []
        edges = data.get("edges") or []
        rep.ok(len(nodes) > 0, f"返回节点（{len(nodes)} 个）")
        rep.ok(len(edges) > 0, f"返回连线（{len(edges)} 条）")

        if nodes:
            keys = set(nodes[0].keys())
            missing = REQUIRED_NODE_FIELDS - keys
            rep.ok(
                not missing,
                "节点含全部必备字段（含 w/h）",
                f"缺 {sorted(missing)} —— 八成是服务跑着旧代码。"
                "不带 --reload 启动时，改完代码必须重启进程。",
            )
            rep.ok(
                all(n.get("w") and n.get("h") for n in nodes),
                f"每个节点都有正的 w/h（{len(nodes)}/{len(nodes)}）",
            )
            rep.ok(
                all(isinstance(n.get("x"), (int, float)) for n in nodes),
                "坐标为数字而非字符串",
            )

        # ---------- 脱敏 ----------
        print("\n【5】脱敏（游客视角）")
        living = [n for n in nodes if n.get("living") == 1]
        dead = [n for n in nodes if n.get("living") == 2]
        if living:
            masked = [n for n in living if "*" in (n.get("displayName") or "")]
            rep.ok(
                len(masked) == len(living),
                f"在世者姓名全部打码（{len(masked)}/{len(living)}）",
            )
        else:
            rep.ok(True, "本家族无在世者，跳过")
        if dead:
            # 只对"本来就有名字"的已故者要求全名可见。
            # 老谱里「配某氏」这类人 name 就是 NULL，拿它当分母会假失败。
            dead_named = [n for n in dead if n.get("name")]
            unmasked = [
                n for n in dead_named
                if n.get("displayName") == n["name"]
            ]
            if dead_named:
                rep.ok(
                    len(unmasked) == len(dead_named),
                    f"已故先祖显示全名（{len(unmasked)}/{len(dead_named)}）",
                )
            else:
                rep.ok(True, "本家族无具名已故者，跳过")

        # ---------- 参数契约 ----------
        print("\n【6】参数校验")
        bad = get(client, "/v1/tree/1?view=mine")
        rep.ok(
            bad.get("code") == 40001,
            "view=mine 缺 anchor 时返回 40001 而非 500",
            f"实际 code={bad.get('code')} msg={bad.get('msg')}",
        )
        nf = get(client, "/v1/tree/1?view=notaview")
        rep.ok(nf.get("code") == 40001, "非法 view 值被拒（40001）")

    print("\n" + "=" * 68)
    if rep.failed == 0:
        print(f"结果：{rep.passed} 项全部通过 —— 对外服务与源码一致")
        print("=" * 68)
        return 0

    print(f"结果：{rep.passed} 通过 / {rep.failed} 失败")
    for f in rep.failures:
        print(f"  - {f}")
    print("=" * 68)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
