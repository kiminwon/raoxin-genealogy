// 落地页 —— 进入宗族的**唯一入口**，一页两态。
//
//   A 有码：从家族群点链接 / 扫小程序码进来 → 直接显示预览
//   B 无码：自己打开小程序，或上一个码已失效     → 显示输码表单
//
// 为什么合成一页而不是做两个页面：两态做的是同一件事——"拿到一个码，
// 进入某个宗族"，区别只是码从哪来（链接里带 vs 手输）。分两页就要把
// "入族"这个动作维护两遍，而且迟早会分叉。
//
// 为什么无码时只给输入框、不给任何族的信息：
//   家谱里**已故先人**的姓名对外是完整可查的（脱敏只对在世者生效），
//   所以门槛必须留着。但这道门槛只在"进族"这一刻问一次——
//   进来之后各页面不该再追问"你当初怎么进来的"。
//
// 这一页不发树的请求——只调 /invite/{code} 一个接口拿全部内容。
// 落地页必须快：家族群里点开链接的人是"顺便看看"的心态，
// 多等一秒就关掉了。

const { get, post } = require('../../utils/request');

Page({
  data: {
    loading: true,
    // input 输码 | preview 预览
    //
    // 注意没有单独的"码无效"态：链接失效时直接退回 input，把原因写在
    // codeError 里。用户此时手里往往还有一个别的码，当场能换一个，
    // 而不是撞上一堵写着"链接已失效"的墙。
    stage: 'input',

    code: '',
    codeInput: '',
    codeError: '',
    looking: false,
    // 输入框焦点受控。为什么必须受控：见 onLookup 里的说明——
    // 切到预览态之前必须先失焦，否则一加等安卓机型上原生 input 会残留在页面上。
    inputFocus: false,

    accepting: false,
    codeType: 1, // 1 浏览码 / 2 历史编辑码（已停止签发）
    // 分享链接带过来的锚点：发链接的那位家人。不带则 0。
    // 它**只影响"先看谁"**，不影响能改什么——写权限一律由认领后的 edit_scope 决定。
    anchorPersonId: 0,
    family: {},
    inviter: {},
    anchor: {},
    preview: { generations: [] },
    scopePreview: null,

    primaryText: '进去看看',
  },

  onLoad(query) {
    let code = (query && query.code) || '';
    let anchor = parseInt(query && query.anchor, 10) || 0;

    // 扫小程序码进来时参数在 scene 里（上限 32 字符，所以用短格式）。
    // 支持两种写法：
    //   scene=CHEN2026          只有族码（打印在祠堂/族谱封底上）
    //   scene=CHEN2026-123      族码 + 分享人 personId
    // 也兼容 URL 编码的 "code=CHEN2026&anchor=123"。
    if (!code && query && query.scene) {
      const raw = decodeURIComponent(query.scene);
      if (raw.includes('=')) {
        raw.split('&').forEach((kv) => {
          const [k, v] = kv.split('=');
          if (k === 'code' && v) code = v;
          if (k === 'anchor' && v) anchor = parseInt(v, 10) || 0;
        });
      } else {
        const parts = raw.split('-');
        code = parts[0] || '';
        if (parts[1]) anchor = parseInt(parts[1], 10) || 0;
      }
    }

    code = String(code || '').trim().toUpperCase();

    // 分享链接：?code=CHEN2026&anchor=123
    if (anchor > 0) {
      this.setData({ anchorPersonId: anchor });
      // 落到 globalData，认领页是另一条路径（redirectTo），带不过 data，
      // 认领完 reLaunch 回首页时还得知道"先看谁"
      getApp().globalData.shareAnchor = anchor;
    }

    if (code) {
      // 先回填输入框：万一这个码已经失效，用户退回输码态时看到的是
      // 刚才那个码，改一两个字符就能再试，不用从头敲
      this.setData({ codeInput: code });
      this.load(code);
      return;
    }

    this.setData({ loading: false, stage: 'input' });
  },

  // ============================================================
  // 一、查码
  // ============================================================

  async load(code) {
    this.setData({ loading: true, codeError: '' });

    try {
      // 锚点只喂给预览：落地页的三代、以及"这一支从谁说起"，都跟着分享人走。
      // 没有锚点（自己在小程序里输码）时按接口默认的始祖三代。
      const params = {};
      if (this.data.anchorPersonId > 0) params.previewPersonId = this.data.anchorPersonId;
      const d = await get(`/invite/${code}`, params, { silent: true });

      const fam = d.family || {};
      this.setData({
        loading: false,
        stage: 'preview',
        code,
        codeInput: code,
        codeError: '',
        codeType: d.codeType,
        family: fam,
        inviter: d.inviter || {},
        anchor: d.anchor || {},
        preview: d.preview || { generations: [] },
        scopePreview: d.scopePreview || null,
        // 浏览码进来看全族，编辑码进来是"填你家这一支"，文案要分开
        primaryText: d.codeType === 2 ? '这就是我家，我来补' : '进去看看我家的谱',
      });

      if (fam.name) {
        wx.setNavigationBarTitle({ title: fam.name });
      }
    } catch (e) {
      this.setData({
        loading: false,
        stage: 'input',
        codeInput: code,
        codeError: this._codeErrorText(e),
      });
    }
  },

  /**
   * 码不认时的说法要具体，因为用户正一手攥着家人发给他的码在输。
   *
   * 40302 是后端统一的"码无效 / 过期 / 用完"，这里不再细分——
   * 对用户来说下一步动作是同一个：再要一个。
   */
  _codeErrorText(e) {
    if (e && e.code === -1) return '连不上服务器，请确认网络后重试';
    if (e && e.code === 40302) return e.message || '这个码已经用不了了，请让家人重新发一个';
    return (e && e.message) || '没查到对应的家族，请核对后再试';
  },

  onCodeInput(e) {
    this.setData({
      codeInput: String(e.detail.value || '').toUpperCase(),
      codeError: '',
    });
  },

  /** 用户点进输入框。受控 focus 的另一半：状态要跟真实焦点对齐。 */
  onCodeFocus() {
    this.setData({ inputFocus: true });
  },

  async onLookup() {
    // ⚠️ 先失焦，再查码 —— 顺序不能反。
    //
    // 原生 input 在部分安卓 ROM（2026-10-17 一加真机实测）上同层渲染会失败：
    // 它浮在所有元素之上，而且 wx:if 把它销毁之后**原生层并不跟着回收**，
    // 结果就是输完码、切到预览态了，"CHEN2026"还带着光标浮在页面中间。
    // 主动把 focus 交给 false 让它失焦，原生层才会随焦点释放被正常回收；
    // hideKeyboard 顺带把键盘收起来，不然键盘还会挡住预览内容。
    //
    // 为什么敢在这里失焦：load() 里有一次网络往返（几百毫秒起），
    // 焦点释放早就在 setData 切态之前完成了，不需要再等定时器。
    if (this.data.inputFocus) this.setData({ inputFocus: false });
    try {
      wx.hideKeyboard();
    } catch (e) {
      // 没有键盘时不报错就行
    }

    if (this.data.loading || this.data.looking) return;

    const code = String(this.data.codeInput || '').trim().toUpperCase();
    if (!code) {
      this.setData({ codeError: '请先输入家族码' });
      return;
    }

    this.setData({ looking: true });
    await this.load(code);
    this.setData({ looking: false });
  },

  /** 从预览退回输码：换一个家族，或者手里其实有两个码 */
  onBackToInput() {
    this.setData({ loading: false, stage: 'input', codeError: '' });
  },

  // ============================================================
  // 二、入族
  // ============================================================

  /**
   * 接受邀请。
   *
   * 顺序很重要：先登录（拿 token），再 accept（落授权、绑族），最后跳走。
   * 未登录就 accept 会被 40100 拒掉，所以这里强制先走一次登录。
   *
   * 这一步做完，user.family_id 才真的有了值——后面所有页面
   * （包括答题自证）都以它为"我属于哪个族"的唯一依据。
   */
  async onAccept() {
    if (this.data.accepting) return;
    this.setData({ accepting: true });

    const g = getApp();

    try {
      if (!g.globalData.token) {
        await g.ensureLogin();
      }

      // 锚点放 body：POST 的参数不拼进 URL，也避免 query 拼串被工具误当成路径段
      const payload = this.data.anchorPersonId > 0
        ? { anchorPersonId: this.data.anchorPersonId }
        : {};
      const data = await post(`/invite/${this.data.code}/accept`, payload, { silent: true });

      if (data && data.familyId) {
        g.globalData.familyId = data.familyId;
        // user 是登录那一刻的快照。入族/切族之后必须把整份快照同步过来——
        // 这三列在后端是"当前族快照"，切到另一个族时 role 和认领人都会变。
        // 只同步 familyId 的话，会出现"端上以为我已经认领过（旧族的那个人），
        // 实际在新族里没认领"，下一次写操作才被 40301 挡回来。
        if (g.globalData.user) {
          g.globalData.user.familyId = data.familyId;
          if (data.role !== undefined) g.globalData.user.role = data.role;
          if (data.claimedPersonId !== undefined) {
            g.globalData.user.claimedPersonId = data.claimedPersonId;
          }
          if (data.needClaim !== undefined) g.globalData.user.needClaim = data.needClaim;
        }
      }
      // 让首页知道数据变了，下次 onShow 会重拉
      g.globalData.treeDirty = true;
      // 进来先看谁：以服务端回的为准（它已经校验过这个人在本族），
      // 而不是只信链接里的 query——链接可能带着别的族的人。
      if (data && data.recommendAnchor) {
        g.globalData.shareAnchor = data.recommendAnchor;
      }
      // 这个码已经消费过了，别再让首页重复跳回来
      g.globalData.pendingInviteCode = '';

      const nodes = (data && data.grantedScopes && data.grantedScopes.nodeCount) || 0;

      // needClaim 用后端这次返回的值：认领状态是**按族**算的，
      // 他在 A 族认领过陈雨欣，不代表在 B 族也认领了谁。
      const needClaim = data && data.needClaim !== undefined
        ? data.needClaim
        : !(g.globalData.user && g.globalData.user.claimedPersonId);

      if (needClaim) {
        // 直接带他去确认身份。这是整条链路上最关键的一跳：
        // 没有确认身份，他下次不会再打开这个小程序。
        //
        // 注意这里**不带 personId**：认领是"答两个名字，系统反查"，
        // 不是"在树上点自己"——认领前他在树上看到的是「陈*明」，点不准。
        wx.redirectTo({ url: '/pages/claim/claim' });
        return;
      }

      if (nodes > 0) {
        wx.showToast({ title: `已开放 ${nodes} 个节点`, icon: 'none', duration: 1800 });
      }

      this.goHome();
    } catch (e) {
      this.setData({ accepting: false });

      // 40302：码失效 / 用完 / 过期——退回输码态，他当场就能换一个
      if (e.code === 40302) {
        this.setData({ stage: 'input', codeError: e.message || '这个码已经不能用了' });
        return;
      }
      // 其他错误（比如后端没起）已经在 request 层弹过 toast
    }
  },

  onSkip() {
    this.goHome();
  },

  goHome() {
    setTimeout(() => {
      wx.reLaunch({ url: '/pages/home/home' });
    }, 400);
  },
});
