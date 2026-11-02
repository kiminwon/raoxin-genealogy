// 饶信谱脉 · 家谱小程序
//
// 后端地址不在这里写死——统一由 utils/env.js 的第一个开关决定
// （'local' 连本机后端 / 'cloud' 连云服务器）。
//
// 在微信开发者工具里需要勾选「详情 → 本地设置 → 不校验合法域名」，
// 否则 wx.request 会被拦——因为我们现在用的是 http + IP。

const auth = require('./utils/auth');
const env = require('./utils/env');
const session = require('./utils/session');

App({
  globalData: {
    baseUrl: env.API_BASE,
    // 当前连的是哪个后端，界面上可以直接显示，省得联调时两边猜
    backend: env.BACKEND,
    token: '',
    user: null,
    familyId: null,
    // 从邀请链接进来时记录，登录后用于 accept
    pendingInviteCode: '',
    // 分享链接里的锚点（分享人 personId）。**这不是权限，只是"先看谁"**：
    // 未认领的人点进「我的一家」时，先聚焦到发链接的那位家人身上，
    // 他第一眼看到的才是自己熟悉的名字，而不是从始祖开始找自己。
    shareAnchor: null,
    // 下拉刷新标记：首页重新拉树
    treeDirty: false,
  },

  onLaunch(options) {
    // session.load() 会核对这条 token 是哪个后端、哪种身份来源签发的。
    // 对不上就直接丢掉 —— 换身份来源之后，手机里那条旧 token 还在有效期内，
    // 不丢就会一直顶着老身份跑（"所有人都是陈雨欣"就是这么来的）。
    const cached = session.load();
    if (cached) {
      this.globalData.token = cached;
    }

    // 分享进来的邀请码 / 场景值
    const code = (options && options.query && options.query.code) || '';
    if (code) {
      this.globalData.pendingInviteCode = code;
    }
    const anchor = parseInt(options && options.query && options.query.anchor, 10);
    if (anchor > 0) {
      this.globalData.shareAnchor = anchor;
    }
  },

  onShow(options) {
    const q = (options && options.query) || {};
    const code = q.code || '';
    if (code && code !== this.globalData.pendingInviteCode) {
      this.globalData.pendingInviteCode = code;
    }
    // 热启动（小程序还在后台）也会走到这里，锚点同样要接住
    const anchor = parseInt(q.anchor, 10);
    if (anchor > 0) {
      this.globalData.shareAnchor = anchor;
    }
  },

  /**
   * 静默登录：拿到 token 但不强制用户操作。
   * 未登录也能浏览脱敏的三代，只有「要编辑」时才必须登录。
   */
  async ensureLogin() {
    // ⚠️ 冷启动陷阱（2026-10-21 修）：token 存在 storage 里，`globalData.user` 却只在内存里。
    // 小程序被杀掉重进之后 user 一定是空的，而原来的写法只要看见 token 就 `return user`
    // （返回 undefined），调用方于是拿到"登录过了、但没有身份"：
    //   · 首页取不到 claimedPersonId → 锚点兜底成始祖 → 「我的一家」里连自己都不在画面上，
    //     必须先点「全族总览」再点回来（那时 user 已被别的路径填上）才聚焦到自己
    //   · `needClaim: !claimed` 误判成 true → 认领提示一直挂着
    // 所以 token 在、user 不在时，用 /auth/me 把身份补回来，不重走 wx.login
    // （重走会换 token，等于给每次冷启动平白加一次登录往返）。
    if (this.globalData.token && this.globalData.user) {
      return this.globalData.user;
    }

    if (this.globalData.token) {
      try {
        const me = await auth.fetchMe();
        const user = (me && me.user) || me || null;
        if (user) {
          this.globalData.user = user;
          if (user.familyId) {
            this.globalData.familyId = user.familyId;
          }
          return user;
        }
      } catch (e) {
        // token 过期/被清 → 落到下面重新登录
      }
    }

    const user = await auth.login();
    this.globalData.user = user;
    if (user && user.familyId) {
      this.globalData.familyId = user.familyId;
    }
    return user;
  },
});
