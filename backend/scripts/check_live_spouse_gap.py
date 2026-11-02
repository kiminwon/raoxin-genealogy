"""对着**运行中的服务**量夫妻间距（线上/本机都能用），验证布局改动真的生效。

    cd backend
    .venv\\Scripts\\python.exe scripts/check_live_spouse_gap.py [base_url]

只读，不改任何数据。
"""

from __future__ import annotations

import asyncio
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
CODE = sys.argv[2] if len(sys.argv) > 2 else "dev:mock_qiming"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        r = await c.post("/v1/auth/login", json={"code": CODE})
        body = r.json()
        if body.get("code") != 0:
            print("登录失败：", body.get("msg"), "（本机用 dev: 前缀，线上不能 mock 登录）")
            sys.exit(1)
        token = body["data"]["token"]
        me = body["data"].get("user") or body["data"]
        anchor = me.get("claimedPersonId") or body["data"].get("claimedPersonId")
        print(f"登录 {CODE} → claimedPersonId={anchor}")

        h = {"Authorization": f"Bearer {token}"}
        r = await c.get("/v1/tree/1", params={"view": "mine", "anchor": anchor}, headers=h)
        data = r.json()
        if data.get("code") != 0:
            print("取树失败：", data)
            sys.exit(1)
        d = data["data"]
        print(f"view={d.get('view')} 节点 {len(d['nodes'])} canvas {d['canvas']['width']}x"
              f"{d['canvas']['height']} spouseGap={d['canvas'].get('spouseGap')} cached={d.get('cached')}")

        by_id = {n["id"]: n for n in d["nodes"]}
        bad = 0
        for n in d["nodes"]:
            sp = n.get("spouseOf")
            if sp is None or sp not in by_id:
                continue
            a = by_id[sp]
            gap = n["x"] - (a["x"] + a["w"])
            name = f"{a.get('displayName')}↔{n.get('displayName')}"
            flag = "✓" if gap == 0 else "✗"
            if gap != 0:
                bad += 1
            print(f"  {flag} {name}  间距 {gap}px  (x {a['x']} / {n['x']})"
                  f"  isSpouse={n.get('isSpouse')} endReason={n.get('endReason')}")

        print("✅ 夫妻全部零间距" if bad == 0 else f"❌ {bad} 对不紧贴")
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
