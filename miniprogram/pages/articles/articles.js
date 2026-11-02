// 谱文列表：按分类分组展示。
// 分类是固定枚举（谱序/编修历史/迁徙源流/凡例/字辈派语/其他），
// 顺序按传统谱的目录排，不按字母也不按时间——长辈翻谱就认这个顺序。

const { get } = require('../../utils/request');

const CATEGORY_ORDER = [1, 2, 3, 4, 5, 6];
const CATEGORY_LABEL = {
  1: '谱序',
  2: '编修历史',
  3: '迁徙源流',
  4: '凡例',
  5: '字辈派语',
  6: '其他',
};

Page({
  data: {
    loading: true,
    loadError: '',
    groups: [],
    // 房长及以上才能写。角色是"当前族"的快照（切族由后端管），
    // 这里直接读 globalData，拿不到就当族人——顶多看不到按钮，不会误放行。
    canEdit: false,
  },

  onLoad() {
    this.load();
  },

  onShow() {
    // onLoad 已经拉过一次，首次进页面直接跳过；
    // 从详情页回来（新写 / 改了标题 / 删了）必须重拉，
    // 否则列表还停在进去之前的样子——"刚写的那篇不在列表里"。
    if (!this._shownOnce) {
      this._shownOnce = true;
      return;
    }
    this.load();
  },

  async load() {
    this.setData({ loading: true, loadError: '' });
    const g = getApp();
    try {
      // ⚠️ 这一页 2026-11-02 起是 tabBar 的一格，可以被**直接点开**（冷启动）：
      // 那一刻 globalData.user / familyId 都还是空的。不先补身份就查族，
      // 会把"还没登录"当成"还没加入家族"——人在族里，页面却说没有。
      const user = await g.ensureLogin().catch(() => null);
      const fid = (user && user.familyId) || g.globalData.familyId;
      if (!fid) {
        this.setData({ loading: false, loadError: '还没有加入任何家族' });
        return;
      }
      // 写作权限跟着身份一起刷新（切族后再进来，角色可能变了）
      this.setData({ canEdit: Number((user && user.role) || 1) >= 2 });

      const d = await get(`/family/${fid}/articles`, {}, { silent: false });
      this.apply(d.items || []);
    } catch (e) {
      this.setData({ loading: false, loadError: e.message || '读取失败' });
    }
  },

  apply(items) {
    const byCat = {};
    items.forEach((it) => {
      (byCat[it.category] = byCat[it.category] || []).push(it);
    });
    const groups = CATEGORY_ORDER.filter((c) => byCat[c] && byCat[c].length).map((c) => ({
      category: c,
      label: CATEGORY_LABEL[c],
      items: byCat[c],
    }));
    this.setData({ groups, loading: false });
  },

  onOpen(e) {
    const id = e.currentTarget.dataset.id;
    wx.navigateTo({ url: '/pages/article/article?id=' + id });
  },

  onCreate() {
    wx.navigateTo({ url: '/pages/article/article?mode=new' });
  },

  onRetry() {
    this.load();
  },
});
