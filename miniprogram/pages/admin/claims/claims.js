// 认领审核队列。
//
// 三个 tab 只是 status 参数（1 待审 / 2 已通过 / 3 已驳回），不缓存三份数据：
// 队列本来就短（一个族几十人），每次切 tab 重拉反而不会出现"审完还停在列表里"。
//
// 动作只有三个：通过、驳回（必填理由）、撤销（仅已通过的）。
// 驳回与撤销的理由**会通知到申请人**，所以输入框的提示要写清这一点——
// 用户以为写的是内部备注，就会写出"这人像骗子"这种话。

const { get, post } = require('../../../utils/request');

const app = () => getApp();

const REL_TEXT = { father: '父亲', mother: '母亲', spouse: '配偶' };
const STRENGTH_TEXT = { strong: '证据硬', medium: '一般', weak: '偏弱' };
const STRENGTH_CLASS = { strong: 'tag-jade', medium: 'tag-gold', weak: 'tag-seal' };

/** '2026-10-29T15:04:00' → 时间戳。iOS 不认带 T 的格式，统一换成斜杠。 */
function parseTime(s) {
  if (!s) return null;
  const t = new Date(String(s).replace(/-/g, '/').replace('T', ' ')).getTime();
  return isNaN(t) ? null : t;
}

function timeText(s) {
  const t = parseTime(s);
  if (!t) return '';
  const diff = Date.now() - t;
  if (diff < 60 * 1000) return '刚刚';
  if (diff < 3600 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 24 * 3600 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
  const d = new Date(t);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

Page({
  data: {
    familyId: null,
    status: 1,
    tabs: [
      { value: 1, label: '待审', count: 0 },
      { value: 2, label: '已通过', count: 0 },
      { value: 3, label: '已驳回', count: 0 },
    ],
    items: [],
    loading: true,
    openId: 0,
    emptyText: '',
    busy: false,
  },

  onLoad(query) {
    const u = (app().globalData && app().globalData.user) || {};
    const familyId = u.familyId || (app().globalData && app().globalData.familyId) || null;
    if (!familyId) {
      wx.showToast({ title: '还没有进入家族', icon: 'none' });
      setTimeout(() => wx.navigateBack(), 900);
      return;
    }
    if (Number(u.role || 1) < 2) {
      wx.showModal({
        title: '需要理事会成员权限',
        content: '认领审核由理事会成员办理。',
        showCancel: false,
        success: () => wx.navigateBack(),
      });
      return;
    }
    const status = Number(query && query.status) || 1;
    this.setData({ familyId, status });
    this.load(status);
  },

  onPullDownRefresh() {
    const done = () => wx.stopPullDownRefresh();
    this.load(this.data.status).then(done, done);
  },

  onShow() {
    // 审核完返回再进来要看到最新队列；onLoad 已经拉过的不重复拉
    if (this._loadedOnce) this.load(this.data.status);
  },

  async load(status) {
    const emptyMap = {
      1: '没有待审的认领。答题自证的申请会先出现在这里，抽查到问题再驳回。',
      2: '还没有通过的认领。',
      3: '还没有被驳回的申请。',
    };
    this.setData({ loading: true, emptyText: emptyMap[status] || '', openId: 0 });

    const d = await get(
      '/admin/claims',
      { familyId: this.data.familyId, status, limit: 50 },
      { silent: true }
    ).catch(() => null);

    const raw = (d && d.items) || [];
    const items = raw.map((it) => {
      const a = it.verifyAnswers || {};
      const rel = REL_TEXT[a.relation] || '';
      const parts = [];
      if (rel && a.relativeName) parts.push(`我${rel}：${a.relativeName}`);
      if (a.selfName) parts.push(`我叫：${a.selfName}`);
      if (a.extraName) parts.push(`另一位家长：${a.extraName}`);
      return {
        claimId: it.claimId,
        personName: it.personName,
        personGenerationLabel: it.personGenerationLabel,
        fatherName: it.fatherName,
        verifyMethodText: it.verifyMethodText,
        applicantNickname: it.applicantNickname,
        evidenceStrength: it.evidenceStrength,
        strengthText: STRENGTH_TEXT[it.evidenceStrength] || '',
        strengthClass: STRENGTH_CLASS[it.evidenceStrength] || 'tag-grey',
        verifyAnswers: a,
        answersText: parts.join('；'),
        resultText: it.resultText,
        reviewNote: it.reviewNote,
        reviewerNickname: it.reviewerNickname,
        reviewedTimeText: timeText(it.reviewedAt),
        timeText: timeText(it.createdAt),
        initial: (it.personName || '？').slice(0, 1),
        open: false,
      };
    });

    // tab 上的小数字只对"待审"有意义；另外两个 tab 显示总数容易让人误会
    const tabs = this.data.tabs.map((t) =>
      t.value === status ? { ...t, count: items.length } : t
    );

    this._loadedOnce = true;
    this.setData({ loading: false, items, tabs });
  },

  onSwitchTab(e) {
    const value = Number(e.currentTarget.dataset.value);
    if (value === this.data.status) return;
    this.setData({ status: value });
    this.load(value);
  },

  onToggle(e) {
    const id = Number(e.currentTarget.dataset.id);
    const openId = this.data.openId === id ? 0 : id;
    this.setData({
      openId,
      items: this.data.items.map((it) => ({ ...it, open: it.claimId === openId })),
    });
  },

  // ============================================================
  // 三个动作
  // ============================================================

  onApprove(e) {
    const { id, name } = e.currentTarget.dataset;
    wx.showModal({
      title: '通过这次认领',
      content: `确认「${name || '该节点'}」就是这位申请人？通过后他立刻可以填写自己这一支。`,
      editable: true,
      placeholderText: '备注（可不填，申请人可见）',
      confirmText: '通过',
      confirmColor: '#2B574B',
      success: async (r) => {
        if (!r.confirm) return;
        await this._review(id, true, (r.content || '').trim());
      },
    });
  },

  onReject(e) {
    const { id } = e.currentTarget.dataset;
    wx.showModal({
      title: '驳回这次认领',
      content: '',
      editable: true,
      placeholderText: '写清要他补什么，这句话会通知到申请人',
      confirmText: '驳回',
      confirmColor: '#A6321E',
      success: async (r) => {
        if (!r.confirm) return;
        const note = (r.content || '').trim();
        if (!note) {
          // 驳回不给理由，申请人只会收到一句"未通过"，然后在群里问到底为什么
          wx.showToast({ title: '请写一句理由再驳回', icon: 'none' });
          return;
        }
        await this._review(id, false, note);
      },
    });
  },

  onRevert(e) {
    const { id, name } = e.currentTarget.dataset;
    wx.showModal({
      title: '撤销这次认领',
      content: `「${name || '该节点'}」将不再由这位申请人代表。他做过的改动不会被删除，只是不能再改。`,
      editable: true,
      placeholderText: '写清原因，会通知到本人',
      confirmText: '撤销',
      confirmColor: '#A6321E',
      success: async (r) => {
        if (!r.confirm) return;
        const note = (r.content || '').trim();
        if (!note) {
          wx.showToast({ title: '请写一句原因再撤销', icon: 'none' });
          return;
        }
        if (this.data.busy) return;
        this.setData({ busy: true });
        try {
          await post(
            `/admin/claim/${id}/revert`,
            { reviewNote: note, revokeScope: true },
            { showLoading: true }
          );
          wx.showToast({ title: '已撤销', icon: 'success' });
          await this.load(this.data.status);
        } catch (err) {
          // request 已经弹过 toast
        } finally {
          this.setData({ busy: false });
        }
      },
    });
  },

  async _review(id, approve, note) {
    if (this.data.busy) return;
    this.setData({ busy: true });
    try {
      await post(
        `/admin/claim/${id}/review`,
        { approve, reviewNote: note || null, grantCloseRelatives: true },
        { showLoading: true }
      );
      wx.showToast({ title: approve ? '已通过' : '已驳回', icon: 'success' });
      await this.load(this.data.status);
    } catch (err) {
      // request 已 toast
    } finally {
      this.setData({ busy: false });
    }
  },
});
