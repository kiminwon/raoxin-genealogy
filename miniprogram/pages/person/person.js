// 人物详情 / 编辑
//
// 只读视图的字段排布是刻意固定的顺序：字、号、排行 → 生卒 → 籍贯葬地 → 职业生平。
// 这正是老谱一条传记的书写顺序。长辈翻到这一页会觉得"这就是谱上的写法"，
// 而不是"数据库里的一行"。

const { get, patch } = require('../../utils/request');
const { genLabelOf, roleText, livingText, lifeSpan, dateText } = require('../../utils/format');

const LIVING_LABELS = ['在世', '已故', '不详'];
const PRIVACY_LABELS = ['公开', '族人可见', '近亲可见', '私密'];
const PRECISION_LABELS = ['只知年份', '年月', '年月日'];
const RANK_LABELS = [
  '长子', '次子', '三子', '四子', '五子', '六子',
  '长女', '次女', '三女', '幼子', '独子', '独女',
];

const SOURCE_TEXT = {
  1: '老谱录入',
  2: '族人填报',
  3: 'OCR 识别导入',
  4: '口述',
  5: 'AI 推断',
};

function today() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

/** "1965" / "1965-03" / "1965-03-12" → date picker 能接受的 YYYY-MM-DD */
function toPickerDate(s) {
  if (!s) return '';
  const t = String(s);
  if (t.length >= 10) return t.slice(0, 10);
  if (t.length === 7) return t + '-01';
  if (t.length === 4) return t + '-01-01';
  return '';
}

/** 反过来：按精度截断，用来判断"用户到底改了没有" */
function truncateByPrecision(s, precision) {
  if (!s) return null;
  const t = String(s);
  if (precision === 1) return t.slice(0, 4);
  if (precision === 2) return t.slice(0, 7);
  return t.slice(0, 10);
}

Page({
  data: {
    loading: true,
    errorText: '',
    personId: null,
    person: null,
    editing: false,
    submitting: false,
    // 当前聚焦的表单项（-1 = 无）。保存时拨回 -1 让输入框失焦，
    // 否则安卓上原生输入层会残留在保存后的页面上（同 landing 页的坑）。
    editFocusIdx: -1,

    genText: '',
    roleText: '',
    livingText: '',
    livingCls: 'tag-grey',
    lifeText: '',
    fields: [],
    relatives: [],
    biological: null,

    form: [],
    generationHint: null,
    today: today(),
  },

  onLoad(query) {
    const id = Number(query && query.id);
    if (!id) {
      this.setData({ loading: false, errorText: '缺少人物编号' });
      return;
    }

    this.setData({ personId: id });
    this.load(id).then((p) => {
      // 从卡片「补充信息」进来时直接开编辑
      if (p && query.mode === 'edit') {
        this.onStartEdit();
      }
    });
  },

  async load(id) {
    this.setData({ loading: true });

    try {
      const p = await get(`/person/${id}`, {}, { silent: true });

      this.setData({
        loading: false,
        person: p,
        genText: genLabelOf(p),
        roleText: roleText(p.lineageRole),
        livingText: livingText(p.livingStatus),
        livingCls: p.livingStatus === 2 ? 'tag-jade' : 'tag-grey',
        lifeText: lifeSpan(p),
        fields: this._buildFields(p),
        relatives: this._buildRelatives(p.relatives),
        biological: p.biological || null,
      });

      wx.setNavigationBarTitle({ title: p.displayName || '谱上记载' });
      return p;
    } catch (e) {
      this.setData({
        loading: false,
        errorText: e.message || '读取失败',
      });
      return null;
    }
  },

  _buildFields(p) {
    const out = [];
    const push = (label, v) => {
      if (v !== null && v !== undefined && v !== '') {
        out.push({ label, value: String(v) });
      }
    };

    push('字', p.courtesyName);
    push('号', p.artName);
    push('排行', p.rankInFamily);
    if (p.birth) push('出生', p.birth.display);
    if (p.death) push('去世', p.death.display);
    push('籍贯', p.nativePlace);
    push('葬地', p.burialPlace);
    push('墓葬', p.burialNote);
    push('职业', p.occupation);
    push('学历 / 功名', p.education);
    push('生平', p.biography);
    push('来源', SOURCE_TEXT[p.sourceType]);

    return out;
  },

  _buildRelatives(rel) {
    if (!rel) return [];

    const rows = [];
    if (rel.father) rows.push({ key: 'father', relation: '父', persons: [rel.father] });
    if (rel.mother) rows.push({ key: 'mother', relation: '母', persons: [rel.mother] });

    if (rel.spouses && rel.spouses.length) {
      rows.push({
        key: 'spouse',
        relation: rel.spouses.length > 1 ? '配偶' : '配偶',
        persons: rel.spouses,
      });
    }
    if (rel.children && rel.children.length) {
      rows.push({ key: 'child', relation: '子女', persons: rel.children });
    }
    if (rel.siblings && rel.siblings.length) {
      rows.push({ key: 'sibling', relation: '手足', persons: rel.siblings });
    }
    return rows;
  },

  // ============================================================
  // 编辑
  // ============================================================

  onStartEdit() {
    const p = this.data.person;
    if (!p) return;

    if (!p.myPermissions || !p.myPermissions.canEdit) {
      wx.showModal({
        title: '暂时不能修改',
        content: '您还没有这一位的编辑权限。可以先确认自己的身份，或请房长发一张编辑码给您。',
        confirmText: '去确认身份',
        success: (res) => {
          if (res.confirm) wx.navigateTo({ url: '/pages/claim/claim' });
        },
      });
      return;
    }

    this.setData({
      editing: true,
      form: this._buildForm(p),
      generationHint: null,
    });
  },

  onCancelEdit() {
    // 取消同样会销毁表单，同样要先失焦
    this.blurEditForm();
    this.setData({ editing: false, generationHint: null });
  },

  _buildForm(p) {
    const livingIdx = Math.max(0, (p.livingStatus || 3) - 1);
    const privIdx = Math.max(0, (p.privacyLevel || 2) - 1);
    const birthPrec = (p.birth && p.birth.precision) || 1;
    const deathPrec = (p.death && p.death.precision) || 1;

    const rankIdx = RANK_LABELS.indexOf(p.rankInFamily || '');

    return [
      { key: 'name', label: '姓名', type: 'text', value: p.name || '', placeholder: '如：陈启明' },
      { key: 'courtesyName', label: '字', type: 'text', value: p.courtesyName || '', optional: true },
      { key: 'artName', label: '号', type: 'text', value: p.artName || '', optional: true },
      {
        key: 'rankInFamily',
        label: '排行',
        type: 'picker',
        labels: ['（不填）'].concat(RANK_LABELS),
        index: rankIdx < 0 ? 0 : rankIdx + 1,
        optional: true,
      },
      {
        key: 'livingStatus',
        label: '在世状态',
        type: 'picker',
        labels: LIVING_LABELS,
        index: livingIdx,
      },
      {
        key: 'birthDate',
        label: '出生',
        type: 'date',
        value: toPickerDate(p.birth && p.birth.date),
        optional: true,
      },
      {
        key: 'birthDatePrecision',
        label: '出生日期精度',
        type: 'picker',
        labels: PRECISION_LABELS,
        index: Math.max(0, birthPrec - 1),
        optional: true,
      },
      {
        key: 'deathDate',
        label: '去世',
        type: 'date',
        value: toPickerDate(p.death && p.death.date),
        optional: true,
      },
      {
        key: 'deathDatePrecision',
        label: '去世日期精度',
        type: 'picker',
        labels: PRECISION_LABELS,
        index: Math.max(0, deathPrec - 1),
        optional: true,
      },
      { key: 'nativePlace', label: '籍贯', type: 'text', value: p.nativePlace || '', optional: true },
      { key: 'burialPlace', label: '葬地', type: 'text', value: p.burialPlace || '', optional: true },
      { key: 'occupation', label: '职业', type: 'text', value: p.occupation || '', optional: true },
      { key: 'education', label: '学历 / 功名', type: 'text', value: p.education || '', optional: true },
      {
        key: 'biography',
        label: '生平',
        type: 'textarea',
        value: p.biography || '',
        placeholder: '一句话也好。老谱上一段传记，往往就是这么来的。',
        optional: true,
      },
      {
        key: 'privacyLevel',
        label: '谁能看到',
        type: 'picker',
        labels: PRIVACY_LABELS,
        index: privIdx,
      },
    ];
  },

  onInput(e) {
    this._setFormValue(e.currentTarget.dataset.index, e.detail.value);
  },

  onDate(e) {
    this._setFormValue(e.currentTarget.dataset.index, e.detail.value);
  },

  onPicker(e) {
    const i = e.currentTarget.dataset.index;
    const idx = Number(e.detail.value);

    // 排行要同时改 index 和值，因为它存的不是枚举序号
    const item = this.data.form[i];
    if (item.key === 'rankInFamily') {
      const label = item.labels[idx];
      this.setData({
        [`form[${i}].index`]: idx,
        [`form[${i}]._picked`]: idx === 0 ? '' : label,
      });
      return;
    }
    this.setData({ [`form[${i}].index`]: idx });
  },

  _setFormValue(i, value) {
    this.setData({ [`form[${i}].value`]: value });
  },

  _collect() {
    const p = this.data.person;
    const form = this.data.form;
    const byKey = {};
    form.forEach((f) => {
      byKey[f.key] = f;
    });

    const living = byKey.livingStatus.index + 1;
    const name = (byKey.name.value || '').trim();

    const changed = {};

    const put = (key, value, oldValue) => {
      if (value === oldValue) return;
      if ((value === '' || value === null) && (oldValue === '' || oldValue === null)) return;
      changed[key] = value;
    };

    put('name', name, p.name || '');
    put('courtesyName', (byKey.courtesyName.value || '').trim(), p.courtesyName || '');
    put('artName', (byKey.artName.value || '').trim(), p.artName || '');

    const rank = byKey.rankInFamily._picked;
    if (rank !== undefined) {
      put('rankInFamily', rank, p.rankInFamily || '');
    }

    put('livingStatus', living, p.livingStatus || 0);

    // 日期：按精度截断后再比，避免"只改了精度但日期本身没动"被误判成改动
    const birthPrec = byKey.birthDatePrecision.index + 1;
    const birthVal = truncateByPrecision(byKey.birthDate.value, birthPrec);
    const oldBirth = p.birth ? p.birth.date : null;
    if (birthVal !== oldBirth) {
      if (birthVal) changed.birthDate = birthVal;
      changed.birthDatePrecision = birthPrec;
    } else if (birthPrec !== ((p.birth && p.birth.precision) || 1)) {
      changed.birthDatePrecision = birthPrec;
    }

    const deathPrec = byKey.deathDatePrecision.index + 1;
    const deathVal = truncateByPrecision(byKey.deathDate.value, deathPrec);
    const oldDeath = p.death ? p.death.date : null;
    if (deathVal !== oldDeath) {
      if (deathVal) changed.deathDate = deathVal;
      changed.deathDatePrecision = deathPrec;
    } else if (deathPrec !== ((p.death && p.death.precision) || 1)) {
      changed.deathDatePrecision = deathPrec;
    }

    put('nativePlace', (byKey.nativePlace.value || '').trim(), p.nativePlace || '');
    put('burialPlace', (byKey.burialPlace.value || '').trim(), p.burialPlace || '');
    put('occupation', (byKey.occupation.value || '').trim(), p.occupation || '');
    put('education', (byKey.education.value || '').trim(), p.education || '');
    put('biography', (byKey.biography.value || '').trim(), p.biography || '');
    put('privacyLevel', byKey.privacyLevel.index + 1, p.privacyLevel || 0);

    return { changed, name, living };
  },

  /** 表单项聚焦时记下是哪一个（受控 focus 的一半：状态跟真实焦点对齐） */
  onEditFocus(e) {
    const idx = Number(e.currentTarget.dataset.index);
    if (this.data.editFocusIdx !== idx) this.setData({ editFocusIdx: idx });
  },

  /** 保存前把输入框失焦 —— 原因同 landing/claim 页：安卓上原生输入层会残留 */
  blurEditForm() {
    if (this.data.editFocusIdx !== -1) this.setData({ editFocusIdx: -1 });
    try {
      wx.hideKeyboard();
    } catch (e) {
      // 没有键盘时不报错就行
    }
  },

  async onSubmit() {
    if (this.data.submitting) return;

    // 保存会退出编辑态、销毁表单：先失焦（见 blurEditForm 的说明）
    this.blurEditForm();

    const { changed, name, living } = this._collect();

    if (!name) {
      wx.showToast({ title: '姓名不能为空', icon: 'none' });
      return;
    }
    if (living === 2 && !changed.deathDate && !(this.data.person.death)) {
      // 不阻断，只是提醒——很多人确实不知道先人哪年走的
      wx.showToast({ title: '已故先人可不填去世时间', icon: 'none', duration: 2000 });
    }

    if (!Object.keys(changed).length) {
      wx.showToast({ title: '还没有改动', icon: 'none' });
      return;
    }

    this.setData({ submitting: true });

    try {
      const data = await patch(
        `/person/${this.data.personId}`,
        { fields: changed, submitNote: '小程序端补充' },
        { silent: true }
      );

      // 落在自己 edit_scope 内 → 立刻生效；超出范围 → 进房长的待办。
      // 两种情况的文案必须分开，否则用户会以为改完还要等人批。
      this._lastAutoMerged = !!data.autoMerged;
      this._lastStatusText = data.statusText || '';

      // 字辈提示：只提示，不阻断（决策第 1 条）
      const hint = data.generationHint;
      if (hint && hint.message) {
        this.setData({ generationHint: hint, submitting: false });
        wx.showModal({
          title: '字辈提示',
          content: hint.message + '\n\n（这只是参考，不影响您的提交）',
          showCancel: false,
          confirmText: '知道了',
          success: () => this._afterSubmit(),
        });
        return;
      }

      this._afterSubmit();
    } catch (e) {
      this.setData({ submitting: false });
      if (e.code === 40301) {
        wx.showModal({
          title: '没有编辑权限',
          content: e.message || '这一位不在您被授权的范围内。',
          showCancel: false,
        });
        return;
      }
      wx.showToast({ title: e.message || '提交失败', icon: 'none', duration: 2500 });
    }
  },

  _afterSubmit() {
    const g = getApp();
    g.globalData.treeDirty = true;

    this.setData({ submitting: false, editing: false, generationHint: null });

    wx.showToast({
      title: this._lastAutoMerged ? '已生效，树上马上能看到' : '已提交，等房长确认',
      icon: 'none',
      duration: 2200,
    });
    setTimeout(() => wx.navigateBack({ delta: 1 }), 1400);
  },

  // ============================================================
  // 跳转
  // ============================================================

  onGoPerson(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    wx.navigateTo({ url: `/pages/person/person?id=${id}` });
  },

  onGoFill() {
    wx.navigateTo({ url: `/pages/fill/fill?personId=${this.data.personId}` });
  },

  onClaim() {
    wx.navigateTo({ url: `/pages/claim/claim?personId=${this.data.personId}` });
  },

  /**
   * 「看他在树上的位置」→ 全族大宏图，并定位高亮这个人。
   *
   * 原来只是 reLaunch 回首页：「我的一家」的卡片流只包含"我这一支"，
   * 点别人（比如刚给父亲添的兄弟）根本不在流里，看起来就是"没反应"。
   * 全族的谱页里人人都在，所以这里一定要带 view=overview + focus。
   * focus 只是滚动/高亮的意图，不携带任何权限。
   */
  onBackToTree() {
    const id = this.data.personId || (this.data.person && this.data.person.id);
    const q = id ? `?view=overview&focus=${id}` : '?view=overview';
    wx.reLaunch({ url: '/pages/home/home' + q });
  },
});
