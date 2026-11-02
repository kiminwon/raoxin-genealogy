// 我的：身份、族书架、分享。
//
// 三块数据的来源各不相同，别混：
//   /auth/me      —— 我是谁（昵称、族、角色、认领到哪个节点）
//   /family/mine  —— 这个族的堂号/始迁祖/浏览码（分享要用）
//   /family/list  —— 我加入过的全部族（书架）
//
// 分享是这个页面最重要的动作：接力修谱的起点就是"把链接发到家族群"。
//
// 分享链接 = 族浏览码（进门）+ 分享人 personId（锚点）。**链接不带权限**：
// 编辑权只来自认领后的 edit_scope。个人编辑码（codeType=2）2026-10-22 已下线，
// 因为它试图让链接携带权限，等于在身份系统之外开一条查不到人的后门。

const { get, post } = require('../../utils/request');
const auth = require('../../utils/auth');

Page({
  data: {
    loading: true,
    backend: '',
    nickname: '',
    initial: '族',
    familyId: null,
    familyName: '',
    role: 1,
    roleText: '',
    claimedPersonId: null,
    claimedName: '',
    viewCode: '',
    isReviewer: false,

    // 我这一格（原「待办」tab，2026-11-02 并入本页）
    // missingCount = -1 表示"没答上来"，和 0（确实补完了）必须分开：
    // 把失败说成"已经完整"，用户就再也不会回来补了。
    missingCount: 0,
    percent: 0,
    todoItems: [],
    todoName: '',

    families: [],
  },

  onShow() {
    this._pendingShare = null;
    this.load();
  },

  onPullDownRefresh() {
    const done = () => wx.stopPullDownRefresh();
    this.load().then(done, done);
  },

  async load() {
    this.setData({ loading: true, backend: getApp().globalData.backend });

    try {
      const meBody = await auth.fetchMe().catch(() => null);
      const user = (meBody && meBody.user) || getApp().globalData.user;

      if (!user) {
        this.setData({ loading: false, families: [] });
        return;
      }
      getApp().globalData.user = user;

      const name = user.nickname || '';
      this.setData({
        nickname: name,
        initial: (name || '族').slice(0, 1),
        familyId: user.familyId || null,
        familyName: user.familyName || '',
        role: user.role || 1,
        roleText: this._roleText(user.role),
        claimedPersonId: user.claimedPersonId || null,
        // 入口只对理事会成员及以上显示（真正的权限判断在后端每个接口里）
        isReviewer: Number(user.role || 1) >= 2,
      });

      // 三块互不依赖，串行只是为了让失败不互相拖累
      await Promise.all([
        this._loadFamily(),
        this._loadFamilies(),
        this._loadClaimedPerson(user.claimedPersonId),
        this._loadTodo(user.claimedPersonId),
      ]);

      this.setData({ loading: false });
    } catch (e) {
      this.setData({ loading: false });
      wx.showToast({ title: (e && e.message) || '加载失败', icon: 'none' });
    }
  },

  _roleText(role) {
    // 与后端 family.py::_ROLE_TEXT 同一套叫法，三处（后端/首页/我的）不许各写一份
    return { 1: '族人', 2: '理事会成员', 3: '族管理员', 4: '平台运营' }[Number(role)] || '族人';
  },

  async _loadFamily() {
    try {
      // 注意：viewCode 在 data.family 里，不在顶层
      const d = await get('/family/mine', {}, { silent: true });
      const fam = d && d.family;
      if (fam) this.setData({ viewCode: fam.viewCode || '', familyName: fam.name || this.data.familyName });
    } catch (e) {
      // 没入族就是 40100 / 空，这里不打扰用户
    }
  },

  async _loadFamilies() {
    try {
      const d = await get('/family/list', {}, { silent: true });
      this.setData({ families: (d && d.families) || [] });
    } catch (e) {
      this.setData({ families: [] });
    }
  },

  async _loadClaimedPerson(pid) {
    if (!pid) {
      this.setData({ claimedName: '' });
      return;
    }
    try {
      const p = await get(`/person/${pid}`, {}, { silent: true });
      this.setData({ claimedName: (p && p.displayName) || '' });
    } catch (e) {
      // 脱敏/权限异常时宁可只显示"已在谱上"，也不要瞎猜名字
      this.setData({ claimedName: '已在谱上' });
    }
  },

  /**
   * 我这一格缺什么。
   *
   * 缺项清单只在服务端算（`build_missing_tasks` 是唯一口径），端上不自己拼——
   * 两边各算一遍，迟早出现"这里说差一项、点进去说已经补全"。
   * 拿不到就置 -1（不是 0）：0 的意思是"确实补完了"，那是好消息，不能凭空给。
   */
  async _loadTodo(pid) {
    if (!pid) {
      this.setData({ todoItems: [], missingCount: 0, percent: 0, todoName: '' });
      return;
    }
    try {
      const d = await get(`/person/${pid}/missing`, {}, { silent: true });
      this.setData({
        todoItems: ((d && d.items) || []).slice(0, 3),
        missingCount: (d && d.missingCount) || 0,
        percent: Math.round(((d && d.completeness) || 0) * 100),
        todoName: (d && d.displayName) || '',
      });
    } catch (e) {
      this.setData({ todoItems: [], missingCount: -1, percent: 0 });
    }
  },

  // ============================================================
  // 分享
  // ============================================================

  /**
   * 分享家族 → 落地页，**链接里直接带族浏览码**，对方点开就是预览态，
   * 不需要先输码（"进入小程序 → 找输码框 → 问家人要码"这条路已经堵住了）。
   *
   * 再带一个 anchor=认领的节点：对方入族后还没认领自己时，「我的一家」
   * 先聚焦到分享人身上——他第一眼看到的是自己认得出的名字。
   * ⚠️ anchor 只是视角，不是权限：能改什么仍然只看他自己的 edit_scope。
   */
  onShareAppMessage() {
    const code = this.data.viewCode;
    const anchor = this.data.claimedPersonId;
    const family = this.data.familyName || '家族';

    if (code) {
      const extra = anchor ? `&anchor=${anchor}` : '';
      return {
        title: anchor
          ? `${family}的家谱 · ${this.data.claimedName || '我'}这一支，您来看看`
          : `${family}的家谱 · 进来看看你在哪一支`,
        path: `/pages/landing/landing?code=${encodeURIComponent(code)}${extra}`,
        // 分享卡封面：金蓝「电子族谱」发光树（2026-11-02 换）。
        // imageUrl 为空时微信默认截当前页面——卡片上是一屏代码界面，没法看。
        imageUrl: '/images/share-cover.jpg',
      };
    }

    // 还没拿到浏览码（族关掉了对外分享 / 接口失败）：宁可不带码，
    // 也要把落地页给出去——对方至少能自己输码，而不是收到一个打不开的卡片。
    return {
      title: '饶信谱脉 · 把家里的谱续下去',
      path: `/pages/landing/landing${anchor ? `?anchor=${anchor}` : ''}`,
    };
  },

  onShareTimeline() {
    const code = this.data.viewCode;
    const anchor = this.data.claimedPersonId;
    const qs = [];
    if (code) qs.push(`code=${encodeURIComponent(code)}`);
    if (anchor) qs.push(`anchor=${anchor}`);
    return {
      title: `${this.data.familyName || '家族'}的家谱`,
      query: qs.join('&'),
      imageUrl: '/images/share-cover.jpg',
    };
  },

  // ============================================================
  // 操作
  // ============================================================

  onClaimTap() {
    if (this.data.claimedName) {
      const pid = this.data.claimedPersonId;
      if (pid) wx.navigateTo({ url: `/pages/person/person?id=${pid}` });
      return;
    }
    wx.navigateTo({ url: '/pages/claim/claim' });
  },

  async onSwitchFamily(e) {
    const { id, current } = e.currentTarget.dataset;
    if (current || !id) return;

    try {
      wx.showLoading({ title: '切换中', mask: true });
      const d = await post('/family/switch', { familyId: id }, { silent: true });
      wx.hideLoading();

      const g = getApp();
      if (g.globalData.user) {
        // 切族换的是整份快照，不能只改 familyId
        g.globalData.user.familyId = d.family.id;
        g.globalData.user.familyName = d.family.name;
        g.globalData.user.role = d.role;
        g.globalData.user.claimedPersonId = d.claimedPersonId;
        g.globalData.user.needClaim = !!d.needClaim;
      }
      g.globalData.familyId = d.family.id;
      // 分享锚点属于上一个族的人，切族后必须丢掉——
      // 否则「我的一家」会拿一个不属于本族的 anchor 去请求（服务端 40001）
      g.globalData.shareAnchor = null;
      g.globalData.treeDirty = true;

      wx.showToast({ title: `已切到${d.family.name}`, icon: 'none' });
      // 首页是 tab，用 switchTab 而不是 reLaunch —— 不然会多压一层页面栈
      setTimeout(() => wx.switchTab({ url: '/pages/home/home' }), 400);
    } catch (err) {
      wx.hideLoading();
      wx.showToast({ title: (err && err.message) || '切换失败', icon: 'none' });
    }
  },

  onAddFamily() {
    wx.navigateTo({ url: '/pages/landing/landing' });
  },

  onAdmin() {
    wx.navigateTo({ url: '/pages/admin/admin' });
  },

  // ---------- 我这一格 ----------

  onTodoItem(e) {
    const pid = this.data.claimedPersonId;
    if (!pid) return;
    // 填写页从这一格的第一问开始；taskKey 目前不认（fill.js 只接 personId），
    // 传了也是死参数——等填写页支持"直接落到某一条"再加。
    wx.navigateTo({ url: `/pages/fill/fill?personId=${pid}` });
  },

  /** 「逐条补完」= 原待办页，现在不再是 tab，从这张卡进去 */
  onTodoAll() {
    wx.navigateTo({ url: '/pages/todo/todo' });
  },

});
