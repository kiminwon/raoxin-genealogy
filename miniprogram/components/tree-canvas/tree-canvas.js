// 全族大宏图的 Canvas 树状图（2026-10-29 第四版）。
//
// 演进：
//   v1 点位骨架——只画色块，认不出人；
//   v2 按世卡片列表——有名字，但没有"谁是谁的儿子"，不是树；
//   v3 可缩放的谱牌树——放大看名字、缩小看全貌，但缩放对长辈是额外负担；
//   v4（本版）**固定 1:1、只平移**——卡牌 88×96 屏幕像素，名字永远可读，
//      上下左右滑动看全族；最左侧一列「世代丁口」固定不动（横向滑动时不跑），
//      纵向吸附到各代那一行，滑到哪一代就标到哪一代。
//
// 三条硬约束（都有代价，别再改回去）：
//   1. 不缩放。scale 恒 1，没有 pinch，没有 fitAll——地形图式的"缩到 4% 看全貌"
//      在这台设备上等于一片糊点（564 人实测整屏适配 3.9%、字号 0.47px）。
//   2. 左栏是**屏幕坐标**绘制的固定栏，内容先画、左栏后画盖在上面，
//      滑动时卡牌从栏下穿过，栏本身纹丝不动。
//   3. 只画视口内的节点与线段。564 个节点全画每帧 10ms+，裁剪后可见的
//      通常只有二三十个，滑动是 60fps。
//
// 配色同黛青谱衣：在世黛青、已故灰纸、自己朱砂边、从详情页定位来的烫金圈。

const EDGE = {
  PARENT: 1,
  MARRY: 2,
  ADOPT: 3,
  EXT: 4,
};

// 2026-11-02 从 88×96 缩到 64×72：一屏横向要放下 4 张牌
//（375 − 62 丁口栏 = 313；4×64 + 3×16 间距 = 304，正好）。
// 与服务端 tree_node_width/height、tree_sibling_gap 保持一致。
const NODE_W = 64;
const NODE_H = 72;

// ---- 配色 ----
const CARD_LIVING_BG = '#FDFBF4';
const CARD_DEAD_BG = '#EFECE3';
const CARD_LIVING_EDGE = '#2B574B';
const CARD_DEAD_EDGE = '#C9C3B4';
const BAR_LIVING = '#2B574B';
const BAR_DEAD = '#AFA998';
const NAME_LIVING = '#23262B';
const NAME_DEAD = '#8D887B';
const SELF_EDGE = '#A6321E';
const FOCUS_RING = '#C9A961';
const RAIL_BG = '#F6F3EA';
const RAIL_LINE = '#E0D9C8';
const RAIL_TEXT = '#4E5459';
const RAIL_STRONG = '#2B574B';
const FONT_SERIF = '"STSong", "SimSun", "Source Han Serif SC", serif';

const NAME_FONT = 11;      // 2026-11-02 从 22 缩半：牌小了，名字跟着小一半
const RAIL_W = 62;         // 左栏宽度（px）
const PAD = 20;            // 内容离屏幕边缘至少留这么多

Component({
  properties: {
    nodes: {
      type: Array,
      value: [],
      observer() {
        this._dirty();
      },
    },
    edges: {
      type: Array,
      value: [],
      observer() {
        this._dirty();
      },
    },
    // 每一代的丁口：[{gen, count, male, female}]，服务端算好下发。
    // 只数男丁和本族女儿，媳妇/女婿/外孙不计（谱例口径）。
    stats: {
      type: Array,
      value: [],
      observer() {
        this._schedule();
      },
    },
    // 从详情页「看他在树上的位置」跳进来要定位并高亮的人
    focusId: {
      type: Number,
      value: null,
      observer(v) {
        if (v) this._focusPerson(v);
      },
    },
    // 世界坐标尺寸（服务端算出来的画布大小）
    cw: {
      type: Number,
      value: 0,
      observer() {
        // 换族 / 换锚点后画布尺寸会变，必须重新落位
        this._placed = false;
        this._dirty();
      },
    },
    ch: {
      type: Number,
      value: 0,
      observer() {
        this._placed = false;
        this._dirty();
      },
    },
  },

  data: {
    showHint: true,
    showEmpty: false,
    emptyText: '这个宗族还没有录入人物',
  },

  lifetimes: {
    attached() {
      // 视图平移只存内存：touchmove 每秒 60 次，setData 会直接卡死。
      this._view = { x: 0, y: 0 };
      this._segs = [];
      this._ready = false;
      this._pendingDraw = false;
      this._placed = false;
    },
    detached() {
      if (this._hintTimer) clearTimeout(this._hintTimer);
      this._canvas = null;
      this._ctx = null;
    },
    ready() {
      // ready 是**生命周期**，不是普通方法：写在 methods 里永远不会被调用，
      // 表现是"切到大宏图一片空白"，且没有任何报错。
      this._init();
    },
  },

  methods: {
    // ============================================================
    // 初始化
    // ============================================================

    _init() {
      this.createSelectorQuery()
        .select('#ov')
        .fields({ node: true, size: true })
        .exec((res) => {
          const item = res && res[0];
          if (!item || !item.node) return;

          const canvas = item.node;
          const ctx = canvas.getContext('2d');
          let dpr = 2;
          try {
            const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
            dpr = info.pixelRatio || 2;
          } catch (e) {
            dpr = 2;
          }

          canvas.width = Math.max(1, Math.round(item.width * dpr));
          canvas.height = Math.max(1, Math.round(item.height * dpr));

          this._canvas = canvas;
          this._ctx = ctx;
          this._dpr = dpr;
          this._w = item.width;
          this._h = item.height;
          this._ready = true;

          this._place();
          this._buildSegments();
          this._draw();

          // 提示露 5 秒就收，别长期压着图
          this._hintTimer = setTimeout(() => {
            this.setData({ showHint: false });
          }, 5000);
        });
    },

    _dirty() {
      if (!this._ready) return;
      this._buildSegments();
      if (!this._placed) this._place();
      this._schedule();
    },

    /**
     * 落位：把 focus 那个人（没有就从始祖）放到可用区中央。
     * 不缩放，所以这里只算平移量。
     */
    _place() {
      const nodes = this.data.nodes || [];
      if (!this._w || !this._h) return;
      if (!nodes.length) {
        this.setData({ showEmpty: true });
        return;
      }
      this.setData({ showEmpty: false });

      let target = null;
      if (this.data.focusId) {
        target = nodes.find((n) => n.id === this.data.focusId) || null;
      }
      if (!target) {
        // 始祖 = 世代最小、横坐标最小（长房长子那一脉）
        let bestGen = Infinity;
        for (const n of nodes) if (n.gen < bestGen) bestGen = n.gen;
        let bestX = Infinity;
        for (const n of nodes) {
          if (n.gen === bestGen && (n.x || 0) < bestX) {
            bestX = n.x || 0;
            target = n;
          }
        }
      }

      const cx = (target ? target.x : 0) + NODE_W / 2;
      const cy = (target ? target.y : 0) + NODE_H / 2;
      this._view = {
        x: RAIL_W + (this._w - RAIL_W) / 2 - cx,
        y: this._h / 2 - cy,
      };
      this._placed = true;
      this._clamp();
    },

    /** 平移边界：内容小于视口就居中并锁死；否则允许留 PAD 白边 */
    _clamp() {
      const cw = this.data.cw || 0;
      const ch = this.data.ch || 0;
      const v = this._view;
      if (!this._w || !this._h) return;

      const availW = this._w - RAIL_W;
      if (cw <= availW - PAD * 2) {
        v.x = RAIL_W + (availW - cw) / 2;
      } else {
        const minX = RAIL_W + PAD - cw;
        const maxX = this._w - PAD;
        v.x = Math.max(minX, Math.min(maxX, v.x));
      }

      if (ch <= this._h - PAD * 2) {
        v.y = (this._h - ch) / 2;
      } else {
        const minY = PAD - ch;
        const maxY = this._h - PAD;
        v.y = Math.max(minY, Math.min(maxY, v.y));
      }
    },

    /** 定位并高亮某人（详情页「看他在树上的位置」进来） */
    _focusPerson(id) {
      const n = (this.data.nodes || []).find((x) => x.id === id);
      if (!n || !this._ready) return;
      this._view = {
        x: RAIL_W + (this._w - RAIL_W) / 2 - (n.x + NODE_W / 2),
        y: this._h / 2 - (n.y + NODE_H / 2),
      };
      this._clamp();
      this._schedule();
    },

    /**
     * 公开定位：外部（首页搜索）在人已在图上、focusId 属性值没变时
     * 调这一枪——observer 只在值变化时触发，重复定位同一个人得走这里。
     */
    focusOn(id) {
      this._focusPerson(id);
    },

    // ============================================================
    // 几何：把 edges 翻译成线段
    // ============================================================
    //
    // 规则与 tree-lines.js 完全一致（父→中线→子，正交折线），
    // 只是那边输出 CSS 字符串给 view，这边输出坐标给 canvas。

    _buildSegments() {
      const nodes = this.data.nodes || [];
      const edges = this.data.edges || [];
      const map = {};
      for (const n of nodes) map[n.id] = n;

      const spouse = {};
      for (const e of edges) {
        if (e.type !== EDGE.MARRY) continue;
        if (!map[e.from] || !map[e.to]) continue;
        spouse[e.from] = e.to;
        spouse[e.to] = e.from;
      }

      const ax = (n) => {
        const sp = map[spouse[n.id]];
        const nw = n.w || NODE_W;
        if (sp && Math.abs(sp.y - n.y) < 2) {
          const L = n.x <= sp.x ? n : sp;
          const R = n.x <= sp.x ? sp : n;
          return (L.x + (L.w || NODE_W) + R.x) / 2;
        }
        return n.x + nw / 2;
      };

      const segs = [];
      for (const e of edges) {
        const a = map[e.from];
        const b = map[e.to];
        if (!a || !b) continue;

        if (e.type === EDGE.MARRY) {
          const L = a.x <= b.x ? a : b;
          const R = a.x <= b.x ? b : a;
          const x1 = L.x + (L.w || NODE_W);
          const x2 = R.x;
          if (x2 - x1 <= 0.5) continue;
          const y = L.y + (L.h || NODE_H) / 2;
          segs.push({ x1, y1: y, x2, y2: y, t: 'm' });
          continue;
        }

        const sx = ax(a);
        const sy = a.y + (a.h || NODE_H);
        const tx = ax(b);
        const ty = b.y;
        if (ty - sy <= 1) continue;

        const midY = sy + (ty - sy) / 2;
        segs.push({ x1: sx, y1: sy, x2: sx, y2: midY, t: e.type });
        if (Math.abs(tx - sx) > 1) {
          segs.push({ x1: sx, y1: midY, x2: tx, y2: midY, t: e.type });
        }
        segs.push({ x1: tx, y1: midY, x2: tx, y2: ty, t: e.type });
      }

      this._segs = segs;

      // 每代最上面那个节点的世界 y，左栏标签靠它对行
      const rows = {};
      for (const n of nodes) {
        if (rows[n.gen] == null || n.y < rows[n.gen]) rows[n.gen] = n.y;
      }
      this._genRows = rows;
    },

    // ============================================================
    // 绘制
    // ============================================================

    _schedule() {
      if (this._pendingDraw || !this._canvas) return;
      this._pendingDraw = true;
      const raf = this._canvas.requestAnimationFrame || ((cb) => setTimeout(cb, 16));
      raf(() => {
        this._pendingDraw = false;
        this._draw();
      });
    },

    _draw() {
      const ctx = this._ctx;
      if (!ctx) return;

      const dpr = this._dpr || 1;
      const v = this._view;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this._w, this._h);
      ctx.translate(v.x, v.y);

      // 可视区域（世界坐标），用来裁掉屏幕外的节点与线
      const wx0 = -v.x - NODE_W;
      const wx1 = this._w - v.x + NODE_W;
      const wy0 = -v.y - NODE_H;
      const wy1 = this._h - v.y + NODE_H;

      // ---- 连线（先画，节点压在上面）----
      ctx.lineWidth = 0.9;
      for (const s of this._segs) {
        if (Math.max(s.x1, s.x2) < wx0 || Math.min(s.x1, s.x2) > wx1) continue;
        if (Math.max(s.y1, s.y2) < wy0 || Math.min(s.y1, s.y2) > wy1) continue;
        ctx.beginPath();
        ctx.strokeStyle = s.t === 'm' ? 'rgba(184, 134, 11, 0.72)' : 'rgba(43, 87, 75, 0.34)';
        ctx.moveTo(s.x1, s.y1);
        ctx.lineTo(s.x2, s.y2);
        ctx.stroke();
      }

      // ---- 节点：谱牌卡片 ----
      const focusId = this.data.focusId;
      for (const n of (this.data.nodes || [])) {
        const w = n.w || NODE_W;
        const h = n.h || NODE_H;
        if (n.x + w < wx0 || n.x > wx1) continue;
        if (n.y + h < wy0 || n.y > wy1) continue;
        this._drawCard(ctx, n, n.id === focusId);
      }

      // ---- 左栏（屏幕坐标，最后画：卡牌从栏下滑过，栏不动）----
      this._drawRail();
    },

    /**
     * 一张谱牌：纸白圆角卡 + 顶部世色条 + 居中姓名。
     * 在世黛青、已故灰纸；自己朱砂边；focus 烫金外圈。
     */
    _drawCard(ctx, n, focused) {
      const w = n.w || NODE_W;
      const h = n.h || NODE_H;
      const dead = n.living === 2;

      if (focused) {
        ctx.fillStyle = FOCUS_RING;
        this._roundRect(ctx, n.x - 5, n.y - 5, w + 10, h + 10, 11);
        ctx.fill();
      }

      ctx.fillStyle = dead ? CARD_DEAD_BG : CARD_LIVING_BG;
      this._roundRect(ctx, n.x, n.y, w, h, 8);
      ctx.fill();

      ctx.lineWidth = n.isSelf ? 2.2 : 1.1;
      ctx.strokeStyle = n.isSelf ? SELF_EDGE : dead ? CARD_DEAD_EDGE : CARD_LIVING_EDGE;
      ctx.stroke();

      // 顶部世色条
      ctx.fillStyle = dead ? BAR_DEAD : BAR_LIVING;
      this._roundRect(ctx, n.x + 6, n.y + 5, w - 12, 4, 2);
      ctx.fill();

      const name = n.displayName || n.name || '';
      if (!name) return;

      // 不缩放，名字永远可读；太长（复姓/脱敏四字）就缩字号装进牌里。
      // ⚠️ 必须先设 font 再 measureText——measureText 用的是**当前** font，
      // 拿上一张卡（或刻度）留下的字号去量，量出来的宽度是假的。
      let fs = NAME_FONT;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = fs + 'px ' + FONT_SERIF;
      while (fs > 9 && ctx.measureText(name).width > w - 8) {
        fs -= 1;
        ctx.font = fs + 'px ' + FONT_SERIF;
      }
      ctx.fillStyle = dead ? NAME_DEAD : NAME_LIVING;
      ctx.fillText(name, n.x + w / 2, n.y + h / 2 + 3);
    },

    /**
     * 左侧固定的世代丁口栏。
     *
     * 位置是**屏幕坐标**：横向滑动时它不是跟着跑，卡牌从它下面穿过；
     * 纵向跟着世代行——标签的意义就是"这一行是第几世、多少人"，
     * 不跟行就失去了锚。
     */
    _drawRail() {
      const ctx = this._ctx;
      const v = this._view;
      const dpr = this._dpr || 1;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // 栏底
      ctx.fillStyle = RAIL_BG;
      ctx.fillRect(0, 0, RAIL_W, this._h);
      ctx.strokeStyle = RAIL_LINE;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(RAIL_W + 0.5, 0);
      ctx.lineTo(RAIL_W + 0.5, this._h);
      ctx.stroke();

      // 栏头：「丁口」两个字 + 一条短金线，钉在顶部不随内容动
      ctx.fillStyle = RAIL_STRONG;
      ctx.font = '13px ' + FONT_SERIF;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('丁口', RAIL_W / 2, 18);
      ctx.fillStyle = 'rgba(201, 169, 97, 0.85)';
      ctx.fillRect(RAIL_W / 2 - 12, 30, 24, 1);

      const stats = this.data.stats || [];
      const rows = this._genRows || {};
      if (!stats.length) return;

      ctx.textAlign = 'center';
      let lastY = 18;   // 和栏头拉开
      for (const s of stats) {
        const wy = rows[s.gen];
        if (wy == null) continue;

        const y = v.y + wy + NODE_H / 2;
        if (y < 46 || y > this._h - 14) continue;
        // 两代贴太近就跳过（1:1 下正常不会，防的是布局改版后行高变小）
        if (y - lastY < 42) continue;
        lastY = y;

        // 世代
        ctx.fillStyle = RAIL_STRONG;
        ctx.font = '12px ' + FONT_SERIF;
        ctx.fillText('第' + s.gen + '世', RAIL_W / 2, y - 8);
        // 人数
        ctx.fillStyle = RAIL_TEXT;
        ctx.font = '10px sans-serif';
        ctx.fillText((s.count || 0) + ' 人', RAIL_W / 2, y + 8);
      }
    },

    _roundRect(ctx, x, y, w, h, r) {
      const rr = Math.min(r, h / 2, w / 2);
      ctx.beginPath();
      ctx.moveTo(x + rr, y);
      ctx.lineTo(x + w - rr, y);
      ctx.arcTo(x + w, y, x + w, y + rr, rr);
      ctx.lineTo(x + w, y + h - rr);
      ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
      ctx.lineTo(x + rr, y + h);
      ctx.arcTo(x, y + h, x, y + h - rr, rr);
      ctx.lineTo(x, y + rr);
      ctx.arcTo(x, y, x + rr, y, rr);
      ctx.closePath();
    },

    // ============================================================
    // 手势：只平移（不缩放）
    // ============================================================

    _onTouchStart(e) {
      const t = e.touches || [];
      this.setData({ showHint: false });
      if (!t.length) return;
      this._drag = {
        x0: t[0].x,
        y0: t[0].y,
        ox: this._view.x,
        oy: this._view.y,
        moved: 0,
        t0: Date.now(),
      };
    },

    _onTouchMove(e) {
      const t = e.touches || [];
      if (!this._drag || !t.length) return;
      const now = Date.now();
      if (now - (this._lastMove || 0) < 16) return;
      this._lastMove = now;

      const dx = t[0].x - this._drag.x0;
      const dy = t[0].y - this._drag.y0;
      this._drag.moved = Math.max(this._drag.moved, Math.abs(dx) + Math.abs(dy));
      this._view.x = this._drag.ox + dx;
      this._view.y = this._drag.oy + dy;
      this._clamp();
      this._schedule();
    },

    _onTouchEnd(e) {
      const drag = this._drag;
      this._drag = null;

      // 只有"按下没怎么动、且没停留太久"才算点击——
      // 否则用户拖一下图，松手就打开某个人的详情，非常烦人。
      if (!drag || drag.moved > 10 || Date.now() - drag.t0 > 350) return;
      if (e && e.changedTouches && e.changedTouches.length) {
        const pt = e.changedTouches[0];
        if (pt.x < RAIL_W) return;   // 左栏是统计，不是人
        this._hitTest(pt.x, pt.y);
      }
    },

    _hitTest(sx, sy) {
      const v = this._view;
      const wx = sx - v.x;
      const wy = sy - v.y;

      let best = null;
      let bestD = Infinity;
      let bestW = 0;
      let bestH = 0;
      for (const n of (this.data.nodes || [])) {
        const w = n.w || NODE_W;
        const h = n.h || NODE_H;
        if (wx < n.x || wx > n.x + w) continue;
        if (wy < n.y || wy > n.y + h) continue;
        const d = Math.abs(wx - (n.x + w / 2)) + Math.abs(wy - (n.y + h / 2));
        if (d < bestD) {
          bestD = d;
          best = n;
          bestW = w;
          bestH = h;
        }
      }

      if (best) {
        // cx/cy = 节点中心的视口坐标（canvas 元素内 px）。
        // 大宏图点人不再直接开半屏详情（会被铺满全屏的树压住），
        // 而是浮出操作环——环要锚在人身上，所以把中心坐标一起带出去。
        this.triggerEvent('nodetap', {
          id: best.id,
          cx: Math.round(best.x + bestW / 2 + v.x),
          cy: Math.round(best.y + bestH / 2 + v.y),
        });
      }
    },
  },
});
