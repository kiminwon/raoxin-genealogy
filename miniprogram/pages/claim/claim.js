// 认领自己 —— 答题自证
//
// 为什么不是「在树上点自己 + 等房长审」：
//   ① 脱敏规则下，认领**之前**用户看到的自己是「陈*明」，他没法确认那是不是自己；
//   ② 房长年纪大、不会用审批界面，把审核挂在主路径上，整条链路就卡死了。
// 所以改成：直接问，系统反查 —— 用户不需要先在树上找到任何东西。
//
// 题目的强度取决于数据：所填亲属在世时，树上显示的是「陈*栋」，外人抄不到；
// 已故时树上就是全名。所以"谱里没我、要新建节点"这条路径上，
// 遇到已故亲属会多问一位家长——成本很低，却能把照着屏幕建节点的噪音挡在外面。

const { get, post } = require('../../utils/request');

const RELATIONS = [
  { value: 'father', label: '我父亲', ask: '他叫什么名字' },
  { value: 'mother', label: '我母亲', ask: '她叫什么名字' },
  { value: 'spouse', label: '我配偶', ask: '他/她叫什么名字' },
];

/**
 * 限速剩余时间的人话。
 * 后端把 retryAfterSeconds 一起给了，这里只做换算——
 * 让用户看到"还需约 43 分钟"而不是 2580 这个数字，也不是"请稍后再试"。
 */
function waitText(sec) {
  const s = Number(sec) || 0;
  if (s <= 0) return '';
  if (s < 60) return `还要等 ${s} 秒`;
  const m = Math.ceil(s / 60);
  if (m < 60) return `还要等约 ${m} 分钟`;
  return `还要等约 ${Math.round(m / 60)} 小时`;
}

Page({
  data: {
    loading: true,
    submitting: false,
    // quiz 答题 | done 完成 | nomatch 对不上 | already 已确认
    stage: 'quiz',

    familyName: '',
    familyMeta: '',
    relations: RELATIONS,
    relation: 'father',
    askLabel: '他叫什么名字',
    relationHint: '',

    relativeName: '',
    selfName: '',
    extraName: '',
    extraOpen: false,
    extraNote: '',

    result: null,
    canSubmit: false,

    // 当前聚焦的是哪个输入框（-1 = 无）。
    // 提交时把它拨回 -1 让输入框失焦 —— 见 blurForm() 的说明。
    formFocusIdx: -1,
  },

  /**
   * 提交前先把输入框失焦。
   *
   * 原生 input 在部分安卓 ROM（2026-10-17 一加真机实测）上同层渲染会失败：
   * 它浮在所有元素之上，而且带着焦点被 wx:if 销毁后**原生层不跟着回收**，
   * 结果就是已经切到下一态了，输入框还浮在页面上。
   * 用受控 focus 拨回 -1 主动失焦，原生层才会随焦点释放被正常回收。
   *
   * 为什么用「索引」而不是一个布尔：布尔会让**所有**输入框的 focus 属性
   * 同时从 false 变 true，互相抢焦点；索引永远只有一个框是 true。
   * 为什么敢立即切态：提交有一次网络往返，远长于焦点释放所需的时间。
   */
  blurForm() {
    if (this.data.formFocusIdx !== -1) this.setData({ formFocusIdx: -1 });
    try {
      wx.hideKeyboard();
    } catch (e) {
      // 没有键盘时不报错就行
    }
  },

  onLoad() {
    this.loadQuiz();
  },

  async loadQuiz() {
    this.setData({ loading: true });

    try {
      const g = getApp();
      if (!g.globalData.token) {
        await g.ensureLogin();
      }

      const d = await get('/auth/identify/quiz', {}, { silent: true });

      // 已确认过身份就别再答一遍
      if (d.alreadyClaimed) {
        this.setData({
          loading: false,
          stage: 'already',
          familyName: d.familyName || '',
          result: {
            personId: d.alreadyClaimed.personId,
            personName: d.alreadyClaimed.name,
            generationLabel: d.alreadyClaimed.generation
              ? `第${d.alreadyClaimed.generation}世`
              : '',
          },
        });
        return;
      }

      // 后端把题目发下来了，这里按它渲染，不写死选项——
      // 题目将来会按数据变（比如老谱里配偶无名时改问别的），端上不该跟着改。
      const relQ = (d.questions || []).find((q) => q.key === 'relation');
      const relations = (relQ && relQ.options) ? relQ.options : RELATIONS;
      const current = relations[0] || RELATIONS[0];

      this.setData({
        loading: false,
        stage: 'quiz',
        familyName: d.familyName || '',
        familyMeta: d.personCount ? `${d.personCount} 位族人 · ${d.generationCount} 世` : '',
        relations,
        relation: current.value,
        askLabel: this._askLabel(current.value),
        relationHint: (relQ && relQ.hint) || '',
        quizHint: d.hint || '',
      });
    } catch (e) {
      this.setData({ loading: false });

      // 40001 在这一步只可能是"还没入族"：quiz 接口只有 familyId 一个参数，
      // 而且我们不传（见上面的请求），所以它要么用到 user.family_id，
      // 要么就是空。别弹一句用户看不懂的后端话术，
      // 直接把他送到输家族码那一屏。
      if (e.code === 40001) {
        wx.redirectTo({ url: '/pages/landing/landing' });
        return;
      }

      wx.showToast({
        title: e.code === 40100 ? '请重新进入小程序' : (e.message || '读取失败'),
        icon: 'none',
        duration: 2600,
      });
    }
  },

  _askLabel(relation) {
    const r = RELATIONS.find((x) => x.value === relation);
    return r ? r.ask : '他/她叫什么名字';
  },

  onPickRelation(e) {
    const value = e.currentTarget.dataset.value;
    const extraOpen = value === 'spouse' ? false : this.data.extraOpen;
    this.setData({
      relation: value,
      askLabel: this._askLabel(value),
      // 换了关系，之前补填的名字就不再对应了
      extraName: value === 'spouse' ? '' : this.data.extraName,
      extraOpen: value === 'spouse' ? false : extraOpen,
      extraNote: '',
    });
    this._refreshCanSubmit();
  },

  onInput(e) {
    const key = e.currentTarget.dataset.key;
    this.setData({ [key]: e.detail.value });
    this._refreshCanSubmit();
  },

  onToggleExtra() {
    this.setData({ extraOpen: !this.data.extraOpen });
  },

  _refreshCanSubmit() {
    const { relativeName, selfName } = this.data;
    this.setData({
      canSubmit: !!(relativeName || '').trim() && !!(selfName || '').trim(),
    });
  },

  /** 输入框聚焦时记下是哪一个（受控 focus 的一半：状态跟真实焦点对齐） */
  onFormFocus(e) {
    const idx = Number(e.currentTarget.dataset.focusIndex);
    if (this.data.formFocusIdx !== idx) this.setData({ formFocusIdx: idx });
  },

  async onSubmit() {
    if (this.data.submitting) return;

    // 提交即离开答题界面：先把输入框失焦（见 blurForm 的说明）
    this.blurForm();

    const relativeName = (this.data.relativeName || '').trim();
    const selfName = (this.data.selfName || '').trim();
    if (!relativeName || !selfName) {
      wx.showToast({ title: '把两个名字都填上', icon: 'none' });
      return;
    }

    this.setData({ submitting: true });

    try {
      const d = await post(
        '/auth/identify/verify',
        {
          relation: this.data.relation,
          relativeName,
          selfName,
          extraName: (this.data.extraName || '').trim() || null,
        },
        { silent: true }
      );
      this.setData({ submitting: false });
      this._handle(d);
    } catch (e) {
      this.setData({ submitting: false });
      this._handleError(e);
    }
  },

  _handle(d) {
    const g = getApp();
    const result = d.result;

    if (result === 'matched') {
      g.globalData.treeDirty = true;
      g.globalData.claimedPersonId = d.personId;
      if (g.globalData.user) g.globalData.user.claimedPersonId = d.personId;

      this.setData({
        stage: 'done',
        result: {
          kind: 'matched',
          personId: d.personId,
          title: d.alreadyClaimed ? '已经确认过了' : '确认成功',
          name: d.personName,
          generationLabel: d.generationLabel || '',
          grantedNodeCount: d.grantedNodeCount || 0,
          filled: d.filledFields || [],
          sub: d.alreadyClaimed
            ? '这一位就是您，可以直接去补全信息。'
            : '现在可以补全您和直系亲属的信息了。',
        },
      });
      return;
    }

    if (result === 'new_member') {
      g.globalData.treeDirty = true;
      g.globalData.claimedPersonId = d.personId;
      if (g.globalData.user) g.globalData.user.claimedPersonId = d.personId;

      this.setData({
        stage: 'done',
        result: {
          kind: 'new_member',
          personId: d.personId,
          title: '已经把您接上去了',
          name: d.personName,
          generationLabel: d.generationLabel || '',
          relativeName: d.relativeName || '',
          grantedNodeCount: d.grantedNodeCount || 0,
          filled: [],
          sub: `家谱上原来没有您，已经按您填的信息，把您接在「${d.relativeName || '您填的亲属'}」名下。`,
        },
      });
      return;
    }

    if (result === 'ambiguous') {
      // 两种情况都会走到这里：
      //   ① 族里同名多人 → 让他补填另一位家长
      //   ② 谱上没有他，但所填亲属已故（答案在屏幕上，证据太弱）→ 也让他补填一位
      this.setData({
        stage: 'ambiguous',
        extraOpen: true,
        extraNote: d.reason || '',
        result: { msg: d.msg || '请再补填一位，确认是哪一支' },
      });
      wx.pageScrollTo({ scrollTop: 300, duration: 200 });
      return;
    }

    this.setData({
      stage: 'nomatch',
      result: {
        msg: d.msg || '没匹配上',
        hint: (d.fallback && d.fallback.hint) || '',
      },
    });
    wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },

  _handleError(e) {
    // 40901：这一位已经被别人认领了。不先到先得，交给房长核实。
    if (e.code === 40901) {
      const d = e.data || {};
      this.setData({
        stage: 'nomatch',
        result: {
          msg: '这一位已经被人认领了',
          hint: d.hint || '如果您确认这是您本人，可以请房长核实。',
          conflict: true,
        },
      });
      return;
    }

    // 42900：答题次数用完了 / 连续答错到上限
    // 2026-10-29：后端现在会告诉我们要等多久，别再写"稍后再试"让用户盲猜。
    // 而且真的能提前解开 —— 理事会成员在工作台一键解禁，提示要指向这个动作。
    if (e.code === 42900) {
      const d = e.data || {};
      const wait = waitText(d.retryAfterSeconds);
      this.setData({
        stage: 'nomatch',
        result: {
          msg: e.message || '尝试次数过多',
          hint: wait
            ? `${wait}可以再试。急着用的话，请族里的理事会成员在「我的 → 理事会工作台」里帮您解开，不用等满一小时。`
            : '请族里的理事会成员在「我的 → 理事会工作台」里帮您解开，或稍后再试。',
        },
      });
      return;
    }

    if (e.code === 40100) {
      wx.showToast({ title: '请重新进入小程序再试', icon: 'none' });
      return;
    }

    wx.showToast({ title: e.message || '提交失败', icon: 'none', duration: 2600 });
  },

  onRetry() {
    this.setData({
      stage: 'quiz',
      relativeName: '',
      selfName: '',
      extraName: '',
      extraOpen: false,
      extraNote: '',
      result: null,
      canSubmit: false,
    });
    wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },

  onGoHome() {
    const g = getApp();
    g.globalData.treeDirty = true;
    wx.reLaunch({ url: '/pages/home/home' });
  },

  onGoFill() {
    const g = getApp();
    const r = this.data.result || {};
    g.globalData.treeDirty = true;
    const pid = r.personId || g.globalData.claimedPersonId;
    if (!pid) {
      this.onGoHome();
      return;
    }
    wx.reLaunch({ url: `/pages/fill/fill?personId=${pid}` });
  },
});
