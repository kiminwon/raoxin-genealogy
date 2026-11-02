// 待补全清单（接力修谱的入口）
//
// 核心交互契约：一次只问一个问题，答完立刻给正反馈，然后自动问下一个。
// 不做"列表 + 批量提交"——那是给数据录入员用的，不是给族里的大伯用的。

const { get, post } = require('../../utils/request');
const { cnNumber } = require('../../utils/format');

// 清单里可能出现的字段。type 决定端上用哪种输入控件。
const FIELD_SPEC = {
  name: { label: '姓名', type: 'text', placeholder: '如：陈启明' },
  partnerName: { label: '配偶姓名', type: 'text', placeholder: '如：李秀英' },
  surname: { label: '姓氏', type: 'text', placeholder: '如：陈' },
  gender: { label: '性别', type: 'picker', labels: ['男', '女'], values: [1, 2] },
  livingStatus: {
    label: '在世状态',
    type: 'picker',
    labels: ['在世', '已故', '不详'],
    values: [1, 2, 3],
    // 问活人为什么不填配偶是常态，问这个没必要，默认在世
    default: 0,
  },
  birthDate: { label: '出生日期', type: 'date' },
  deathDate: { label: '去世日期', type: 'date' },
  marriageDate: { label: '结婚时间', type: 'date' },
  burialPlace: { label: '葬地', type: 'text', placeholder: '如：茶亭镇马鞍山' },
  occupation: { label: '职业', type: 'text', placeholder: '如：教师' },
  nativePlace: { label: '籍贯', type: 'text', placeholder: '如：广信区茶亭镇' },
};

function today() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

Page({
  data: {
    loading: true,
    forbidden: false,
    forbiddenMsg: '',
    done: false,
    doneTitle: '',
    doneSub: '',
    doneMore: false,     // done 页是否还要给「接着补」（服务端复核后仍有余项）
    leftCount: 0,        // 服务端复核出来的剩余项数
    submitting: false,
    flashShow: false,

    personId: null,
    displayName: '',
    task: null,
    queue: [],          // 本地排队的后续任务
    fields: [],
    preview: '',
    prefillTip: '',
    percent: 0,
    total: 0,
    index: 0,
    autoFocus: false,   // 故意不自动弹键盘：先让他看清"为什么问"
    today: today(),
  },

  onLoad(query) {
    const pid = Number(query && query.personId);
    this.setData({ personId: pid || null });
    this._initialTotal = 0;
    this._lastAutoMerged = false;
    this.load();
  },

  async load() {
    const g = getApp();
    let pid = this.data.personId;

    // 没传就默认填自己
    if (!pid) {
      pid = g.globalData.user && g.globalData.user.claimedPersonId;
    }

    if (!pid) {
      this.setData({
        loading: false,
        forbidden: true,
        forbiddenMsg: '还没有确认「我」是哪一位。先答两个名字对上自己，才能补自己的信息。',
      });
      return;
    }

    this.setData({ personId: pid, loading: true });

    try {
      const d = await get(`/person/${pid}/missing`, {}, { silent: true });

      const items = d.items || [];
      this._initialTotal = d.missingCount || 0;

      this.setData({
        loading: false,
        displayName: d.displayName || '您',
        total: d.missingCount || 0,
        percent: this._calcPercent(d.missingCount || 0),
        queue: items.slice(1),
      });

      if (!items.length) {
        this._showDone(d);
        return;
      }
      this._applyTask(items[0], 0, d.missingCount || 0);
    } catch (e) {
      // 40300 = 无权限看这份清单（后端 forbidden），40301 = 不在编辑范围。
      // 原来只认 40301，40300 就掉进下面的"读取失败"——用户看着一个
      // 看不懂的技术提示，也不知道该去补自己的。
      if (e.code === 40300 || e.code === 40301) {
        this.setData({
          loading: false,
          forbidden: true,
          forbiddenMsg: e.message || '待补全清单只对本人开放——别人缺什么，不该由您代填。',
        });
        return;
      }
      this.setData({ loading: false, done: true, doneTitle: '读取失败', doneSub: e.message || '' });
    }
  },

  _calcPercent(remaining) {
    if (!this._initialTotal) return 0;
    const doneCount = this._initialTotal - remaining;
    return Math.max(0, Math.min(100, Math.round((doneCount / this._initialTotal) * 100)));
  },

  /**
   * 把一个任务变成"问题 + 表单 + 前后对比"。
   */
  _applyTask(task, index, remaining) {
    const fields = this._buildFields(task);

    // 新建类任务：把系统已知的部分（姓氏、世次）说清楚，
    // 用户才知道自己填的这个会被记到哪儿去
    let tip = '';
    const pre = task.prefill || {};
    if (pre.surname || pre.generation) {
      const parts = [];
      if (pre.surname) parts.push(`${pre.surname}姓`);
      if (pre.generation) parts.push(`第${cnNumber(pre.generation)}世`);
      if (parts.length) {
        tip = `系统会记为：${parts.join('、')}。如果不对，可以在填完后到详情页改。`;
      }
    }

    this.setData({
      task,
      fields,
      preview: '',
      prefillTip: tip,
      index,
      total: remaining,
      percent: this._calcPercent(remaining),
    });
  },

  _buildFields(task) {
    const pre = task.prefill || {};
    const out = [];

    // 改字段：只问这一个
    if (task.type === 'update_field') {
      const spec = FIELD_SPEC[task.field] || { label: task.label || '内容', type: 'text' };
      out.push(this._oneField(task.field, spec, true));
      return out;
    }

    // 新建人物 / 婚姻：必填在前，选填在后
    const keys = [];
    (task.required || []).forEach((k) => keys.push({ key: k, required: true }));
    (task.optional || []).forEach((k) => keys.push({ key: k, required: false }));

    keys.forEach(({ key, required }) => {
      const spec = FIELD_SPEC[key];
      if (!spec) return;
      out.push(this._oneField(key, spec, required));
    });

    // 性别如果 prefill 已经定了，就不必再问
    if (pre.gender && !out.some((f) => f.key === 'gender')) {
      // 不显示，直接由 prefill 带上
    }

    return out;
  },

  _oneField(key, spec, required) {
    const f = {
      key,
      label: spec.label,
      type: spec.type,
      value: '',
      optional: !required,
      placeholder: spec.placeholder || '',
    };

    if (spec.type === 'picker') {
      f.labels = spec.labels;
      f.values = spec.values;
      f.index = spec.default || 0;
    }
    return f;
  },

  // ============================================================
  // 输入
  // ============================================================

  onInput(e) {
    const i = e.currentTarget.dataset.index;
    const value = e.detail.value;

    this.setData({ [`fields[${i}].value`]: value });

    // 姓名这一格要实时反映到"填完之后"那一行——
    // 用户一边打字一边看着"配李氏"变成"李秀英"，这才是那个瞬间
    const f = this.data.fields[i];
    if (f && (f.key === 'name' || f.key === 'partnerName')) {
      this.setData({ preview: value });
    }
  },

  onPick(e) {
    const i = e.currentTarget.dataset.index;
    this.setData({ [`fields[${i}].index`]: Number(e.detail.value) });
  },

  // ============================================================
  // 提交
  // ============================================================

  async onSubmit() {
    if (this.data.submitting) return;

    const task = this.data.task;
    if (!task) return;

    const values = {};
    let firstValue = '';

    for (const f of this.data.fields) {
      let v;
      if (f.type === 'picker') {
        v = f.values[f.index];
      } else {
        v = (f.value || '').trim();
      }
      if (v === '' || v === null || v === undefined) continue;
      values[f.key] = v;
      if (!firstValue) firstValue = String(v);
    }

    if (!Object.keys(values).length) {
      wx.showToast({ title: '填一项就行', icon: 'none' });
      return;
    }

    this.setData({ submitting: true });

    try {
      const r = await post(
        `/person/${this.data.personId}/missing/submit`,
        { key: task.key, values },
        { silent: true }
      );
      // 已经认领过自己之后，自己这一支的填写是**立刻生效**的（autoMerged）。
      // 只有超出范围的改动才进房长的待办。末尾的文案得跟着这个走，
      // 否则会出现"明明已经落进谱里了，却告诉用户'已提交，等房长确认'"。
      this._lastAutoMerged = !!(r && r.autoMerged);

      this._flash();
      this._next(firstValue);
    } catch (e) {
      this.setData({ submitting: false });
      wx.showToast({ title: e.message || '没提交上去，再试一次', icon: 'none', duration: 2500 });
    }
  },

  _flash() {
    this.setData({ flashShow: true });
    setTimeout(() => this.setData({ flashShow: false }), 700);
  },

  /**
   * 进入下一问。
   *
   * 本地队列空了但服务端说还有，就重拉一次——
   * 因为答完一条后，新的缺口可能才刚出现（比如刚填了父亲，系统就发现还没有母亲）。
   */
  async _next(filledValue) {
    const queue = this.data.queue.slice();
    const remaining = Math.max(0, this.data.total - 1);

    if (queue.length) {
      this.setData({ submitting: false });
      this._applyTask(queue[0], this.data.index + 1, remaining);
      this.setData({ queue: queue.slice(1) });
      return;
    }

    if (remaining > 0) {
      this.setData({ submitting: false, loading: true });
      await this.load();
      // load 会重置 percent/index，这里只把"已答"的口径保留住
      return;
    }

    this.setData({ submitting: false });
    this._showDone({
      displayName: this.data.displayName,
      lastFilled: filledValue,
    });
  },

  /**
   * 「没有」也是一种回答。
   *
   * 带 skipLabel 的任务（未婚 / 暂无子女）跳过时会**落库**：
   * 原来这里只是本地跳到下一条——用户明确说了"没有"，下次进来照旧被问，
   * 比不给按钮更烦，而且进度永远到不了 100%。
   * 没有 skipLabel 的任务保持原语义："这条先不填"，下次还会出现。
   */
  async onSkip() {
    const task = this.data.task || {};
    if (this.data.submitting) return;

    if (task.key && task.skipLabel) {
      this.setData({ submitting: true });
      try {
        await post(
          `/person/${this.data.personId}/missing/submit`,
          { key: task.key, skip: true },
          { silent: true }
        );
      } catch (e) {
        this.setData({ submitting: false });
        wx.showToast({
          title: (e && e.message) || '没能保存，请重试',
          icon: 'none',
        });
        return;
      }
      this.setData({ submitting: false });
      // 落库后这一项不再是缺口，本地推进一格即可；
      // 不要走 load()——它会把 percent 按"新的 total"重算，进度条会跳回 0。
      await this._next(task.skipLabel);
      return;
    }

    const queue = this.data.queue.slice();
    const remaining = Math.max(0, this.data.total - 1);

    if (queue.length) {
      this._applyTask(queue[0], this.data.index + 1, remaining);
      this.setData({ queue: queue.slice(1) });
      return;
    }

    if (remaining > 0) {
      this.load();
      return;
    }

    this._showDone({ displayName: this.data.displayName });
  },

  /**
   * 「补完了」这句话必须由服务端来说。
   *
   * 原来这里是端上自己判：本地队列空了、remaining 归零，就宣布
   * 「缺口都补上了」。但「以后再说」是**不落库**的——用户跳过的每一条，
   * 服务端都还记着。于是回到详情页照样显示"还差 1 项"，
   * 而补全页刚跟他说"都补上了"。同一件事算两遍，这已经是第四次。
   *
   * 现在：结束前重新问一次服务端，按它给的数字出文案。
   * 复核失败也不许谎报——只说"这一轮先到这儿"。
   */
  async _showDone(d) {
    const fallback = (d && d.displayName) || this.data.displayName || '您';
    let name = fallback;
    let left = null;

    try {
      const fresh = await get(`/person/${this.data.personId}/missing`, {}, { silent: true });
      name = fresh.displayName || fallback;
      left = Number(fresh.missingCount || 0);
    } catch (e) {
      left = null;
    }

    if (left === null) {
      this.setData({
        done: true,
        doneMore: false,
        leftCount: 0,
        doneTitle: '这一轮先到这儿',
        doneSub: '刚才填的已经记上了。剩下的下次进来会接着问。',
      });
      return;
    }

    if (left > 0) {
      this.setData({
        done: true,
        doneMore: true,
        leftCount: left,
        doneTitle: `还有 ${left} 项没填`,
        doneSub: '刚才没填的那几条还留着，想起来了随时接着补。',
      });
      return;
    }

    this.setData({
      done: true,
      doneMore: false,
      leftCount: 0,
      doneTitle: `${name}这一格已经补全了`,
      doneSub: this._lastAutoMerged
        ? '已经落进谱里了，家族树上马上就能看到。'
        : '这些信息会送到房长那里确认，通过后就会出现在家族树上。',
    });
  },

  /** done 页上的「接着补」：重新拉一次清单，从这里继续问 */
  onContinue() {
    this.setData({ done: false, doneMore: false });
    this._initialTotal = 0;
    this.load();
  },

  onGoHome() {
    const g = getApp();
    g.globalData.treeDirty = true;
    wx.reLaunch({ url: '/pages/home/home' });
  },
});
