// 树节点。只负责把一个节点画对，不做布局、不发请求。
const { nodeLabel, cnNumber, roleText } = require('../../utils/format');

Component({
  options: {
    // 节点多的时候（我的一家 30+ 个），浅比较能省掉不少重渲染
    pureDataPattern: /^_/,
  },

  properties: {
    node: {
      type: Object,
      value: {},
      observer() {
        this._refresh();
      },
    },
    selected: {
      type: Boolean,
      value: false,
      observer() {
        this._refresh();
      },
    },
  },

  data: {
    cls: '',
    label: '',
    genText: '',
    roleText: '',
    pendingText: '',
  },

  lifetimes: {
    attached() {
      this._refresh();
    },
  },

  methods: {
    _refresh() {
      const node = this.data.node || {};
      if (!node.id) return;

      const role = node.role || 1;
      const isExternal = role === 4 || role === 5;
      // 离异（1）/ 婚姻无效（3）用虚框。丧偶（2）不虚框——
      // 丧偶的配偶仍然是这一家的先人，只是人不在了（卡片本来就会转青绿）。
      const isDivorced =
        !!node.isSpouse && (node.endReason === 1 || node.endReason === 3);

      const cls = [
        isExternal ? 'tn-external' : '',
        node.living === 2 ? 'tn-deceased' : '',
        node.isSpouse ? 'tn-spouse' : '',
        // 夫妻卡片零间距紧贴，接缝那一侧要切平圆角，
        // 否则两个 6px 圆角对在一起会凹出一个小缺口，看着像"分开了"。
        node.flatRight ? 'tn-flat-r' : '',
        node.flatLeft ? 'tn-flat-l' : '',
        isDivorced ? 'tn-divorced' : '',
        node.isSelf ? 'tn-self' : '',
        // 自己在的节点已经有朱红框了，不再叠加金边，否则两种强调互相打架
        !node.isSelf && node.onPath ? 'tn-path' : '',
        this.data.selected ? 'tn-selected' : '',
      ]
        .filter(Boolean)
        .join(' ');

      // 外孙/女婿自带角色标签；本族成员不标，标了是噪音（树上 80% 都是本族）
      const showRole = isExternal;

      // 配偶节点写"第几任"比写"媳妇"有用：一位先人三位夫人时，
      // 「元配 / 继配 / 已离异」才是这一屏真正要传达的信息。
      let marryText = '';
      if (node.isSpouse && node.unionSeq) {
        if (node.endReason === 1) {
          marryText = '已离异';
        } else {
          const t = node.unionType;
          marryText =
            t === 2 ? '继配'
              : t === 3 ? '侧室'
                : t === 4 ? '入赘'
                  : t === 5 ? '招赘'
                    : node.unionSeq > 1 ? `第${cnNumber(node.unionSeq)}配` : '元配';
        }
      }

      const count = node.badge && node.badge.count;

      this.setData({
        cls,
        label: nodeLabel(node),
        genText: node.gen ? `${cnNumber(node.gen)}世` : '',
        roleText: marryText || (showRole ? roleText(role) : ''),
        pendingText: count ? String(count) : node.hasPending ? '补' : '',
      });
    },

    handleTap() {
      this.triggerEvent('nodetap', {
        id: this.data.node.id,
        node: this.data.node,
      });
    },
  },
});
