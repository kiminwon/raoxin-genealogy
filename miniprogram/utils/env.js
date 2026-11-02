// ============================================================
// 环境开关 —— 全项目唯一一处
// ============================================================
//
// 为什么收成一个文件：以前"上线前必须改"的东西散在几个地方——
//   utils/auth.js 的 DEV_FIXED_CODE     不改 → 任何人都能冒充演示用户
//   pages/home/home.js 的 DEMO_FAMILY_ID 不改 → 任何人打开小程序都落到 1 号族
// 而家谱里**已故先人的全名对外是完整可查的**（脱敏只对在世者生效），
// 所以第二处漏改等于整族谱系裸奔。几处都得记住，就一定会漏一处。
//
// 现在要翻的开关都在这个文件里。

// ---------- 开发模式 ----------
// ⚠️ 上线前必须改成 false（它同时控制固定身份登录）。
const DEV_MODE = true;

// ---------- 后端地址 ----------
//
// 改这一行就能切换小程序连哪个后端：
//
//   'local'   本机跑的后端。**必须**在微信开发者工具里勾选
//             「详情 → 本地设置 → 不校验合法域名、web-view、TLS 版本以及 HTTPS 证书」，
//             因为它是 http://127.0.0.1，微信本来不认。
//
//   'cloud'   云服务器，走**真域名 + HTTPS + 已备案**。微信对 request 域名的四条
//             要求（HTTPS / 是域名 / 已备案 / 后台配了白名单）全部满足，所以不用再
//             勾"不校验合法域名"，开发者工具、真机预览、体验版、正式版走的是同一条路
//             ——"工具里通、真机不通"那种落差不会再出现。
//
//   'cloudIp' 应急退路：域名或证书出问题时用。http + IP，只能配合开发者工具的
//             "不校验合法域名"使用（真机用不了）。
const BACKEND = 'cloud';

const BACKENDS = {
  // 本机后端：uvicorn 直接监听 8000，没有反向代理
  local: 'http://127.0.0.1:8000/v1',

  // 云服务器：uvicorn 只监听 127.0.0.1:8000，**不对外**；
  // 对外唯一入口是 nginx 的 443（80 会 301 跳过来），所以这里不带端口号。
  cloud: 'https://api.wangqiming.top/v1',

  // 见上面 'cloudIp' 的说明
  cloudIp: 'http://150.158.176.220/v1',
};

const API_BASE = BACKENDS[BACKEND];

// ---------- 身份来源 ----------
//
//   'dev'  固定开发身份：把 code 写死成 dev:mock_xxx，方便在演示账号之间切换
//   'wx'   真登录：wx.login() → code → 后端 jscode2session → **每台微信一个 openid**
//
// 按「连哪个后端」分流，**不按 envVersion**：
//   真机调试的 envVersion 也是 'develop'，但它连的是云端，必须和体验版走同一条路；
//   给它开小灶，就等于把"工具里通、体验版不通"那个落差又造一遍。
//   而且 dev: 身份只被本机后端接受（WX_MOCK_LOGIN=true），云端发过去只会换来 40029。
//
// 这条规则的直接后果：**云端每条微信都是独立用户**。体验版分发出去之后，
// 别人进来是他的身份，不会再跟开发者共用一个 app_user。
const LOGIN_MODE = DEV_MODE && BACKEND === 'local' ? 'dev' : 'wx';

// 固定开发身份，**只在 LOGIN_MODE === 'dev' 时生效**。可选值来自
// backend/scripts/seed_demo.py：
//   dev:mock_new_member   新族人，尚未入族、未认领 —— 走完整新用户旅程
//   dev:mock_qiming       陈启明，已入族、已认领自己 —— 想看树就用这个
//   dev:mock_family_admin 陈国栋，族管理员 —— 用来调试审核、批量通过
const DEV_IDENTITY = 'dev:mock_new_member';

// ---------- 开发期兜底宗族 ----------
//
// 开发期、且**后端不可达**时兜底渲染的宗族，只为让界面是活的、能看版式。
//
// 三个限定条件都不能放宽：
//   ① 仅 DEV_MODE 下生效
//   ② 仅"问不到后端"时生效。「后端明确回答你没入族」不走这里——
//      那要老老实实去输家族码。
//   ③ **仅连本机后端时生效**。云端挂了必须报错：兜一棵别人的树出来，
//      而家谱里已故先人的全名在树上是公开的，这就不是"能看版式"了。
// 写操作永远不许依赖它：一旦认领/编辑把它当成"我在这个族里"，就等于
// 把家族码这道门直接拆掉。
const DEV_DEMO_FAMILY_ID = 1;
const ALLOW_DEMO_FAMILY = DEV_MODE && BACKEND === 'local';

module.exports = {
  DEV_MODE,
  BACKEND,
  BACKENDS,
  API_BASE,
  LOGIN_MODE,
  DEV_IDENTITY,
  DEV_DEMO_FAMILY_ID,
  ALLOW_DEMO_FAMILY,
};
