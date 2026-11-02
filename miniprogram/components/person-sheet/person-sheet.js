// 底部半屏卡片：展示一个人，并提供"下一步动作"。
//
// 这里有个刻意的取舍：卡片不发任何请求。
// 它拿到的 person 由页面传进来，可能是树节点数据（字段少），
// 也可能是详情接口的完整数据。组件按"有什么显示什么"渲染，
// 于是同一个卡片在两种数据源下都能用，页面不用为它准备两套。
const { genLabelOf, roleText, livingText, lifeSpan, dateText } = require('../../utils/format');

Component({
  options: {
    pureDataPattern: /^_/,
  },

  properties: {
    show: {
      type: Boolean,
      value: false,
    },
    person: {
      type: Object,
      value: null,
      observer() {
        this._refresh();
      },
    },
    loading: {
      type: Boolean,
      value: false,
    },
  },

  data: {
    genText: '',
    roleText: '',
    livingText: '',
    livingCls: 'tag-grey',
    lifeText: '',
    fields: [],
    relativeChips: [],
  },

  lifetimes: {
    attached() {
      this._refresh();
    },
  },

  methods: {
    _refresh() {
      const p = this.data.person;
      if (!p || !p.id) {
        this.setData({ fields: [], relativeChips: [] });
        return;
      }

      const living = p.livingStatus;
      this.setData({
        genText: genLabelOf(p),
        roleText: roleText(p.lineageRole),
        livingText: livingText(living),
        // 在世用中性灰（是默认状态，不需要被强调）；已故用青绿，庄重而不悲情
        livingCls: living === 2 ? 'tag-jade' : 'tag-grey',
        lifeText: lifeSpan(p),
        fields: this._buildFields(p),
        relativeChips: this._buildChips(p),
      });
    },

    _buildFields(p) {
      const out = [];

      const push = (label, value) => {
        if (value !== null && value !== undefined && value !== '') {
          out.push({ label, value: String(value) });
        }
      };

      if (p.courtesyName) push('字', p.courtesyName);
      if (p.artName) push('号', p.artName);
      if (p.rankInFamily) push('排行', p.rankInFamily);

      // 出生已并进 lifeText，这里只在有卒、葬、籍贯时补
      if (p.death && living !== 1) push('卒于', dateText(p.death));
      if (p.nativePlace) push('籍贯', p.nativePlace);
      if (p.burialPlace) push('葬地', p.burialPlace);
      if (p.occupation) push('职业', p.occupation);
      if (p.education) push('学历', p.education);

      // 生平单独占一行太长，这里截断，详情页给完整的
      if (p.biography) {
        const bio = String(p.biography);
        push('生平', bio.length > 40 ? bio.slice(0, 40) + '…' : bio);
      }

      // 母子关系：老谱常见"配李氏"，这里明确标出来，是待补全清单的入口之一
      const rel = p.relatives;
      if (rel) {
        if (!rel.father && p.generation > 1) push('父', '谱中未记录');
        if (rel.mother && !rel.mother.name) push('母', rel.mother.displayName + '（缺名）');
      }

      return out;
    },

    _buildChips(p) {
      const rel = p.relatives;
      if (!rel) return [];

      const chips = [];
      const add = (key, label, person) => {
        if (!person) return;
        chips.push({
          key,
          rel: label,
          id: person.id,
          name: person.displayName || person.name || '未详',
        });
      };

      add('father', '父', rel.father);
      add('mother', '母', rel.mother);

      (rel.spouses || []).slice(0, 2).forEach((s, i) => {
        add('spouse' + i, rel.spouses.length > 1 ? (i === 0 ? '元配' : '继配') : '配偶', s);
      });

      (rel.children || []).slice(0, 3).forEach((c, i) => add('child' + i, '子女', c));
      (rel.siblings || []).slice(0, 2).forEach((s, i) => add('sibling' + i, '手足', s));

      return chips;
    },

    onClose() {
      this.triggerEvent('close');
    },

    onMaskTap() {
      this.triggerEvent('close');
    },

    onDetail() {
      this.triggerEvent('action', { type: 'detail', personId: this.data.person.id });
    },

    onEdit() {
      this.triggerEvent('action', { type: 'edit', personId: this.data.person.id });
    },

    onClaim() {
      this.triggerEvent('action', { type: 'claim', personId: this.data.person.id });
    },

    onFillMine() {
      this.triggerEvent('action', { type: 'fillmine', personId: this.data.person.id });
    },

    onChipTap(e) {
      const id = e.currentTarget.dataset.id;
      if (!id) return;
      this.triggerEvent('chiptap', { personId: id });
    },

    // 阻止遮罩层滚动穿透
    noop() {},
  },
});
