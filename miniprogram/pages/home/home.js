// 族谱（首页，tab 名 2026-11-02 从「我的一家」改为「族谱」）
//
// 数据流很简单：一个接口拿 mine 视图的 nodes/edges（含世代与关系），
// 端上把这份原始数据重排成「垂直卡片流」渲染——布局不依赖坐标，
// 缩放、拖拽、连线全部退役。剩下的交互只有：点卡展开、换锚点、补全。

const { get, post } = require('../../utils/request');
const { DEV_MODE, DEV_DEMO_FAMILY_ID, ALLOW_DEMO_FAMILY } = require('../../utils/env');

// ============================================================
// 世系垂直卡片流（2026-10-28 方案 C，启明拍板）
// ============================================================
// 旧的横排树 + 拖拽缩放退役：手机上"全景地图"对长辈是认知负担
// （同事 AI 反馈同样指向这一点）。新交互是一条**垂直代际流**：
// 左侧世代徽章 + 一根细墨线贯穿，每代人物卡自上而下；
// 配偶内嵌在卡里（不再独立成卡）；点卡片内联展开"基本信息 / 子女 / 动作"。
// 布局全部端上重排，数据仍是 mine 视图那一份 nodes/edges——没有新接口。

const CN_DIGITS = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九'];

function cnNum(n) {
  n = Math.max(0, n | 0);
  if (n <= 9) return CN_DIGITS[n];
  if (n === 10) return '十';
  if (n < 20) return '十' + CN_DIGITS[n % 10];
  return CN_DIGITS[Math.floor(n / 10)] + '十' + (n % 10 ? CN_DIGITS[n % 10] : '');
}

const FLOW_ROLE_TEXT = { 2: '女儿', 3: '媳妇', 4: '女婿', 5: '外姓' };

/** 配偶行标注：元配/继配；离异（1）/婚姻无效（3）才加"已离异"，丧偶（2）不加 */
function spouseTag(s) {
  let t = !s.unionSeq || s.unionSeq === 1 ? '元配'
    : s.unionSeq === 2 ? '继配' : '第' + s.unionSeq + '配';
  if (s.unionEnded && (s.endReason === 1 || s.endReason === 3)) t += ' · 已离异';
  return t;
}

/**
 * 把 mine 视图的 nodes/edges 重排成垂直卡片流。
 *
 * 主线 = pathFromFounder（服务端算好的锚点直系链），保证"每代一张主卡"。
 * 其余人的取舍两条规则：
 *   ① **只有"随本支入谱"的人能成卡**：本族血脉（lineage_role 1 男丁 / 2 女儿），
 *      以及**父母在主线上**的招赘外姓孩子（外姓随母入谱，决策 15 谱例）。
 *      媳妇、女婿内嵌在配偶位，永不单独成卡。
 *   ② 血脉内的：**父亲不在这条流上当卡的人，自己当卡**；
 *      父亲已经是卡的（兄弟、叔伯的子女）缩成父卡的「子女」小片。
 *
 * ① 是 2026-10-29 补的：只按 ② 判会出事故——女儿招赘所生的外孙（role 5），
 * 父亲是女婿（role 4，内嵌在配偶位、从来不是卡），于是"父亲不是卡"成立，
 * 外孙自己顶成一张卡，跑到「我这一支」第五世的正中间去。
 * 用户实报：「第五世出现林子轩，他是陈雨欣的后代，不是我的直系」。
 * 注意判据不是"外姓一律删"：锚点换成陈雨欣本人时，她的招赘儿子就在她的
 * 直系上（随母入谱），必须显示——所以外姓成卡的唯一通道是父母在主线上。
 */
function buildFlow(nodes, edges, anchorId, pathFromFounder, openId) {
  if (!nodes || !nodes.length || !anchorId) return [];

  const byId = {};
  nodes.forEach((n) => { byId[n.id] = n; });

  const kidsOf = {};
  const fatherOf = {};
  const parentsOf = {};
  edges.forEach((e) => {
    if (!e || !e.from || !e.to) return;
    if (e.type === 1) {
      (kidsOf[e.from] = kidsOf[e.from] || []).push(e.to);
      if (!fatherOf[e.to]) fatherOf[e.to] = e.from;
    } else if (e.type === 3 || e.type === 4) {
      // 过继（3）/ 外姓随母（4）也是亲子边：进子女片、进父母集。
      // ⚠️ 招赘孩子的母子边是 type 4 不是 type 1——只认 type 1 的话，
      // 陈雨欣的卡上永远挂不出儿子林子轩（2026-10-29 实测）。
      (kidsOf[e.from] = kidsOf[e.from] || []).push(e.to);
    }
    if (e.type === 1 || e.type === 3 || e.type === 4) {
      (parentsOf[e.to] = parentsOf[e.to] || []).push(e.from);
    }
  });

  const mainline = {};
  (pathFromFounder || []).forEach((id) => { mainline[id] = true; });

  // 本族血脉：1 男丁、2 本族女儿天然可成卡；主线无条件可成卡（数据挂脏 role
  // 也不能把先祖从流里删掉）。外姓（5）唯一的成卡通道见函数头 ①。
  const BLOOD = { 1: true, 2: true };

  // 按世代分组（非配偶节点；配偶永远内嵌在本人卡里）
  const gens = {};
  nodes.forEach((n) => {
    if (n.isSpouse) return;
    (gens[n.gen] = gens[n.gen] || []).push(n);
  });
  const genKeys = Object.keys(gens).map(Number).sort((a, b) => a - b);

  const isCard = {};
  const flow = [];
  genKeys.forEach((g) => {
    const cards = [];
    gens[g].forEach((n) => {
      const onMain = mainline[n.id];
      if (!onMain && !BLOOD[n.role]) {
        // 外姓（外孙等）：只有父母在主线上的招赘子女才随母入这一支，
        // 其余（支系女儿的外姓孩子）连片都不进——他们属于别的锚点的"我这一支"
        if (!(parentsOf[n.id] || []).some((p) => mainline[p])) return;
      }
      const f = fatherOf[n.id];
      if (onMain || !f || !isCard[f]) {
        isCard[n.id] = true;
        cards.push(n);
      }
    });
    if (!cards.length) return;

    flow.push({
      gen: g,
      label: '第' + cnNum(g) + '世',
      cards: cards.map((n) => {
        const name = n.displayName || n.name || '';
        const spouses = nodes
          .filter((s) => s.isSpouse && s.spouseOf === n.id)
          .map((s) => ({
            id: s.id,
            displayName: s.displayName || s.name || '',
            surname: (s.displayName || s.name || '?')[0],
            living: s.living,
            tag: spouseTag(s),
            divorced: !!(s.unionEnded && (s.endReason === 1 || s.endReason === 3)),
          }));
        const kids = (kidsOf[n.id] || [])
          .filter((k) => byId[k])
          .map((k) => ({
            id: k,
            displayName: byId[k].displayName || byId[k].name || '',
            surname: (byId[k].displayName || byId[k].name || '?')[0],
          }));
        const roleLabel = FLOW_ROLE_TEXT[n.role] || '';
        const meta = [roleLabel, n.living === 2 ? '已故' : '在世']
          .filter(Boolean).join(' · ');
        return {
          id: n.id,
          displayName: name,
          surname: name[0] || '?',
          living: n.living,
          isSelf: !!n.isSelf,
          onPath: !!n.onPath,
          badge: n.badge || null,
          meta,
          spouses,
          kids,
          open: n.id === openId,
        };
      }),
    });
  });
  return flow;
}

function windowWidth() {
  try {
    if (wx.getWindowInfo) return wx.getWindowInfo().windowWidth;
    return wx.getSystemInfoSync().windowWidth;
  } catch (e) {
    return 375;
  }
}

// ============================================================
// 点人操作环的六个位置
// ============================================================
//
// 角度用"上北下南、顺时针"的屏幕直觉：-90° 是正上方，0° 是正右。
// 分组方式见 openRing 的注释：上长辈、下晚辈、左配偶、右详情。
// 六个位置，一个不多一个不少——再多就要靠读字，而长辈不读字。
const RING_ITEMS = [
  { key: 'father', label: '父', angle: -135, cls: 'ring-up' },
  { key: 'mother', label: '母', angle: -45, cls: 'ring-up' },
  { key: 'spouse', label: '配', angle: 180, cls: 'ring-side' },
  { key: 'detail', label: '详', angle: 0, cls: 'ring-detail' },
  { key: 'son', label: '子', angle: 135, cls: 'ring-down' },
  { key: 'daughter', label: '女', angle: 45, cls: 'ring-down' },
];

// 每个动作要建的"是什么人"。
//   type     —— 关系类型，决定挂在哪（father_of/mother_of 的锚点是孩子自己）
//   gender   —— 1 男 2 女
//   role     —— lineage_role：1 男丁 2 本族女儿 3 媳妇 4 女婿
//               这个字段不是装饰：全族树的筛选和"世代丁口人数"都只认 1/2/3，
//               给媳妇塞个默认的 1，统计里就多一个男丁。
//   genDelta —— 相对这个人的世次偏移
const RING_RULES = {
  father: {
    type: 'father_of', gender: 1, role: 1, genDelta: -1,
    title: '补上父亲的名字', placeholder: '如：陈广福',
  },
  mother: {
    type: 'mother_of', gender: 2, role: 3, genDelta: -1,
    title: '补上母亲的名字', placeholder: '如：王氏',
  },
  spouse: {
    // 性别与丁口身份在 _createRelative 里按"这个人是男是女"反推
    type: 'spouse_of', gender: 2, role: 3, genDelta: 0,
    title: '补上配偶的名字', placeholder: '如：李秀英',
  },
  son: {
    type: 'child_of', gender: 1, role: 1, genDelta: 1,
    title: '添一个儿子', placeholder: '如：陈启明',
  },
  daughter: {
    type: 'child_of', gender: 2, role: 2, genDelta: 1,
    title: '添一个女儿', placeholder: '如：陈小燕',
  },
};

Page({
  data: {
    loading: true,
    familyId: null,
    familyName: '',
    // 家族卡：堂号世系 / 规模 / 完整度 / 我的身份
    familyLine: '',
    roleText: '',
    famHead: 0,
    famGen: 0,
    famPct: 0,
    view: 'mine',
    anchorId: null,
    needClaim: false,

    // 操作环。items 里的 x/y 是相对舞台左上角的 px，
    // 由「加家人」按钮的点击坐标换算出来。
    ring: null,

    nodes: [],
    edges: [],
    canvas: { width: 0, height: 0 },
    // 垂直卡片流（mine 视图的渲染结构，由 buildFlow 重排生成）
    flow: [],
    // scroll-into-view 目标：加载完成后落到锚点所在的那一代
    flowInto: '',

    // 全族大宏图：Canvas 树状图（2026-10-29 定稿，1:1 不缩放）。
    // ovFocus = 从详情页「看他在树上的位置」跳进来时要定位并烫金高亮的那个人；
    // 它只影响落点与描边，不带任何权限。
    ovFocus: null,

    selectedId: null,
    sheetShow: false,
    sheetPerson: null,
    sheetLoading: false,

    pending: null,
    truncated: null,
    emptyText: '这个宗族还没有录入人物',

    // 全族总览的世代统计：服务端按"只数男丁 + 本族女儿"的口径算好下发，
    // 端上只负责显示（端上数字和老谱对不上比不显示更糟）。
    stats: [],
    genSummary: '',

    // "连宗族都没定下来"和"宗族定了但树没拉到"是两种不同的故障：
    // 前者要重走整个入族流程（familyId 还是空的，重拉树没有意义），
    // 后者只要重拉一次树。所以分开存，重试按钮也据此分流。
    loadError: '',
    loadErrorHint: '',

    // 找人：全屏搜索层的状态
    searchShow: false,
    searchKw: '',
    searchHits: [],
    searchLoading: false,

  },

  // ============================================================
  // 生命周期
  // ============================================================

  onLoad(query) {
    this._query = query || {};
    this._rev = 0;              // 树的数据版本号，用于增量刷新
    this._openId = null;        // 当前展开的卡片（流内重排用，不进 data）
    this._lastEdges = [];       // 最近一次 mine 数据的边（展开态切换重排用）
    this._lastPath = [];        // 最近一次的主线链（同上）
    this._nodesMap = {};
    // 从人物详情页「看他在树上的位置」进来：q.view=overview&q.focus=<id>。
    // focus 只决定"滚到哪、高亮谁"，不携带任何权限。
    this._startView = this._query.view === 'overview' ? 'overview' : 'mine';
    this._focusId = Number(this._query.focus) || null;
    this.init();
  },

  onShow() {
    // 从填写页 / 认领页回来时数据可能已经变了。用标记位而不是每次都重拉，
    // 因为"看树→点节点→退回来"是最高频的路径，每次都重拉会很卡。
    const g = getApp();
    if (g.globalData.treeDirty && this.data.familyId) {
      g.globalData.treeDirty = false;
      this.loadTree(this.data.view, this.data.anchorId, true);
    }
  },

  onPullDownRefresh() {
    const done = () => wx.stopPullDownRefresh();
    this.loadTree(this.data.view, this.data.anchorId, true).then(done, done);
  },

  onUnload() {
    if (this._tipTimer) clearTimeout(this._tipTimer);
  },

  // ============================================================
  // 初始化
  // ============================================================

  async init() {
    const g = getApp();
    const q = this._query || {};

    // 从家族群分享链接进来的人，先送去落地页。
    // 那是转化率最高的一屏：先让他看到"这是我家"，再谈认领。
    //
    // 用 reLaunch 而不是 redirectTo：首页现在是 tabBar 页，
    // 从 tab 页 redirectTo 到普通页在不同基础库上表现不一致（有的会连 tabBar 一起留着）。
    // 反正这一步之后没有"返回上一页"的语义，重开栈最干净。
    const inviteCode = q.code || g.globalData.pendingInviteCode;
    if (inviteCode) {
      wx.reLaunch({
        url: '/pages/landing/landing?code=' + encodeURIComponent(inviteCode),
      });
      return;
    }

    // q.familyId 只在开发期认（开发者工具的编译模式靠它直达某一族）。
    // 生产期必须是"真的入过族"——家谱里已故先人的全名在树上是公开的，
    // 一个可以随手拼进链接的 query 不能当门票。
    let fid = (DEV_MODE ? Number(q.familyId) : 0) || g.globalData.familyId || null;

    // answered 的含义是「后端**答复过**你在哪个族」，它和「有没有族」是两件事：
    //   答复了、family 为空 → 这人确实没入族 → 去输家族码
    //   压根没答复          → 网络/服务问题     → 提示重试
    // 这两条以前被同一句兜底压成"渲染 1 号族"，于是用户看到一棵不属于
    // 自己的树、去认领时被后端按"未入族"拒掉——症状出现在认领页，
    // 病根却在这一屏。
    let answered = false;
    // 入族这一步真出错了就记下原文。关掉 mock 之后"微信登录被拒"是最常见的
    // 一种失败，压成"连不上服务器"会让人去查网络、查半天（见下面的 loadError）。
    let entryErr = null;

    if (!fid) {
      try {
        await g.ensureLogin();
        const me = await get('/family/mine', {}, { silent: true });
        answered = true;
        if (me && me.family) {
          fid = me.family.id;
          g.globalData.familyId = fid;
        }
      } catch (e) {
        // 问不到后端。这不是"你没入族"，见上面 answered 的说明。
        entryErr = e;
      }
    }

    if (!fid) {
      if (answered) {
        // 明确没入族 → 去落地页输家族码。
        // 这一跳是整条链路的关键：没有它，用户会停在一棵假树上，
        // 然后拿着"我明明已经在族里"的认知去认领、被拒。
        wx.reLaunch({ url: '/pages/landing/landing' });
        return;
      }
      if (ALLOW_DEMO_FAMILY) {
        // 开发期、且**连的是本机后端**时，兜一个演示族，只为让界面是活的。
        // 云端不用这一支：云端挂了就该老老实实报错。
        // ⚠️ 写操作绝不能依赖它，否则等于把家族码这道门拆掉。
        fid = DEV_DEMO_FAMILY_ID;
      } else {
        const isNet = !entryErr || entryErr.code === -1;
        this.setData({
          loading: false,
          loadError: isNet ? '连不上服务器' : '登录失败',
          loadErrorHint: isNet
            ? '请检查网络后重试'
            : entryErr.message || '请稍后重试',
        });
        return;
      }
    }

    this.setData({ familyId: fid, loadError: '', loadErrorHint: '' });
    // 子页面（谱文列表 / 详情）读的是 globalData.familyId，这里统一回填：
    // 开发期用 ?familyId= 直达某一族时，fid 只存在于本页 data，
    // 不回填的话点进谱文列表会以"还没加入任何家族"收场。
    g.globalData.familyId = fid;
    this.loadSummary(fid);

    // 锚点要靠 globalData.user.claimedPersonId 才定得下来，所以身份必须先到位。
    // 上面那条分支（fid 为空时才 await 登录）在两种情况下会被绕过：
    //   ① 开发期 q.familyId 直达某一族；② 从落地页 reLaunch 过来、globalData.familyId 已有值。
    // 那时 user 还是空的，pickAnchor 只能退回"全族最上面那位"——用户进「我的一家」
    // 看到的是始祖那三代，自己根本不在画面上（必须先点「全族总览」再点回来才聚焦，就很别扭）。
    await g.ensureLogin().catch(() => {});

    // 起始视图由入口决定：详情页「看他在树上的位置」= overview + 定位某人；
    // 其他入口（tab、落地页）一律「我的一家」。
    if (this._startView === 'overview') {
      wx.setNavigationBarTitle({ title: '全族大宏图' });
      this.setData({ view: 'overview' });
    }

    await this.loadTree(this._startView, null);
  },

  async loadSummary(fid) {
    try {
      const s = await get(`/family/${fid}/summary`, {}, { silent: true });
      const st = s.stats || {};

      // 堂号优先（"义门陈"），没有堂号退到郡望（"颍川"）——两者都有个"我是哪一支"的意味，
      // 但堂号更具体，老谱上一般只写堂号。
      const line = [
        s.tangHao ? `堂号 ${s.tangHao}` : s.junWang ? `郡望 ${s.junWang}` : '',
        s.founderName
          ? `始迁祖 ${s.founderName}${s.founderEra ? '（' + s.founderEra + '）' : ''}`
          : s.settlePlace || '',
      ]
        .filter(Boolean)
        .join(' · ');

      const g = getApp();
      const role = (g.globalData.user && g.globalData.user.role) || 1;

      this.setData({
        familyName: s.name || '',
        familyLine: line,
        // 与后端 family.py::_ROLE_TEXT 同一套叫法：2 号角色是「理事会成员」，
        // 真正的房长多半不操作智能机，实际用的是理事会里会用手机的年轻人。
        roleText:
          { 1: '族人', 2: '理事会成员', 3: '族管理员', 4: '平台运营' }[Number(role)] ||
          '族人',
        famHead: st.personCount || 0,
        famGen: st.generationCount || 0,
        famPct: Math.round((st.completeness || 0) * 100),
      });
    } catch (e) {
      // 家族卡拿不到不影响主流程（树照常看），静默
    }
  },

  // ============================================================
  // 树
  // ============================================================

  async loadTree(view, anchor, force) {
    const fid = this.data.familyId;
    if (!fid) return;

    // 换树/换锚点时环必须收掉：它记的是"旧图的某个人"，新图里那些坐标没有意义
    this.setData({ loading: true, ring: null });

    try {
      let realAnchor = anchor;

      // 未认领自己时没有锚点，服务端不给"我的一家"。
      // 这里挑全族最上面那位当锚点，先让用户看清这张谱长什么样，
      // 再由未认领提示引导他去认领。
      if (view !== 'overview' && !realAnchor) {
        realAnchor = await this.pickAnchor(fid);
      }

      const params = { view };
      if (view !== 'overview' && realAnchor) params.anchor = realAnchor;
      // 全族大宏图是 Canvas 谱牌：节点上要写名字，所以必须 detail 全量
      // （脱敏仍由服务端按 viewer 做）。detail=lite 只留给未认领时挑锚点那一步——
      // 那一步只要坐标，不需要姓名。
      // 「我的一家」里兄弟是父卡下的一个小片，横向没有"铺不下"的问题，
      // 直接全量展开（99 足够大），折叠角标那套机制随之退役。
      if (view === 'mine') params.siblings = 99;
      if (!force && this._rev) params.rev = this._rev;

      const data = await get(`/tree/${fid}`, params, { silent: true });

      // 数据没变则一个字节都不传，这里直接复用现有渲染
      if (data && data.notModified) {
        this.setData({ loading: false });
        return;
      }

      const nodes = data.nodes || [];
      const edges = data.edges || [];
      const canvas = data.canvas || { width: 0, height: 0 };

      const anchorId = data.anchorPersonId || realAnchor || null;
      this._rev = data.rev || 0;
      this._nodesMap = {};
      nodes.forEach((n) => {
        this._nodesMap[n.id] = n;
      });

      // 展开态默认落在锚点自己身上；换人之后那张卡若不在流里，
      // buildFlow 的 open 匹配不上就整条流都是收起的，不会错展开别人。
      if (view === 'mine' && !this._openId) this._openId = anchorId;
      // 展开态切换只在前端重排，缓存这两份原始数据免得再拉接口
      this._lastEdges = edges;
      this._lastPath = data.pathFromFounder || [];
      const flow = view === 'mine'
        ? buildFlow(nodes, edges, anchorId, data.pathFromFounder, this._openId)
        : [];

      // 全族大宏图：Canvas 自己排、自己画（服务端给坐标），这里只把
      // "要从谁身上定位进来"递进去。定位意图只消费一次：之后用户再切回
      // 大宏图，不该又被拽回那个人。
      let ovFocus = null;
      if (view === 'overview') {
        if (this._focusId && nodes.some((n) => n.id === this._focusId)) {
          ovFocus = this._focusId;
        }
        this._focusId = null;
      }

      const g = getApp();
      const claimed = g.globalData.user && g.globalData.user.claimedPersonId;

      // 主线全量后流可能从始祖排到很深，落点必须自动滚到锚点那一代，
      // 否则用户进来第一眼是八世祖而不是自己。
      const anchorNode = anchorId
        ? nodes.find((n) => n.id === anchorId)
        : null;

      this.setData({
        view: data.view || view,
        anchorId,
        nodes,
        edges,
        canvas,
        flow,
        // scroll-into-view 的目标：锚点所在那一代的 fgen 容器
        flowInto: view === 'mine' && anchorNode ? 'fgen' + anchorNode.gen : '',
        // 全族大宏图（Canvas 树状图）
        ovFocus,
        stats: data.generationStats || [],
        genSummary:
          data.generationStats && data.generationStats.length
            ? `${data.generationCount} 代 · ${data.lineageTotal} 丁口`
              + `（男${data.maleTotal || 0} 女${data.femaleTotal || 0}）`
            : '',
        loading: false,
        pending: data.pending || null,
        truncated: data.truncated || null,
        needClaim: !claimed,
        emptyText:
          view === 'overview'
            ? '这个宗族还没有录入人物'
            : '这一支暂时没有可显示的人',
        // 舞台只有在这一屏真的渲染出来之后才量得到矩形，
        // 操作环的贴边修正全靠它。setData 回调 = 渲染完成，正好是量的时机。
      }, () => this._measureStage());
    } catch (e) {
      // 分享锚点可能是别的族的人（他后来切了族）、或已被合并删除。
      // 服务端会回 40001，直接渲染成"加载失败"就是一整页空白。
      // 这里自愈：只认分享锚点这一种情况，清掉再按始祖重拉一次。
      const gSelf = getApp();
      if (
        e &&
        e.code === 40001 &&
        view === 'mine' &&
        gSelf.globalData.shareAnchor &&
        realAnchor === gSelf.globalData.shareAnchor
      ) {
        gSelf.globalData.shareAnchor = null;
        return this.loadTree(view, null, force);
      }

      this.setData({
        loading: false,
        nodes: [],
        flow: [],
        emptyText:
          e && e.code === -1
            ? '连不上服务器。请确认后端已启动（uvicorn app.main:app --port 8000）'
            : e.message || '加载失败',
      });
    }
  },

  // ============================================================
  // 全族大宏图（Canvas 树状图 · 不缩放，只平移）
  // ============================================================
  //
  // Canvas 里已经做完滑动、左栏丁口、命中判定，这里只接一件事：
  //   点一个人 → 打开详情（和「我的一家」点卡一个语义，不跳页、不换视图）。
  // 连线和落位都在组件里：这一屏不 setData（touchmove 每秒 60 次，
  // 一个 setData 就能把它拖到 20fps）。

  onOvNodeTap(e) {
    const d = e.detail || {};
    if (!d.id) return;
    // 大宏图点人 = 浮出操作环（上父母、下子女、左配偶、右详情），
    // 和卡片流「加家人」同一套交互，也和参考 App 的加人卡一致。
    // 不再直接开半屏详情：树铺满全屏，半屏卡压不住它，看起来像被树挡住。
    // 环的「详」在大宏图里直接进个人页（见 onRingAction）。
    this.openRing(d.id, { cx: d.cx, cy: d.cy, from: 'overview' });
  },

  /**
   * 未认领时，用全族骨架里最上面那一位当锚点。
   *
   * 用 detail=lite 拉，只拿坐标不留姓名——这一步只为定位，不需要名字。
   */
  async pickAnchor(fid) {
    const g = getApp();
    if (g.globalData.user && g.globalData.user.claimedPersonId) {
      return g.globalData.user.claimedPersonId;
    }

    // 被分享人还没认领自己：先聚焦到**分享人**身上。
    // 这是分享链接唯一的作用（它不带任何权限）——他点开第一眼要判断的是
    // "这上面有没有我认得的人"，把他扔到始祖那一屏等于让他从几百人里找自己。
    // shareAnchor 由落地页/App.onLaunch 从链接的 ?anchor= 写入。
    if (g.globalData.shareAnchor) {
      return g.globalData.shareAnchor;
    }

    try {
      const ov = await get(
        `/tree/${fid}`,
        { view: 'overview', detail: 'lite' },
        { silent: true }
      );
      const list = (ov && ov.nodes) || [];
      // nodes 已按 (世代, 横坐标) 排序，第一个就是始祖那一格
      const first = list.find((n) => n.role === 1) || list[0];
      return first ? first.id : null;
    } catch (e) {
      return null;
    }
  },

  // ============================================================
  // 视图切换（两个大按钮：「一键查我这一支」/「全族大宏图」）
  // ============================================================

  onSwitchView(e) {
    const key = e.currentTarget.dataset.key;
    if (!key || key === this.data.view) return;

    wx.setNavigationBarTitle({
      title: { mine: '族谱', overview: '全族大宏图' }[key] || '饶信谱脉',
    });

    this.setData({
      view: key,
      anchorId: key === 'overview' ? null : this.data.anchorId,
      sheetShow: false,
      selectedId: null,
    });

    this.loadTree(key, key === 'overview' ? null : this.data.anchorId, true);
  },

  // ============================================================
  // 垂直卡片流的交互
  // ============================================================
  //
  // 点卡片 = 内联展开（翻书感，不跳页）；卡内按钮才是动作。
  //   看他这一支 —— 换锚点，流以他为主线上下展开
  //   帮他补全   —— 直跳填写页（角标只给能补的人画，点了一定能补）
  //   加家人     —— 原操作环，锚在卡片上（补父母/配偶/子女）

  onCardTap(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    if (this.data.ring) this.setData({ ring: null, selectedId: null });
    this._openId = this._openId === id ? null : id;
    this.setData({
      flow: buildFlow(
        this.data.nodes, this._lastEdges || [], this.data.anchorId,
        this._lastPath, this._openId
      ),
    });
  },

  /**
   * 配偶行点一下 = 看这个人的详情。
   *
   * 配偶不是"卡"（内嵌在卡里，buildFlow 只给卡算 open 态），所以原来挂的
   * onCardTap 把 openId 设成配偶 id 后，buildFlow 里没有任何一张卡匹配得上，
   * 界面毫无变化——用户实报「妻子的信息无法展开，看不了详情」。
   * 这里直接开详情面板，和「卡头 → 详」同一个落点。
   */
  onSpouseTap(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    if (this.data.ring) this.setData({ ring: null, selectedId: null });
    this.openPerson(id);
  },

  /** 卡头「详」：看这个人详情，不切换展开态 */
  onCardDetail(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    this.openPerson(id);
  },

  onFlowCenter(e) {
    const id = e.currentTarget.dataset.id;
    if (!id || id === this.data.anchorId) return;
    const n = this._nodesMap && this._nodesMap[id];
    this._openId = id;
    this.setData({ anchorId: id });
    this.loadTree('mine', id, true);
    if (n) {
      wx.showToast({
        title: '已切到「' + (n.displayName || n.name || '') + '」这一支',
        icon: 'none',
        duration: 1200,
      });
    }
  },

  onFlowFill(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    wx.navigateTo({ url: '/pages/fill/fill?personId=' + id });
  },

  /** 「加家人」：操作环锚在被点的那张卡上（e.detail 是点击点的页面坐标） */
  onFlowRing(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    const r = this._stageRect;
    this.openRing(id, {
      cx: e.detail.x - (r ? r.left : 0),
      cy: e.detail.y - (r ? r.top : 0),
    });
  },

  /**
   * 顶部「还有先人」：跳到服务端下发的 upAnchor（父链上第一个不在屏上的先人）。
   *
   * 为什么不让端上自己算：窗口里"最上面的卡"未必是"父链上第一个出屏的人"
   * （主线全量后通常父链都在屏上，此时根本不出这个按钮）。以服务端为准。
   */
  onFlowUp() {
    const t = this.data.truncated || {};
    if (t.up && t.upAnchor) {
      this.onFlowCenter({ currentTarget: { dataset: { id: t.upAnchor } } });
      return;
    }
    const flow = this.data.flow;
    if (!flow.length) return;
    const top = flow[0].cards.find((c) => c.onPath) || flow[0].cards[0];
    if (top) {
      this.onFlowCenter({ currentTarget: { dataset: { id: top.id } } });
    }
  },

  /**
   * 底部「还有后代」：跳到服务端下发的 downAnchor。
   *
   * ⚠️ 这里曾是真 bug 的现场：旧逻辑取"流里最底下那张卡"，
   * 而窗口下探 1 代时锚点的孩子全是父卡的「子女」小片，
   * 最底下的卡**永远是锚点本人**——把锚点设为锚点，等于什么都没做。
   * 现在服务端直接告诉端上"往下跳谁"（离锚点最近的出屏后代、优先承袭支）。
   */
  onFlowDown() {
    const t = this.data.truncated || {};
    if (t.down && t.downAnchor) {
      this.onFlowCenter({ currentTarget: { dataset: { id: t.downAnchor } } });
      return;
    }
    const flow = this.data.flow;
    if (!flow.length) return;
    const last = flow[flow.length - 1];
    const bottom = last.cards.find((c) => c.onPath) || last.cards[0];
    if (bottom) {
      this.onFlowCenter({ currentTarget: { dataset: { id: bottom.id } } });
    }
  },

  _measureStage() {
    try {
      // 量 .stage-wrap 而不是 .stage：stage-wrap 两个视图都在
      //（mine 的卡片流和 overview 的 canvas 都铺满它），
      // 量 .stage 的话在大宏图里拿不到矩形，环的贴边修正只能瞎猜。
      wx.createSelectorQuery()
        .select('.stage-wrap')
        .boundingClientRect((r) => {
          if (r && r.width) this._stageRect = r;
        })
        .exec();
    } catch (e) {
      // 量不到就退回"以页面左上角为原点"：环的位置略偏，
      // 下一次拉完树会重新量准。不值得为此报错打断用户。
    }
  },

  // ============================================================
  // 操作环（「加家人」）
  // ============================================================
  //
  // 卡片流里点人 = 内联展开；真正"给他补父母/配偶/子女"的动作
  // 收在展开区的「加家人」里，绕着那张卡浮出六个动作，一步到位。
  //
  // 布局按心智模型分组，不是平均分布：
  //     上（左上父 / 右上母）  = 长辈，往上是往上找
  //     下（左下子 / 右下女）  = 晚辈
  //     左 = 配偶（平辈，同世次）
  //     右 = 详情（"往旁边看"，不改变结构）
  //
  // 已知已填的槽位会被置灰（比如已经有父亲），避免重复建人——
  // 家谱里多一个"同名父亲"比少一个更难收拾。

  openRing(id, center) {
    const n = this._nodesMap && this._nodesMap[id];
    if (!n) return;

    let cx;
    let cy;
    if (center) {
      // 卡片流：环锚在点击点（相对舞台的 px）
      cx = center.cx;
      cy = center.cy;
    } else {
      const scale = this.data.scale || 1;
      const sc = this._scroll || { left: 0, top: 0 };

      // 节点中心 → 舞台坐标。树坐标本身是 px，所以乘 scale 再减滚动量即可，
      // 不需要管 rpx 换算。
      cx = (n.x + (n.w || 88) / 2) * scale - sc.left;
      cy = (n.y + (n.h || 96) / 2) * scale - sc.top;
    }

    // 半径按"不压住节点"倒推：节点是 88×96，半宽 44、半高 48，
    // 按钮半径 26。斜上方四个位置要同时躲开节点，R 至少要 44+26=70；
    // 取 86 留出余量，同时保证整环直径 86*2+52=224px 能塞进 375 宽的屏。
    const R = 86;
    const BTN = 52;
    const items = RING_ITEMS.map((it) => {
      const rad = (it.angle * Math.PI) / 180;
      return {
        key: it.key,
        label: it.label,
        cls: it.cls,
        x: Math.round(cx + Math.cos(rad) * R - BTN / 2),
        y: Math.round(cy + Math.sin(rad) * R - BTN / 2),
        disabled: false,
      };
    });

    this.setData({
      ring: {
        cx,
        cy,
        items: this._clampRing(items, BTN),
        personId: id,
        // 从哪个视图锚出来的：大宏图的「详」直接进个人页，
        // 卡片流的「详」开半屏详情（那里的卡不会被挡）。
        from: (center && center.from) || '',
      },
      selectedId: id,
    });
    this._loadRingRelatives(id);
  },

  /**
   * 环贴边时整体平移，别让按钮被舞台裁掉一半。
   *
   * 被点的节点可能就在屏幕边上（"这是我大哥"往往在树的右侧），
   * 那时环有一半在可视区外。整体平移会让人看起来不像个环，
   * 但比"半个按钮点不到"好——用户要的是那几个动作，不是图标对称。
   */
  _clampRing(items, btn) {
    const r = this._stageRect;
    const w = (r && r.width) || windowWidth();
    const h = (r && r.height) || 400;
    const pad = 6;

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const it of items) {
      if (it.x < minX) minX = it.x;
      if (it.y < minY) minY = it.y;
      if (it.x + btn > maxX) maxX = it.x + btn;
      if (it.y + btn > maxY) maxY = it.y + btn;
    }

    let dx = 0;
    let dy = 0;
    if (minX < pad) dx = pad - minX;
    else if (maxX > w - pad) dx = w - pad - maxX;
    if (minY < pad) dy = pad - minY;
    else if (maxY > h - pad) dy = h - pad - maxY;

    if (!dx && !dy) return items;
    return items.map((it) => ({
      key: it.key,
      label: it.label,
      cls: it.cls,
      x: it.x + dx,
      y: it.y + dy,
      disabled: it.disabled,
    }));
  },

  /**
   * 查这个人已经有哪些直系亲属，把"父/母"这类只能有一个的槽位置灰。
   *
   * 环先出现、灰态后到：查一次要几十到几百毫秒，让环等着会显得点了没反应。
   * 这一下点空了也不要紧——真点了已填的槽，后端也会拒绝重复挂载。
   */
  async _loadRingRelatives(id) {
    try {
      const d = await get(`/person/${id}/relatives`, {}, { silent: true });
      if (!this.data.ring || this.data.ring.personId !== id) return;

      const filled = {
        father: (d && d.father && d.father.displayName) || '',
        mother: (d && d.mother && d.mother.displayName) || '',
      };
      if (!filled.father && !filled.mother) return;

      // 不用对象展开语法：部分基础库对 ES2018 语法支持不一致，白屏比报错更难查
      const items = this.data.ring.items.map((it) => ({
        key: it.key,
        label: it.label,
        cls: it.cls,
        x: it.x,
        y: it.y,
        disabled: it.key === 'father' ? !!filled.father
          : it.key === 'mother' ? !!filled.mother
            : it.disabled,
      }));
      this.setData({ 'ring.items': items });
    } catch (e) {
      // 拿不到就当全空着，让用户点；后端会兜住
    }
  },

  onRingClose() {
    if (this.data.ring) this.setData({ ring: null, selectedId: null });
  },

  onRingAction(e) {
    const key = e.currentTarget.dataset.key;
    const ring = this.data.ring;
    if (!ring || !key) return;

    const item = ring.items.find((i) => i.key === key);
    if (item && item.disabled) {
      wx.showToast({ title: `已经有${item.label}亲了`, icon: 'none' });
      return;
    }

    if (key === 'detail') {
      const from = ring.from;
      this.setData({ ring: null });
      if (from === 'overview') {
        // 大宏图：树铺满全屏，半屏详情卡会被 canvas 压在下面（实测）。
        // 参考同类 App 的做法——「详」直接进个人页，一步到位不叠层。
        wx.navigateTo({ url: `/pages/person/person?id=${ring.personId}` });
      } else {
        this.openPerson(ring.personId);
      }
      return;
    }

    this._askCreateRelative(ring.personId, key);
  },

  /** 一个名字就够建人：其余字段（性别/世代/丁口身份）由关系推出来 */
  _askCreateRelative(pid, key) {
    const rule = RING_RULES[key];
    if (!rule) return;

    const n = this._nodesMap[pid] || {};

    wx.showModal({
      title: rule.title,
      editable: true,
      placeholderText: rule.placeholder,
      success: (res) => {
        if (!res.confirm) return;
        const name = (res.content || '').trim();
        if (!name) {
          wx.showToast({ title: '名字不能空着', icon: 'none' });
          return;
        }
        this._createRelative(pid, key, name, rule, n);
      },
    });
  },

  /**
   * 建人 + 挂关系。
   *
   * 三件事必须一起传，少一件就会出现"人建出来了但树上没有"这类静默错：
   *   name           —— 当然要有
   *   gender         —— 决定画男框还是女框
   *   lineageRole    —— 决定他算不算本族丁口（媳妇是 3、女儿是 2、女婿是 4）
   *   generation     —— 服务端对没有 generation 的人兜底成第 1 世，
   *                     所以"给爷爷补父亲"如果不传，老爷子会跑到始祖旁边去
   */
  async _createRelative(pid, key, name, rule, node) {
    const gen = node && node.gen ? node.gen + rule.genDelta : null;

    // 配偶的性别和丁口身份只能反着推：男丁娶进来的是媳妇(3)，
    // 女儿招进来的是女婿(4)。这两类在树上画法不同，不能都用默认值。
    let gender = rule.gender;
    let role = rule.role;
    if (key === 'spouse') {
      gender = Number(node && node.gender) === 2 ? 1 : 2;
      role = Number(node && node.gender) === 2 ? 4 : 3;
    }

    const fields = { name, gender, lineageRole: role };
    if (gen) fields.generation = gen;

    const relation = { type: rule.type };
    if (rule.type === 'child_of') relation.parentId = pid;
    else if (rule.type === 'spouse_of') relation.partnerId = pid;
    else relation.childId = pid;

    try {
      wx.showLoading({ title: '提交中', mask: true });
      const d = await post(
        '/person',
        { fields, relation, submitNote: '树上直接补录' },
        { silent: true }
      );
      wx.hideLoading();
      this.setData({ ring: null, selectedId: null });

      // 决策第 11 条：写接口必须显式回 autoMerged，端上据此决定是"已加入"还是"待审"。
      // 不能统一提示"已提交"——有权限的人改完还得等审核会让人以为没生效。
      if (d && d.autoMerged) {
        wx.showToast({ title: '已加入这一支', icon: 'none' });
        getApp().globalData.treeDirty = false;
        this.loadTree(this.data.view, this.data.anchorId, true);
      } else {
        wx.showToast({ title: '已提交，等房长确认', icon: 'none' });
      }
    } catch (e) {
      wx.hideLoading();
      // 40301 = 你还没认领，走答题自证；其余照实说
      if (e && e.code === 40301) {
        wx.showModal({
          title: '先确认您是哪一位',
          content: '补录要记在谁名下，所以得先对上「我」。答两个名字就行。',
          confirmText: '去确认',
          success: (r) => {
            if (r.confirm) wx.navigateTo({ url: '/pages/claim/claim' });
          },
        });
        return;
      }
      wx.showToast({ title: (e && e.message) || '提交失败', icon: 'none' });
    }
  },

  async openPerson(id) {
    if (!id) return;

    this.setData({
      sheetShow: true,
      sheetLoading: true,
      sheetPerson: null,
      selectedId: id,
    });

    // 先用树上的数据把卡片撑起来（有名字、有世次），详情回来再补字段。
    // 这样点下去立刻有反应，不会出现"点了没动静"的几百毫秒空白。
    const brief = this._nodesMap[id];
    if (brief) {
      this.setData({ sheetPerson: this._toSheetPerson(brief) });
    }

    try {
      const p = await get(`/person/${id}`, {}, { silent: true });
      // 卡片可能已经被用户关了或者换了人，别用过期响应覆盖
      if (this.data.selectedId === id) {
        this.setData({ sheetPerson: p, sheetLoading: false });
      }
    } catch (e) {
      this.setData({ sheetLoading: false });
    }
  },

  _toSheetPerson(n) {
    return {
      id: n.id,
      displayName: n.displayName || n.name || '未详',
      name: n.name,
      gender: n.gender,
      generation: n.gen,
      lineageRole: n.role,
      livingStatus: n.living,
      isSelf: !!n.isSelf,
      myPermissions: {
        canEdit: false,
        canClaim: false,
        alreadyClaimedByMe: false,
      },
    };
  },

  onSheetClose() {
    this.setData({ sheetShow: false, selectedId: null });
  },

  onSheetAction(e) {
    const { type, personId } = e.detail;

    if (type === 'detail') {
      wx.navigateTo({ url: `/pages/person/person?id=${personId}` });
    } else if (type === 'edit') {
      wx.navigateTo({ url: `/pages/person/person?id=${personId}&mode=edit` });
    } else if (type === 'claim') {
      wx.navigateTo({ url: `/pages/claim/claim?personId=${personId}` });
    } else if (type === 'fillmine') {
      wx.navigateTo({ url: `/pages/fill/fill?personId=${personId}` });
    }
  },

  onSheetChip(e) {
    // 点亲属 chip 直接在卡片里换人，不关卡片。
    // 「父 → 祖 → 曾祖」一路往上点是家谱最自然的浏览方式。
    this.openPerson(e.detail.personId);
  },

  // ============================================================
  // 换族（多族）
  // ============================================================
  //
  // 为什么要有这个出口：一个账号可以同属父系谱和母系谱，但 `user.family_id`
  // 一次只能指向一个族。没有出口的话，从母系谱的码进来之后，父系谱就再也
  // 回不去了——除非清缓存，而清缓存没用（openid 在微信侧，family_id 在后端库里）。

  async onOpenFamilies() {
    // 没有 familyId = 还没进族（或 init 还没跑完）。给一句话，别静默返回——
    // 点了完全没反应，用户会以为是按钮坏了，然后一直点。
    if (!this.data.familyId) {
      wx.showToast({ title: '还没进入家族，请先输家族码', icon: 'none' });
      return;
    }
    if (this._famBusy) return;
    this._famBusy = true;

    let list = [];
    try {
      const d = await this._loadFamilies();
      list = (d && d.families) || [];
    } catch (e) {
      this._famBusy = false;
      // 原因必须写出来。之前只有一句"拿不到家族列表"，而后端 404（接口没上线）、
      // 40100（token 失效）、-1（网络不通）三种情况在这里长得一模一样，
      // 2026-10-17 就因为这个多猜了一轮。现在直接把码和原文摆出来。
      const tag = e && e.code === -1 ? '网络不通'
        : e && e.code ? '错误码 ' + e.code : '';
      wx.showModal({
        title: '拿不到家族列表',
        content: (e && e.message ? e.message : '请稍后重试') + (tag ? '\n（' + tag + '）' : ''),
        showCancel: false,
        confirmText: '知道了',
      });
      return;
    }
    this._famBusy = false;

    // 只在一个族：直接问"要不要加一个"，省掉一次只有一个选项的选择
    if (list.length <= 1) {
      this._askAddFamily();
      return;
    }

    const ADD = '＋ 加入其他家族';
    // showActionSheet 最多 6 项。族比这还多的场景首期不存在，超了先截断——
    // 截断比崩掉强，而且下面照样有"加入其他家族"这条出路。
    const shown = list.slice(0, 5);
    const labels = shown.map((f) => {
      const tail = f.current ? ' · 当前' : f.roleText && f.roleText !== '族人' ? ' · ' + f.roleText : '';
      return f.name + tail;
    });
    labels.push(ADD);

    wx.showActionSheet({
      itemList: labels,
      success: (res) => {
        if (res.tapIndex === shown.length) {
          this.goAddFamily();
          return;
        }
        const target = shown[res.tapIndex];
        // 点当前族等于"什么都不做"。仍然给个轻提示，否则用户会以为点空了
        if (target.current) {
          wx.showToast({ title: '正在看这个族', icon: 'none' });
          return;
        }
        this.switchFamily(target);
      },
      fail: () => {},
    });
  },

  /**
   * 拉"我加入的全部族"。token 过期就重登一次再拉。
   *
   * 为什么要重试：JWT 有效期 7 天，而首页的树是**匿名可读**的，
   * 所以 token 失效后首页照常显示，只有点"切换"这种要登录的动作才暴露。
   * 手写这个重试而不是让用户重进小程序——40100 在用户眼里就是"点了没反应"。
   */
  async _loadFamilies() {
    try {
      return await get('/family/list', {}, { silent: true });
    } catch (e) {
      if (!e || e.code !== 40100) throw e;
      // request.js 在 40100 时已经清掉了 token 和 session，这里重登即可
      const g = getApp();
      g.globalData.token = '';
      await g.ensureLogin();
      return await get('/family/list', {}, { silent: true });
    }
  },

  _askAddFamily() {
    wx.showModal({
      title: '加入其他家族',
      content: '父系谱、母系谱可以同时加。输入另一个家族的家族码就能加进来，随时在这里切换。',
      confirmText: '去输入',
      success: (res) => {
        if (res.confirm) this.goAddFamily();
      },
    });
  },

  goAddFamily() {
    wx.navigateTo({ url: '/pages/landing/landing' });
  },

  async switchFamily(target) {
    wx.showLoading({ title: '切换中', mask: true });
    try {
      const d = await post('/family/switch', { familyId: target.familyId }, { silent: true });
      const g = getApp();
      g.globalData.familyId = target.familyId;
      // user 是登录那一刻的快照，切族后角色和认领人都变了，必须同步，
      // 否则端上还拿着"我在原来那个族是房长"的判断去显示管理入口。
      if (g.globalData.user) {
        g.globalData.user.familyId = target.familyId;
        g.globalData.user.role = d.role;
        g.globalData.user.claimedPersonId = d.claimedPersonId;
        g.globalData.user.needClaim = d.needClaim;
      }
      wx.hideLoading();
      // 用 reLaunch 而不是 setData 刷树：home 里散着 _rev / _scroll /
      // _nodesMap / scale 一堆非 data 状态，逐个重置迟早漏一个，
      // 症状是"新族的树按老族的缩放和滚动位置显示"。
      wx.reLaunch({ url: '/pages/home/home' });
    } catch (e) {
      wx.hideLoading();
      // 40300 = 没加入过这个族，正常不会走到（列表里都是已加入的），
      // 但后端拒绝时要说清楚，而不是静默什么都不发生。
      if (e && e.code === 40300) {
        wx.showToast({ title: '你还没有加入这个家族', icon: 'none' });
      }
    }
  },

  // ============================================================
  // 找人
  // ============================================================
  //
  // 原来是 wx.showModal 弹一个系统输入框。问题在于：它长得像"确认删除"，
  // 用户不确定自己是在搜索还是在填表；而且一次只能给一个结果，
  // 而谱里同名是常态（"启"字辈常有三个启华），只能让他一个个试。
  // 现在是一个真的搜索层：边打字边列候选，带世次，点谁是谁。

  onSearch() {
    this.setData({ searchShow: true, searchKw: '', searchHits: [] });
  },

  onSearchClose() {
    if (this._sTimer) {
      clearTimeout(this._sTimer);
      this._sTimer = null;
    }
    this.setData({ searchShow: false, searchKw: '', searchHits: [], searchLoading: false });
  },

  onSearchInput(e) {
    const kw = (e.detail.value || '').trim();
    this.setData({ searchKw: kw });

    if (this._sTimer) clearTimeout(this._sTimer);
    if (!kw) {
      this.setData({ searchHits: [] });
      return;
    }
    // 300ms 防抖：一个族 500+ 人，每敲一个字就查一次后端没必要
    this._sTimer = setTimeout(() => this._fetchHits(kw), 300);
  },

  onSearchConfirm(e) {
    const kw = ((e.detail && e.detail.value) || '').trim();
    if (!kw) return;
    if (this._sTimer) clearTimeout(this._sTimer);
    this._fetchHits(kw);
  },

  async _fetchHits(kw) {
    if (!this.data.familyId) return;
    this.setData({ searchLoading: true });
    try {
      const data = await get(
        `/tree/${this.data.familyId}/search`,
        { q: kw },
        { silent: true }
      );
      // 请求回来时输入框可能已经变了，别用过期的结果盖掉新的
      if (this.data.searchKw !== kw) return;
      this.setData({ searchHits: (data.items || []).slice(0, 20), searchLoading: false });
    } catch (e) {
      this.setData({ searchHits: [], searchLoading: false });
    }
  },

  onSearchPick(e) {
    const id = Number(e.currentTarget.dataset.id);
    const role = Number(e.currentTarget.dataset.role) || 0;
    if (!id) return;
    this.setData({ searchShow: false, searchKw: '', searchHits: [] });
    this.focusPerson(id, role);
  },

  /**
   * 搜到一个人之后带他去树里的位置。
   *
   * 预期（2026-11-02 实报）：搜人不是想先读资料卡，是想看"他在族里哪儿"——
   * 直接跳进全族大宏图，把他居中 + 烫金高亮。原实现是换锚点重拉卡片流
   * 再弹半屏详情，等于多绕一层。
   *
   * 大宏图只画本族丁口（role 1 男丁 / 2 本族女儿）；媳妇/女婿不在树上，
   * 这类人落回个人页，别让他对着始祖发呆。
   */
  focusPerson(id, role) {
    if (role && role !== 1 && role !== 2) {
      wx.navigateTo({ url: `/pages/person/person?id=${id}` });
      return;
    }

    // 已经在大宏图里：不重拉，直接居中 + 高亮
    if (this.data.view === 'overview' && this._nodesMap && this._nodesMap[id]) {
      this.setData({ ovFocus: id });
      // ovFocus 没变时 observer 不触发，手动补一枪定位
      const cv = this.selectComponent('#ovcanvas');
      if (cv && cv.focusOn) cv.focusOn(id);
      return;
    }

    // 换视图：定位意图交给 loadTree 的 _focusId 消费（一次即弃）
    wx.setNavigationBarTitle({ title: '全族大宏图' });
    this.setData({ view: 'overview', anchorId: null, sheetShow: false, selectedId: null });
    this._focusId = id;
    this.loadTree('overview', null, true);
  },

  // ============================================================
  // 其他入口
  // ============================================================

  onGoClaim() {
    wx.navigateTo({ url: '/pages/claim/claim' });
  },

  onGoFill() {
    const g = getApp();
    // 底部条上的数字属于 pending.personId（可能是当前聚焦的配偶/长辈），
    // 不一定是我。原来固定跳 claimedPersonId，就出现"条上说吴敏还差一项，
    // 点进去说陈启华已补全"——同一条入口说两个人。
    // 只有在没有 pending 数据时才回落到自己。
    const p = this.data.pending || {};
    const pid = p.personId
      || (g.globalData.user && g.globalData.user.claimedPersonId);
    if (!pid) {
      this.onGoClaim();
      return;
    }
    wx.navigateTo({ url: `/pages/fill/fill?personId=${pid}` });
  },

  onRetry() {
    // 连宗族都还没定下来时，"重拉树"没有意义——familyId 是空的，
    // 而且很可能这次就能拿到族了。所以从入族那一步重走。
    if (this.data.loadError) {
      this.setData({ loading: true, loadError: '', loadErrorHint: '' });
      this.init();
      return;
    }
    this.loadTree(this.data.view, this.data.anchorId, true);
  },
});
