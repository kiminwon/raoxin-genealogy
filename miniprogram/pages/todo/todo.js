// 待办：我这一格还缺什么。
//
// 数据只有两个来源：
//   /auth/me                 —— 我在哪个族、认领了谁
//   /person/{me}/missing     —— 这一格缺什么（服务端算，最多给 3 条）
// 不在端上自己拼清单：缺项规则（父/母/配偶/生卒/葬地）是产品口径，
// 两边各算一遍迟早对不上。

const { get } = require('../../utils/request');
const auth = require('../../utils/auth');

Page({
  data: {
    loading: true,
    needFamily: false,
    needClaim: false,

    personId: null,
    displayName: '',
    familyName: '',
    items: [],
    missingCount: 0,
    percent: 0,
  },

  onShow() {
    this.load();
  },

  onPullDownRefresh() {
    const done = () => wx.stopPullDownRefresh();
    this.load().then(done, done);
  },

  async load() {
    this.setData({ loading: true });

    try {
      const g = getApp();
      // /auth/me 返回的是 { user: {...} }，不是裸的 user —— 少剥一层
      // 就会把 undefined 当成"没入族"，用户明明在族里却被要求去输码。
      const meBody = await auth.fetchMe().catch(() => null);
      const user = (meBody && meBody.user) || g.globalData.user;
      if (user) g.globalData.user = user;

      if (!user || !user.familyId) {
        this.setData({ loading: false, needFamily: true, needClaim: false });
        return;
      }

      const pid = user.claimedPersonId;
      if (!pid) {
        this.setData({
          loading: false,
          needFamily: false,
          needClaim: true,
          familyName: user.familyName || '',
        });
        return;
      }

      const d = await get(`/person/${pid}/missing`, {}, { silent: true });
      const pct = Math.round(((d && d.completeness) || 0) * 100);

      this.setData({
        loading: false,
        needFamily: false,
        needClaim: false,
        personId: pid,
        displayName: d.displayName || (d.person && d.person.displayName) || '',
        familyName: user.familyName || '',
        items: (d && d.items) || [],
        missingCount: (d && d.missingCount) || 0,
        percent: pct,
      });
    } catch (e) {
      // 连不上后端时不要假装"没有待办"——那会让人以为已经补完了
      this.setData({
        loading: false,
        needFamily: false,
        needClaim: false,
        items: [],
        missingCount: 0,
      });
      wx.showToast({ title: (e && e.message) || '加载失败', icon: 'none' });
    }
  },

  goLanding() {
    wx.reLaunch({ url: '/pages/landing/landing' });
  },

  goClaim() {
    wx.navigateTo({ url: '/pages/claim/claim' });
  },

  onTask() {
    this.goFill();
  },

  goFill() {
    const pid = this.data.personId;
    if (!pid) {
      this.goClaim();
      return;
    }
    wx.navigateTo({ url: `/pages/fill/fill?personId=${pid}` });
  },
});
