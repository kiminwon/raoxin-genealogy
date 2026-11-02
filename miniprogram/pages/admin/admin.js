// 理事会工作台。
//
// 权限：role >= 2（理事会成员及以上）。后端每个接口都会再校验一次，
// 前端的判断只是为了不让族人看到一个进不去的入口——**不是安全边界**。
//
// 这一页的数据全部来自 /admin/*，出任何错都只提示、不白屏：
// 理事会长在家族群里当着几十号人打开它，白屏比少一个数字更糟。

const { get, post } = require('../../utils/request');

const app = () => getApp();

const ROLE_TEXT = { 1: '族人', 2: '理事会成员', 3: '族管理员', 4: '平台运营' };

Page({
  data: {
    loading: true,
    familyId: null,
    familyName: '',
    role: 1,
    roleText: '',
    stats: {},
    locks: [],
    locksNote: '',
    busy: false,
  },

  onLoad() {
    const u = (app().globalData && app().globalData.user) || {};
    const familyId = u.familyId || (app().globalData && app().globalData.familyId) || null;
    const role = Number(u.role || 1);

    if (!familyId) {
      wx.showToast({ title: '还没有进入家族', icon: 'none' });
      setTimeout(() => wx.navigateBack(), 900);
      return;
    }
    if (role < 2) {
      // 别写"无权限"就完了：告诉他怎么才能有
      wx.showModal({
        title: '需要理事会成员权限',
        content: '认领审核与解禁由理事会成员办理。请让族管理员在「我的 → 成员角色」里把您设为理事会成员。',
        showCancel: false,
        success: () => wx.navigateBack(),
      });
      return;
    }

    this.setData({
      familyId,
      familyName: u.familyName || '',
      role,
      roleText: ROLE_TEXT[role] || '理事会成员',
    });
    this.load();
  },

  onPullDownRefresh() {
    const done = () => wx.stopPullDownRefresh();
    this.load().then(done, done);
  },

  async load() {
    const { familyId } = this.data;
    if (!familyId) return;
    this.setData({ loading: this.data.stats.personCount === undefined });

    // 两块互不依赖，谁挂了都不影响另一块显示
    const [dash, locks] = await Promise.all([
      get('/admin/dashboard', { familyId }, { silent: true }).catch(() => null),
      get('/admin/identify/locks', { familyId }, { silent: true }).catch(() => null),
    ]);

    this.setData({
      loading: false,
      stats: (dash && dash.stats) || this.data.stats,
      locks: (locks && locks.items) || [],
      locksNote: (locks && locks.note) || '',
    });
  },

  // ============================================================
  // 跳转
  // ============================================================

  onGoClaims(e) {
    const status = (e.currentTarget.dataset.status || 1);
    wx.navigateTo({ url: `/pages/admin/claims/claims?status=${status}` });
  },

  onPendingRevision() {
    // 修订审核页还没做。**说实话，别让人点了等一个空白页**（决策 20：
    // 提示词必须指向当前做得到的动作）。
    wx.showModal({
      title: '修订审核还没开放',
      content: `当前有 ${this.data.stats.pendingRevisionCount || 0} 条待审修订。这一块页面还在做，暂时需要技术支持代审。认领审核已经可以用了。`,
      showCancel: false,
    });
  },

  // ============================================================
  // 解禁
  // ============================================================

  onUnlock(e) {
    const { id, name } = e.currentTarget.dataset;
    if (this.data.busy) return;

    wx.showModal({
      title: '解除答题限制',
      content: `让「${name || '这位成员'}」现在就能重新答题？这不会改任何家谱数据。`,
      confirmText: '解禁',
      confirmColor: '#2B574B',
      success: async (r) => {
        if (!r.confirm) return;
        this.setData({ busy: true });
        try {
          await post('/admin/identify/unlock', {
            familyId: this.data.familyId,
            userId: id,
            note: '理事会一键解禁',
          });
          wx.showToast({ title: '已解禁', icon: 'success' });
          await this.load();
        } catch (err) {
          wx.showToast({ title: (err && err.message) || '解禁失败', icon: 'none' });
        } finally {
          this.setData({ busy: false });
        }
      },
    });
  },
});
