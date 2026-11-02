// 谱文详情 + 编辑（房长及以上）。
// 一个页面两种状态：read 只读（按空行分段渲染），edit 是 textarea 原文编辑。
// 不做富文本（启明 2026-10-21 拍板）：老谱文是文言段落，textarea 够用，
// 分段靠空行，端上按 \n 切段渲染。

const { get, post } = require('../../utils/request');

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
    mode: 'read',        // read | edit
    isNew: false,
    id: null,
    familyId: null,
    canEdit: false,

    title: '',
    category: 6,
    categoryLabel: '其他',
    updatedAt: '',
    paragraphs: [],      // read 态按空行切段

    // edit 态
    form: { title: '', category: 6, content: '' },
    categories: [
      { value: 1, label: '谱序' },
      { value: 2, label: '编修历史' },
      { value: 3, label: '迁徙源流' },
      { value: 4, label: '凡例' },
      { value: 5, label: '字辈派语' },
      { value: 6, label: '其他' },
    ],
    catIndex: 5,
    saving: false,
  },

  onLoad(q) {
    const g = getApp();
    const role = (g.globalData.user && Number(g.globalData.user.role)) || 1;
    this.setData({
      familyId: g.globalData.familyId,
      canEdit: role >= 2,
    });

    if (q.mode === 'new') {
      wx.setNavigationBarTitle({ title: '写谱文' });
      this.setData({
        mode: 'edit',
        isNew: true,
        form: { title: '', category: 6, content: '' },
        catIndex: 5,
      });
      return;
    }

    this.setData({ id: Number(q.id) });
    this.load();
  },

  async load() {
    try {
      const d = await get(`/article/${this.data.id}`, {}, { silent: false });
      wx.setNavigationBarTitle({ title: d.categoryLabel || '谱文' });
      this.setData({
        title: d.title,
        category: d.category,
        categoryLabel: d.categoryLabel,
        updatedAt: d.updatedAt || '',
        paragraphs: String(d.content || '').split(/\n+/).map((s) => s.trim()).filter(Boolean),
        form: { title: d.title, category: d.category, content: d.content },
        catIndex: Math.max(0, this.data.categories.findIndex((c) => c.value === d.category)),
      });
    } catch (e) {
      wx.showToast({ title: e.message || '读取失败', icon: 'none' });
    }
  },

  onEdit() {
    this.setData({ mode: 'edit' });
    wx.setNavigationBarTitle({ title: this.data.isNew ? '写谱文' : '编辑谱文' });
  },

  onCancel() {
    if (this.data.isNew) {
      wx.navigateBack();
      return;
    }
    // 回读态：把表单还原成已保存的版本
    this.setData({
      mode: 'read',
      catIndex: Math.max(0, this.data.categories.findIndex((c) => c.value === this.data.category)),
    });
    wx.setNavigationBarTitle({ title: this.data.categoryLabel || '谱文' });
  },

  onTitle(e) {
    this.setData({ 'form.title': e.detail.value });
  },

  onContent(e) {
    this.setData({ 'form.content': e.detail.value });
  },

  onCat(e) {
    const idx = Number(e.detail.value);
    this.setData({ catIndex: idx, 'form.category': this.data.categories[idx].value });
  },

  async onSave() {
    const f = this.data.form;
    if (!f.title.trim()) {
      wx.showToast({ title: '先写个标题', icon: 'none' });
      return;
    }
    if (!f.content.trim()) {
      wx.showToast({ title: '正文还没写', icon: 'none' });
      return;
    }

    this.setData({ saving: true });
    try {
      let d;
      if (this.data.isNew) {
        d = await post(`/family/${this.data.familyId}/articles`, f, { method: 'POST' });
      } else {
        d = await post(`/article/${this.data.id}`, f, { method: 'PUT' });
      }
      wx.showToast({ title: '已保存', icon: 'success' });
      this.setData({
        mode: 'read',
        isNew: false,
        id: d.id,
        title: d.title,
        category: d.category,
        categoryLabel: d.categoryLabel,
        updatedAt: d.updatedAt || '',
        paragraphs: String(d.content || '').split(/\n+/).map((s) => s.trim()).filter(Boolean),
        form: { title: d.title, category: d.category, content: d.content },
      });
      wx.setNavigationBarTitle({ title: d.categoryLabel || '谱文' });
    } catch (e) {
      // 403 等错误 request 封装已经 toast 过了
    } finally {
      this.setData({ saving: false });
    }
  },

  onDelete() {
    wx.showModal({
      title: '删除这篇谱文',
      content: '删除后族人将看不到它（记录仍保留，可找管理员恢复）',
      confirmText: '删除',
      confirmColor: '#A6321E',
      success: async (r) => {
        if (!r.confirm) return;
        try {
          await post(`/article/${this.data.id}`, {}, { method: 'DELETE' });
          wx.showToast({ title: '已删除', icon: 'success' });
          setTimeout(() => wx.navigateBack(), 600);
        } catch (e) {
          // request 已 toast
        }
      },
    });
  },
});
