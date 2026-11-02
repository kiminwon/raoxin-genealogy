"""一次性探针：验证 deploy.env 里的 WX_SECRET 是否真的有效。

**故意不打印密钥本身**，只报长度、字符集和微信的判定结论。
原理：拿一个假 code 去调 jscode2session——
  · 密钥有效  → 微信先过密钥这关，再报 code 无效（40029）
  · 密钥无效  → 直接报 40125 invalid appsecret
这样不用真登录就能把密钥验掉，避免"填错一个字符，全员登录失败"。

用法（关掉 WX_MOCK_LOGIN 之前必跑一次）：

    backend\\.venv\\Scripts\\python.exe _deploy\\check_wx_secret.py
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent / "deploy.env"


def pick(text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


raw = ENV.read_bytes()
text = raw.decode("utf-8")
appid = pick(text, "WX_APPID")
secret = pick(text, "WX_SECRET")
mock = pick(text, "WX_MOCK_LOGIN")

print("—— 本地文件检查（不显示密钥内容）——")
print(f"  AppID          长度 {len(appid)}   值 {appid}")
print(f"  WX_SECRET      长度 {len(secret)}  （微信密钥为 32 位字母数字）")
print(f"  含空白字符     {any(c.isspace() for c in secret)}")
print(f"  全字母数字     {secret.isalnum()}")
print(f"  文件含 CR(\\r)  {b'\r' in raw}")
print(f"  WX_MOCK_LOGIN  {mock}")

if not appid or not secret:
    raise SystemExit("\n缺 AppID 或 Secret，先填 deploy.env")

print("\n—— 向微信核实（用假 code，不产生真实会话）——")
query = urllib.parse.urlencode(
    {"appid": appid, "secret": secret, "js_code": "PROBE_NOT_A_REAL_CODE",
     "grant_type": "authorization_code"}
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开本机代理
try:
    with opener.open(f"https://api.weixin.qq.com/sns/jscode2session?{query}", timeout=10) as r:
        body = json.loads(r.read().decode("utf-8"))
except Exception as e:  # noqa: BLE001
    raise SystemExit(f"  请求失败：{e}")

code = body.get("errcode", 0)
msg = body.get("errmsg", "")
print(f"  返回 errcode={code} msg={msg}")

VERDICT = {
    40029: "✅ 密钥有效（微信先过密钥这关，报的是 code 无效）",
    40125: "❌ 密钥无效（invalid appsecret）—— 粘贴错了、多了空格，或刚重置还没生效",
    40013: "❌ AppID 无效",
    45011: "⚠️ 频率限制，等一分钟重试",
    40163: "✅ 密钥有效（code 已被使用过）",
}
print(f"\n结论：{VERDICT.get(code, '⚠️ 未预期的返回，人工看一眼')}")
