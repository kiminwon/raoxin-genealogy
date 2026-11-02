// 登录：wx.login 拿 code -> 后端换 token
//
// 不索要手机号、不索要用户信息。家谱的首次访问 90% 来自"别人分享到家族群的链接"，
// 多一步授权就少一半用户。

const { post } = require('./request');
const { LOGIN_MODE, DEV_IDENTITY } = require('./env');
const session = require('./session');

// ============================================================
// 两种登录身份
// ------------------------------------------------------------
// 走哪条由 utils/env.js 的 LOGIN_MODE 决定，规则只有一条：
//   **连本机后端 → 固定开发身份；连云端 → 真 wx.login()**
//
// 为什么不在开发者工具里一律用固定身份（历史做法）：
//   真机调试的 envVersion 也是 'develop'，但它连的是云端。给工具开小灶，
//   "工具里通、体验版不通"的落差就会再来一次；而且 dev: 身份本来也只被
//   本机后端接受（云端 WX_MOCK_LOGIN=false），发过去只会换来 40029。
//
// 代价要说清楚：连云端时开发者也变成"自己的微信号"这个真实用户，
// 不再是陈启明/陈雨欣那些演示账号。想以演示账号身份看数据，
// 就把 BACKEND 切回 'local'（本机后端开着 mock），不要指望云端认 dev:。
//
// DEV_IDENTITY（固定开发身份）在 utils/env.js 里，和 DEV_MODE 放一起：
// 所有"上线前要翻的开关"集中一处，别散。

function wxLogin() {
  return new Promise((resolve, reject) => {
    wx.login({
      success: (res) => (res.code ? resolve(res.code) : reject(new Error('wx.login 未返回 code'))),
      fail: reject,
    });
  });
}

async function login() {
  const g = getApp();
  const code = LOGIN_MODE === 'dev' ? DEV_IDENTITY : await wxLogin();
  const inviteCode = (g && g.globalData.pendingInviteCode) || '';

  const data = await post(
    '/auth/login',
    { code, inviteCode: inviteCode || undefined },
    { silent: true }
  );

  if (data.token) {
    g.globalData.token = data.token;
    g.globalData.user = data.user;
    if (data.user && data.user.familyId) {
      g.globalData.familyId = data.user.familyId;
    }
    // 跟环境标记一起存：换后端 / 换身份来源时这条 token 会被自动作废，
    // 不会再出现"改了登录方式，手机上还用着老身份"。
    session.save(data.token);
  }

  return data.user;
}

async function fetchMe() {
  const { get } = require('./request');
  return get('/auth/me', {}, { silent: true });
}

/**
 * 确保已登录。需要编辑能力时调用。
 */
async function requireLogin() {
  const g = getApp();
  if (g.globalData.token && g.globalData.user) {
    return g.globalData.user;
  }
  const user = await login();
  if (!user) {
    throw new Error('登录失败');
  }
  return user;
}

module.exports = { login, fetchMe, requireLogin, wxLogin };
