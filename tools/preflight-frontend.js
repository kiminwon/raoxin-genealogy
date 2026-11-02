/**
 * 前端预检：用小程序自己的代码，跑真实后端数据。
 *
 * ## 为什么需要这个
 *
 * 开发者工具能告诉你"页面长什么样"，但白屏、NaN、空白节点这类问题
 * 排查起来很慢——你看到的只是一个空页面，不知道是没拿到数据、
 * 拿到了但字段名不对、还是算坐标时除了个 undefined。
 *
 * 好在 miniprogram/utils/ 下的 tree-lines.js 和 format.js 是**纯函数**，
 * 不碰 wx.*，所以在 Node 里就能直接跑。于是可以把"页面为什么空"
 * 提前拆成三个能分别验证的问题：
 *
 *   1. 接口通不通、字段在不在      → 这一段
 *   2. 坐标算不算得出（有没有 NaN） → 拿真实数据跑 buildLines()
 *   3. 文案有没有渲染成 undefined   → 拿真实数据跑 format.js
 *
 * 这三步过了，再进开发者工具，剩下的基本只剩样式问题。
 *
 * ## 用法
 *
 *   node tools/preflight-frontend.js
 *   node tools/preflight-frontend.js http://127.0.0.1:8000
 *
 * 只读，不改任何数据。
 */

'use strict';

const path = require('path');
const fs = require('fs');

const BASE = (process.argv[2] || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const ROOT = path.resolve(__dirname, '..');
const MP = path.join(ROOT, 'miniprogram');

const { EDGE, buildLines } = require(path.join(MP, 'utils/tree-lines.js'));
const fmt = require(path.join(MP, 'utils/format.js'));

// 演示数据里的固定锚点
const VIEW_CODE = 'CHEN2026';
const ANCHOR_NAME = '陈启明';
const FAMILY_ID = 1;

let passed = 0;
const failures = [];

function ok(cond, label, hint) {
  if (cond) {
    passed += 1;
    console.log(`  [PASS] ${label}`);
  } else {
    failures.push(label);
    console.log(`  [FAIL] ${label}`);
    if (hint) console.log(`         -> ${hint}`);
  }
  return cond;
}

async function api(pathname, { method = 'GET', body, token } = {}) {
  const headers = { 'content-type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = await fetch(BASE + pathname, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json;
  try {
    json = await res.json();
  } catch (e) {
    return { httpStatus: res.status, code: -1, msg: `响应不是 JSON: ${e.message}` };
  }
  return { httpStatus: res.status, ...json };
}

// ============================================================
// 一、前端调用的接口路径 vs 后端实际注册的路径
// ============================================================

const CALL_RE = /\b(get|post|patch|request)\s*\(\s*[`'"]([^`'"]+)[`'"]/g;

function normPath(p) {
  // 先剥 query / hash，再归一化占位符。
  // `/invite/${code}/accept?anchorPersonId=1` 里的 query 不是路径的一部分；
  // 不剥就会被归一化成 `/invite/{}/accept{}`，永远匹配不上后端路由，
  // 于是"路径没注册"的假警报把真问题淹掉。
  const clean = String(p).split('?')[0].split('#')[0];
  return clean.replace(/\$\{[^}]*\}/g, '{}').replace(/\{[^}]*\}/g, '{}');
}

function collectFrontendCalls() {
  const calls = [];
  const walk = (dir) => {
    for (const name of fs.readdirSync(dir)) {
      const full = path.join(dir, name);
      const st = fs.statSync(full);
      if (st.isDirectory()) {
        if (name !== 'node_modules') walk(full);
        continue;
      }
      if (!name.endsWith('.js')) continue;
      const src = fs.readFileSync(full, 'utf8');
      let m;
      CALL_RE.lastIndex = 0;
      while ((m = CALL_RE.exec(src))) {
        const line = src.slice(0, m.index).split('\n').length;
        calls.push({
          file: path.relative(MP, full).replace(/\\/g, '/'),
          line,
          method: m[1].toUpperCase(),
          raw: m[2],
          norm: '/v1' + normPath(m[2]),
        });
      }
    }
  };
  walk(MP);
  return calls;
}

async function checkPaths() {
  console.log('【1】前端调用的接口，后端是否真的注册了');
  const spec = await api('/openapi.json');
  if (!spec.paths) {
    ok(false, '能取到 OpenAPI 文档', `GET /openapi.json 没返回 paths`);
    return;
  }
  const registeredRaw = Object.keys(spec.paths);
  const registered = new Set(registeredRaw.map(normPath));

  const calls = collectFrontendCalls();
  const uniq = new Map();
  for (const c of calls) {
    if (!uniq.has(c.norm)) uniq.set(c.norm, c);
  }
  ok(uniq.size > 0, `从 miniprogram/ 提取到 ${uniq.size} 个不同接口路径`);

  const missing = [];
  for (const [norm, c] of uniq) {
    if (!registered.has(norm)) missing.push({ norm, c });
  }

  ok(
    missing.length === 0,
    `全部路径都在后端注册（${uniq.size - missing.length}/${uniq.size}）`,
    missing
      .map((m) => `${m.c.file}:${m.c.line} 调 ${m.c.raw} → 归一化 ${m.norm}，后端没有`)
      .join('\n            ')
  );

  // 反向：后端有、前端没调（只提示，不算失败）
  const usedNorm = new Set(uniq.keys());
  const unused = registeredRaw.filter((p) => !usedNorm.has(normPath(p)) && p !== '/health');
  if (unused.length) {
    console.log(`  [INFO] 后端有但前端还没用的接口 ${unused.length} 个：`);
    console.log('         ' + unused.sort().join('\n         '));
  }
}

// ============================================================
// 二、拿真实数据跑前端的 buildLines()，看坐标算不算得出来
// ============================================================

function inspectLines(nodes, edges, tag) {
  console.log(`\n  --- ${tag} ---`);
  ok(nodes.length > 0, `${tag}：有节点（${nodes.length}）`);

  const bad = nodes.filter(
    (n) =>
      !Number.isFinite(n.x) || !Number.isFinite(n.y) ||
      !Number.isFinite(n.w) || !Number.isFinite(n.h)
  );
  ok(
    bad.length === 0,
    `${tag}：每个节点的 x/y/w/h 都是有限数字（${nodes.length - bad.length}/${nodes.length}）`,
    bad.length
      ? `坏节点 id=${bad.slice(0, 5).map((n) => n.id).join(',')}；` +
        `示例 ${JSON.stringify(nodes[0])}。缺 w/h 时端上算连线会得到 NaN`
      : ''
  );

  const lines = buildLines(nodes, edges);
  const broken = lines.filter(
    (l) => /NaN|undefined|null/.test(l.style || '')
  );
  ok(
    broken.length === 0,
    `${tag}：buildLines 产出 ${lines.length} 段线，样式里没有 NaN/undefined`,
    broken.length ? `坏样式：${JSON.stringify(broken.slice(0, 3))}` : ''
  );

  if (edges.length > 0) {
    ok(lines.length > 0, `${tag}：有 ${edges.length} 条边，确实画出了线`);
  }

  // 每条边都要能在节点表里找到两端，否则前端会静默跳过（线少一截，不报错）
  const ids = new Set(nodes.map((n) => n.id));
  const dangling = edges.filter((e) => !ids.has(e.from) || !ids.has(e.to));
  ok(
    dangling.length === 0,
    `${tag}：没有悬空边（${edges.length - dangling.length}/${edges.length} 两端都在节点表里）`,
    dangling.length ? `悬空示例：${JSON.stringify(dangling.slice(0, 3))}` : ''
  );

  // 婚姻线：buildLines 专门处理 type=2，用来把亲子线对齐到夫妻中心
  const marry = edges.filter((e) => e.type === EDGE.MARRY);
  const marryLines = lines.filter((l) => l.cls.includes('ln-marry'));
  ok(
    marry.length === 0 || marryLines.length > 0,
    `${tag}：${marry.length} 条婚姻边画出了 ${marryLines.length} 段金色横线`,
    marry.length && !marryLines.length
      ? '一条都没画出来——多半是夫妻 y 坐标不等或间距为 0'
      : ''
  );

  return { lines: lines.length, nodes: nodes.length, edges: edges.length };
}

// ============================================================
// 三、拿真实数据跑 format.js，看文案会不会渲染成 undefined
// ============================================================

function inspectFormat(nodes, person, tag) {
  console.log(`\n  --- ${tag} 文案 ---`);
  const labels = nodes.map((n) => fmt.nodeLabel(n));
  ok(
    labels.every((s) => s && s !== 'undefined' && s !== 'null'),
    `${tag}：${labels.length} 个节点标签都非空`,
    labels.every((s) => s) ? '' : `空标签示例：${JSON.stringify(labels.filter((s) => !s).slice(0, 5))}`
  );

  const gens = nodes.map((n) => fmt.genLabel(n.gen));
  ok(
    gens.every((s) => s !== 'undefined'),
    `${tag}：世次文案正常（样本 ${[...new Set(gens)].sort().join(' / ')}）`
  );

  if (person) {
    const life = fmt.lifeSpan(person);
    ok(
      !/undefined|NaN/.test(life),
      `人物详情：生卒文案「${life}」没有 undefined`,
      `livingStatus=${person.livingStatus} birth=${JSON.stringify(person.birth)}`
    );
    const role = fmt.roleText(person.lineageRole);
    ok(
      person.lineageRole === undefined || (role !== undefined),
      `人物详情：谱系角色「${role}」能翻译（lineageRole=${person.lineageRole}）`,
      role === '' ? '后端给了 role 但前端对照表里没有这个值' : ''
    );
    const span = fmt.dateText(person.birth);
    ok(
      !/undefined/.test(span),
      `人物详情：出生日期「${span}」能取到 display`
    );
  }
}

// ============================================================
// main
// ============================================================

async function main() {
  console.log(`前端预检：${BASE}`);
  console.log(`前端代码：${MP}\n`);

  // ---------- 【0】登录身份与 token 环境标记（纯逻辑，不需要后端）----------
  //
  // 这一段的背景：曾经所有人进体验版都是同一个身份（陈雨欣），因为 code 被
  // 写死成 dev:mock_new_member。改法是"按连哪个后端分流 + token 绑环境"，
  // 而这两条规则都在端上、都靠人记着，所以钉成断言。
  console.log('\n【0】登录身份与 token 环境标记');
  const env = require(path.join(MP, 'utils/env.js'));

  const expectMode = env.DEV_MODE && env.BACKEND === 'local' ? 'dev' : 'wx';
  ok(
    env.LOGIN_MODE === expectMode,
    `LOGIN_MODE=${env.LOGIN_MODE}（BACKEND=${env.BACKEND}，应为 ${expectMode}）`,
    'dev: 身份只被本机后端接受（WX_MOCK_LOGIN=true）。连云端还发 dev: 会换来 40029，登录全挂。'
  );

  ok(
    env.ALLOW_DEMO_FAMILY === (env.DEV_MODE && env.BACKEND === 'local'),
    `演示族兜底=${env.ALLOW_DEMO_FAMILY}（只允许在本机后端）`,
    '云端问不到后端时必须报错：兜一棵别人的树出来，已故先人的全名就露了。'
  );

  // token 与环境绑定：换后端 / 换身份来源，旧 token 必须当场作废
  const store = {};
  global.wx = {
    getStorageSync: (k) => (k in store ? store[k] : ''),
    setStorageSync: (k, v) => {
      store[k] = v;
    },
    removeStorageSync: (k) => {
      delete store[k];
    },
  };
  const session = require(path.join(MP, 'utils/session.js'));
  session.save('T1');
  ok(
    session.load() === 'T1' && session.tag() === `${env.BACKEND}:${env.LOGIN_MODE}`,
    `token 存取正常，环境标记=${session.tag()}`
  );
  store.tokenTag = 'cloud:dev';
  ok(
    session.load() === '' && !('token' in store),
    '换了身份来源，旧 token 被丢弃',
    '不丢的话，改完登录方式手机上还用老身份跑——"所有人都是陈雨欣"就是这么来的。'
  );

  // ---------- 【0.1】冷启动身份：token 在、user 空 ----------
  //
  // token 存在 storage 里，`globalData.user` 只在内存里 —— 小程序被杀掉重进之后
  // user 一定是空的。曾经 ensureLogin() 见 token 就 `return user`（undefined），
  // 于是首页拿不到 claimedPersonId，pickAnchor 退回"全族最上面那位"：
  // 用户进「我的一家」看到的是始祖那三代，自己根本不在画面上，
  // 必须先点一遍「全族总览」再点回来（那时 user 已被别的路径填上）才聚焦到自己。
  // 这两条断言钉住修复，防止以后有人又把兜底"简化"掉。
  console.log('\n【0.1】冷启动身份（纯逻辑：模拟"杀掉小程序再进"）');

  const appSrc = fs.readFileSync(path.join(MP, 'app.js'), 'utf8');
  const ensureFn = (appSrc.match(/async ensureLogin\(\)\s*\{[\s\S]*?\n  \}/) || [''])[0];
  ok(!!ensureFn, '找得到 app.js::ensureLogin（端上唯一的静默登录入口）');
  ok(
    /fetchMe/.test(ensureFn) && ensureFn.indexOf('fetchMe') < ensureFn.indexOf('auth.login()'),
    'ensureLogin 在「token 在、user 空」时先用 /auth/me 补身份，再考虑重新 login',
    '只 `return this.globalData.user` 等于把 undefined 当"已登录"，冷启动后拿不到 claimedPersonId。'
  );

  const homeSrc = fs.readFileSync(path.join(MP, 'pages/home/home.js'), 'utf8');
  const initFn = (homeSrc.match(/async init\(\)\s*\{[\s\S]*?\n  \},/) || [''])[0];
  ok(!!initFn, '找得到 home.js::init');
  ok(
    initFn.indexOf('ensureLogin') > -1 &&
      initFn.indexOf('ensureLogin') < initFn.indexOf('loadTree('),
    'init 在拉树之前先等身份到位（锚点靠它才定得下来）',
    '顺序反了就会先按"没认领"兜底成始祖，视图落在始祖那一代。'
  );

  // 光看源码字符串不够——真跑一遍 ensureLogin，状态摆成"冷启动"：
  // token 在、globalData.user 为空。正确行为是补一次 /auth/me 把身份拿回来，
  // 而不是重走 wx.login（那会平白多一次登录往返、还可能换掉 token）。
  {
    const authMod = require(path.join(MP, 'utils/auth.js'));
    const realFetchMe = authMod.fetchMe;
    const realLogin = authMod.login;
    let fetchMeCalls = 0;
    let loginCalls = 0;
    authMod.fetchMe = async () => {
      fetchMeCalls += 1;
      return { loggedIn: true, user: { id: 9, claimedPersonId: 18, familyId: 1, role: 1 } };
    };
    authMod.login = async () => {
      loginCalls += 1;
      throw new Error('冷启动不该重走登录');
    };

    let appCfg = null;
    global.App = (cfg) => {
      appCfg = cfg;
    };
    require(path.join(MP, 'app.js'));
    const appInst = Object.create(appCfg);
    appInst.globalData = Object.assign({}, appCfg.globalData, { token: 'T1', user: null });
    global.getApp = () => appInst;

    let coldUser = null;
    let coldErr = null;
    try {
      coldUser = await appInst.ensureLogin();
    } catch (e) {
      coldErr = e;
    }
    ok(
      !coldErr && coldUser && coldUser.claimedPersonId === 18,
      `冷启动 ensureLogin 补回了身份（claimedPersonId=${coldUser && coldUser.claimedPersonId}，` +
        `fetchMe ${fetchMeCalls} 次 / login ${loginCalls} 次）`,
      coldErr ? coldErr.message : '返回的 user 里没有 claimedPersonId，首页锚点会退回始祖。'
    );
    ok(
      loginCalls === 0 && fetchMeCalls === 1,
      '冷启动只补 /auth/me，不重走 wx.login',
      `实际 login ${loginCalls} 次、fetchMe ${fetchMeCalls} 次`
    );

    authMod.fetchMe = realFetchMe;
    authMod.login = realLogin;
    delete global.App;
  }

  // 「都补上了」这句话只能由服务端说。
  // fill.js 原来凭本地队列推：用户点「以后再说」是不落库的，本地却记成"答过了"，
  // 队列一空就宣布"缺口都补上了"；服务端还记着那一条，回到详情页照样"还差 1 项"。
  // 同一件事算两遍，这已经是第四次（67% → 底部条 → 节点角标 → 补全页）。
  console.log('\n【0.2】补全页的「补完了」必须由服务端复核');

  const fillSrc = fs.readFileSync(path.join(MP, 'pages/fill/fill.js'), 'utf8');
  const showDoneFn = (fillSrc.match(/async _showDone\(d\)\s*\{[\s\S]*?\n  \},/) || [''])[0];
  ok(!!showDoneFn, '找得到 fill.js::_showDone');
  ok(
    /await\s+get\(/.test(showDoneFn) && /\/missing/.test(showDoneFn),
    '_showDone 收尾前重新拉一次 /person/{id}/missing，按服务端给的数字出文案',
    '凭本地 remaining 宣布"都补上了"，用户跳过的每一条都会在详情页变成"还差 1 项"。'
  );
  ok(
    /missingCount/.test(showDoneFn),
    '_showDone 看的是服务端的 missingCount，不是端上自己数的队列长度'
  );
  ok(
    !/remaining\s*===\s*0/.test(showDoneFn),
    '_showDone 里没有「本地队列空了就算补完」这种判断'
  );

  const fillWxml = fs.readFileSync(path.join(MP, 'pages/fill/fill.wxml'), 'utf8');
  ok(
    /onContinue/.test(fillWxml),
    '一轮问完但服务端仍有缺口时，done 页给「接着补」而不是只给「回我的一家」'
  );
  ok(
    /40300/.test(fillSrc) && /40301/.test(fillSrc),
    'fill 页同时认 40300（无权限看别人清单）和 40301（无编辑权限）',
    '只认 40301 的话，点别人的清单会掉进"读取失败"，用户看不懂也不知道该去补自己的。'
  );

  // 底部条：「还差 N 项」的数字属于锚点（可能是配偶/长辈），
  // 「去补全」却固定跳 claimedPersonId —— 于是条上写着吴敏还差一项，
  // 点进去是陈启华那一页（已完整）。数字和跳转目标必须是同一个人。
  console.log('\n【0.3】底部条的「去补全」必须跳这条数字的主人');
  const onGoFillFn = (homeSrc.match(/onGoFill\(\)\s*\{[\s\S]*?\n  \},/) || [''])[0];
  ok(!!onGoFillFn, '找得到 home.js::onGoFill');
  ok(
    /pending[\s\S]{0,120}personId|p\.personId/.test(onGoFillFn),
    'onGoFill 从 pending.personId 取人，而不是固定跳 claimedPersonId',
    '数字来自锚点、跳转来自「我」，就会出现"条上说这人还差一项、点进去说另一个人已补全"。'
  );
  ok(
    /claimedPersonId/.test(onGoFillFn) ? /pending/.test(onGoFillFn) : true,
    '只有在没有 pending 数据时才回落到自己（claimedPersonId 是兜底，不是主路径）'
  );

  const homeWxml = fs.readFileSync(path.join(MP, 'pages/home/home.wxml'), 'utf8');
  ok(
    /pending\.isSelf/.test(homeWxml),
    '底部条按 pending.isSelf 区分「我还差 N 项」和「吴敏还差 N 项」',
    '不区分的话，聚焦到别人时条上写"您这一支"，指的不是用户自己。'
  );

  let reachable = true;
  try {
    await api('/health');
  } catch (e) {
    reachable = false;
    console.log(`连不上 ${BASE}：${e.message}`);
    console.log('后端没起。启动：');
    console.log('  cd backend && .venv/Scripts/python.exe -m uvicorn \\');
    console.log('      app.main:app --host 127.0.0.1 --port 8000 --reload');
  }
  if (!reachable) return 1;

  await checkPaths();

  // ---------- 登录成陈启明（锚点本人） ----------
  console.log('\n【2】登录（开发者工具里会用 dev:mock_qiming 这个固定身份）');
  const login = await api('/v1/auth/login', {
    method: 'POST',
    body: { code: 'dev:mock_qiming', inviteCode: VIEW_CODE },
  });

  // 云端关掉 WX_MOCK_LOGIN 之后，dev: 身份在本机以外一律失效。
  // 这不是 bug，是设计——所以给一条能照着做的出路，而不是刷一屏红。
  if (login.code !== 0 && /微信登录失败|invalid code/i.test(login.msg || '')) {
    console.log(`\n  登录被拒：code=${login.code} msg=${login.msg}`);
    console.log('  目标不接受 dev: 身份（说明它 WX_MOCK_LOGIN 已关——云端正是如此）。');
    console.log('  契约预检要对**本机后端**跑：');
    console.log('    node tools/preflight-frontend.js http://127.0.0.1:8000');
    console.log('  云端那份用只读巡检：');
    console.log('    backend/.venv/Scripts/python.exe backend/scripts/check_live.py https://api.wangqiming.top');
    return 2;
  }

  ok(login.code === 0 && login.data && login.data.token, '登录成功并拿到 token',
     JSON.stringify(login).slice(0, 200));
  const token = login.data && login.data.token;
  const me = login.data && login.data.user;
  if (me) {
    ok(!!me.familyId, `用户带上了 familyId=${me.familyId}（端上靠它拉树）`);
    // 注意字段名：后端叫 claimedPersonId（不是 personId）。
    // 端上 home.js / fill.js / landing.js 都读这个名；两边写错一个字母
    // 的后果是"树永远落在始祖身上，找不到自己在哪"，而且不报错。
    ok(
      me.claimedPersonId !== undefined,
      `用户对象带 claimedPersonId=${me.claimedPersonId}（端上用它当锚点找"我在哪"）`,
      `实际字段：${Object.keys(me).join(',')}`
    );
  }

  const anchor = (me && me.claimedPersonId) || null;

  // ---------- 落地页 ----------
  console.log('\n【3】邀请落地页（家族群里点进来的第一屏）');
  const prev = await api(`/v1/invite/${VIEW_CODE}`);
  ok(prev.code === 0, `浏览码 ${VIEW_CODE} 可解析`);
  const fam = (prev.data && prev.data.family) || {};
  ok(!!fam.name, `落地页要显示的家族名「${fam.name}」`);
  ok(
    fam.personCount !== undefined && fam.generationCount !== undefined,
    `落地页要显示的人数/世数（${fam.personCount} 人 / ${fam.generationCount} 世）`
  );
  const previewGens = (prev.data && prev.data.preview && prev.data.preview.generations) || [];
  ok(previewGens.length > 0, `落地页三代预览有 ${previewGens.length} 世`);
  const p0 = (previewGens[0] && previewGens[0].persons && previewGens[0].persons[0]) || {};
  ok(
    p0.displayName !== undefined && p0.isAnchorPath !== undefined,
    `预览条目字段齐（displayName=${p0.displayName} isAnchorPath=${p0.isAnchorPath}）`,
    `实际字段：${JSON.stringify(p0).slice(0, 220)}`
  );

  // ---------- 全族总览 ----------
  console.log('\n【4】全族总览（view=overview）');
  const ov = await api(`/v1/tree/${FAMILY_ID}?view=overview`);
  ok(ov.code === 0, '总览拉取成功', `code=${ov.code} msg=${ov.msg}`);
  const ovData = ov.data || {};
  const ovNodes = ovData.nodes || [];
  const ovEdges = ovData.edges || [];
  ok(!!ovData.canvas, `返回画布尺寸（端上据此算缩放）`,
     JSON.stringify(ovData.canvas || null).slice(0, 200));
  if (ovData.canvas) {
    for (const k of ['width', 'height', 'nodeWidth', 'nodeHeight']) {
      ok(Number.isFinite(ovData.canvas[k]), `画布带 ${k}=${ovData.canvas[k]}`);
    }
  }
  inspectLines(ovNodes, ovEdges, '全族总览');
  inspectFormat(ovNodes, null, '全族总览');

  // ---------- 我的一家 ----------
  if (anchor) {
    console.log('\n【5】我的一家（view=mine，锚定陈启明）');
    const mine = await api(`/v1/tree/${FAMILY_ID}?view=mine&anchor=${anchor}`, { token });
    ok(mine.code === 0, '我的一家拉取成功', `code=${mine.code} msg=${mine.msg}`);
    const mNodes = (mine.data && mine.data.nodes) || [];
    const mEdges = (mine.data && mine.data.edges) || [];
    ok(
      mNodes.filter((n) => n.isSelf).length === 1,
      `恰好一个节点 isSelf=true（这一屏唯一要确认的就是"我在哪"）`,
      `实际 ${mNodes.filter((n) => n.isSelf).length} 个`
    );
    ok(
      mNodes.some((n) => n.onPath),
      '有节点被标成 onPath（树上那条"从始祖到我"的路径）'
    );
    // 外孙只出现在「我的一家」，这是产品决策第 2 条
    ok(
      mNodes.some((n) => n.role === 5),
      '外姓后代（role=5）出现在我的一家',
      '产品决策：外孙只在我的一家显示；这条不过说明过滤规则写反了'
    );
    inspectLines(mNodes, mEdges, '我的一家');

    // ---------- 人物详情 ----------
    console.log('\n【6】人物详情');
    const pd = await api(`/v1/person/${anchor}`, { token });
    ok(pd.code === 0, '详情拉取成功', `code=${pd.code} msg=${pd.msg}`);
    const person = pd.data || {};
    for (const k of ['name', 'displayName', 'lineageRole', 'livingStatus', 'gender', 'generation']) {
      ok(person[k] !== undefined, `详情含字段 ${k}=${JSON.stringify(person[k])}`);
    }
    ok(!!person.relations || !!person.relatives,
       '详情带了关系区块（前端"谱上记载 / 关系链"要用）',
       `实际字段：${Object.keys(person).slice(0, 25).join(',')}`);
    inspectFormat(mNodes, person, '人物详情');

    // ---------- 待补全 ----------
    console.log('\n【7】待补全清单（fill 页）');
    const ms = await api(`/v1/person/${anchor}/missing`, { token });
    ok(ms.code === 0, '待补全清单拉取成功', `code=${ms.code} msg=${ms.msg}`);
    const tasks = (ms.data && (ms.data.tasks || ms.data.items)) || [];
    ok(Array.isArray(tasks), `任务是数组，共 ${tasks.length} 条`);
    if (tasks.length) {
      const t = tasks[0];
      ok(
        t.type !== undefined && t.question !== undefined,
        `任务字段齐（type=${t.type} question=${JSON.stringify(t.question).slice(0, 40)}）`,
        `实际字段：${Object.keys(t).join(',')}`
      );
    }
  } else {
    console.log('\n【5】跳过「我的一家」：登录用户没有 personId');
  }

  // ---------- 家族卡 / 书架 / 操作环 ----------
  console.log('\n【8】家族卡、书架、操作环（这一轮新增的三块界面要用到的字段）');

  const sum = await api(`/v1/family/${FAMILY_ID}/summary`, { token });
  ok(sum.code === 0, '族信息拉取成功', `code=${sum.code} msg=${sum.msg}`);
  const sd = sum.data || {};
  ok(
    !!sd.tangHao || !!sd.junWang,
    `家族卡要显示的堂号/郡望（tangHao=${sd.tangHao} junWang=${sd.junWang}）`
  );
  ok(
    !!sd.founderName,
    `家族卡要显示的始迁祖（${sd.founderName || '空'}${sd.founderEra ? ' · ' + sd.founderEra : ''}）`
  );
  const st = sd.stats || {};
  ok(
    Number.isFinite(st.personCount) && Number.isFinite(st.generationCount),
    `家族卡的规模数字（${st.personCount} 人 / ${st.generationCount} 世）`,
    `stats=${JSON.stringify(st)}`
  );
  ok(
    Number.isFinite(st.completeness) && st.completeness >= 0 && st.completeness <= 1,
    `完整度是 0~1 的小数（${st.completeness}）——端上乘 100 当百分比`,
    `stats.completeness=${st.completeness}；若后端直接给 62，进度条会溢出`
  );

  const fl = await api('/v1/family/list', { token });
  const fams = (fl.data && fl.data.families) || [];
  ok(fl.code === 0 && Array.isArray(fams), `「我加入的族」拉取成功（${fams.length} 个）`);
  if (fams.length) {
    const f0 = fams[0];
    ok(
      f0.familyId !== undefined && !!f0.name && !!f0.roleText && f0.current !== undefined,
      `书架条目字段齐（${f0.name} / ${f0.roleText} / current=${f0.current}）`,
      `实际字段：${Object.keys(f0).join(',')}`
    );
    ok(
      fams.filter((x) => x.current).length === 1,
      '有且只有一个族被标成「当前」（否则书架不知道高亮谁）'
    );
  }

  const inv = await api('/v1/invite/mine', { token });
  const ivs = (inv.data && inv.data.items) || [];
  ok(inv.code === 0 && Array.isArray(ivs), `「我的邀请码」拉取成功（${ivs.length} 条）`);
  if (ivs.length) {
    const iv = ivs[0];
    ok(
      !!iv.code && iv.scopeNodeCount !== undefined
        && iv.usedCount !== undefined && iv.isExpired !== undefined,
      `邀请码条目字段齐（${iv.code} / 可编辑 ${iv.scopeNodeCount} 人 / 已用 ${iv.usedCount}）`,
      `实际字段：${Object.keys(iv).join(',')}`
    );
  }

  if (anchor) {
    const rel = await api(`/v1/person/${anchor}/relatives`, { token });
    ok(rel.code === 0, '直系亲属拉取成功（操作环靠它把已填的槽置灰）',
       `code=${rel.code} msg=${rel.msg}`);
    const rd = rel.data || {};
    const relKeys = ['father', 'mother', 'spouses', 'children'];
    ok(
      relKeys.every((k) => rd[k] !== undefined),
      `亲属区块含 ${relKeys.join('/')}`,
      `实际字段：${Object.keys(rd).join(',')}`
    );
    ok(
      Array.isArray(rd.spouses) && Array.isArray(rd.children),
      'spouses / children 是数组（端上直接遍历，不用再拆一层）'
    );

    // 操作环里"补父亲"只传 childId（锚点是那个孩子，不是父亲）。
    // 这里传空 fields：后端该抱怨"缺姓名"；如果说"需要指定挂载位置"，
    // 就说明 create_person 没把 childId 当锚点，环上那两个按钮点了必失败。
    const mk = await api('/v1/person', {
      method: 'POST',
      token,
      body: { fields: {}, relation: { type: 'father_of', childId: anchor } },
    });
    ok(
      /姓名/.test(mk.msg || ''),
      `新增人物时 relation.childId 能当锚点（后端回：${mk.msg}）`,
      '若回"需要指定挂载位置"，说明 create_person 的锚点只认 parentId/partnerId'
    );
  }

  // ---------- 多配偶 / 离异：端上靠这几个字段排"元配/继配/已离异" ----------
  // 演示族只有单配偶，但**字段形状必须一致**：端上按 unionSeq 排先后、
  // 按 ended 把婚姻边画成虚线。少一个字段不会报错，只会静默退化成
  // "所有夫人都一样"，而这类退化没人会主动报 bug。
  if (anchor && me && me.familyId) {
    const fid = me.familyId;

    const ov = await api(`/v1/tree/${fid}?view=overview`, { token });
    const ovNodes = (ov.data && ov.data.nodes) || [];
    const ovRoles = [...new Set(ovNodes.map((n) => n.role))].sort();
    ok(
      ovNodes.length > 0 && ovRoles.every((r) => r === 1 || r === 2),
      '全族总览只含直系丁口（媳妇不进骨架，宽度不随夫人数膨胀）',
      `roles=${ovRoles.join(',')} nodes=${ovNodes.length}`
    );

    const mine = await api(`/v1/tree/${fid}?view=mine&anchor=${anchor}`, { token });
    const mn = (mine.data && mine.data.nodes) || [];
    const me_edges = (mine.data && mine.data.edges) || [];
    const spNodes = mn.filter((n) => n.isSpouse);
    ok(
      spNodes.length > 0 && spNodes.every((n) => typeof n.unionSeq === 'number'),
      '配偶节点带 unionSeq（端上据此排元配 / 继配 / 侧室）',
      `配偶节点 ${spNodes.length} 个，样例 ${JSON.stringify(spNodes[0] || {})}`
    );
    ok(
      me_edges.filter((e) => e.type === 2).every((e) => 'ended' in e),
      '婚姻边带 ended（离异/丧偶画虚线，人仍然留在图上）',
      '缺 ended 时，离异与正常婚姻在端上长得一模一样'
    );

    // ---------- 夫妻必须零距离紧贴（2026-10-22） ----------
    // 曾经的 bug：配偶卡被放在"她那一列"的中心，而那一列被她的孩子撑宽，
    // 于是妻子被推开 220px——"我的一家"里夫妻看着像两家人。
    // 布局层现在把卡片行（row_w）和子女列（col_w）分开算，
    // 这里用**服务端真实下发的坐标**钉住，不看源码。
    // 卡片行是"一张接一张"排的，所以跟前一位比右边界；每张都跟本人比
    // 会把第二位夫人的间距算成 88，误报成"没贴住"。
    const chains = {};
    mn.forEach((n) => {
      if (!n.spouseOf) return;
      (chains[n.spouseOf] = chains[n.spouseOf] || []).push(n);
    });
    const gaps = [];
    Object.keys(chains).forEach((manId) => {
      const man = mn.find((x) => x.id === Number(manId));
      if (!man) return;
      const row = [man].concat(chains[manId].sort((a, b) => a.x - b.x));
      for (let i = 1; i < row.length; i += 1) {
        gaps.push(row[i].x - (row[i - 1].x + row[i - 1].w));
      }
    });
    ok(
      gaps.length > 0 && gaps.every((g) => g === 0),
      '夫妻卡片零间距紧贴（gap 全为 0）',
      `实测间距 ${JSON.stringify(gaps)}；非 0 说明卡片又被子女列宽度牵着走`
    );
    ok(
      Number(mine.data && mine.data.canvas && mine.data.canvas.spouseGap) === 0,
      'canvas.spouseGap = 0（端上不再按"礼貌距离"排版）',
      '布局参数与断言必须同一个来源，否则改了一处另一处还在按 12px 算'
    );
  }

  // ---------- 端上：离异的虚线 + 配偶内嵌 ----------
  {
    // 2026-10-28 垂直卡片流改版后，mine 视图不再渲染 tree-node（组件保留给
    // 可能的复用），配偶改内嵌在人物卡里。契约跟着换成流的写法。
    const homeWxss = fs.readFileSync(
      path.join(MP, 'pages/home/home.wxss'), 'utf8'
    );
    const homeWxmlSrc = fs.readFileSync(
      path.join(MP, 'pages/home/home.wxml'), 'utf8'
    );
    ok(
      /spouseOf\s*===\s*n\.id|s\.isSpouse && s\.spouseOf === n\.id/.test(homeSrc),
      '配偶内嵌在人物卡里（按 spouseOf 归并，不再独立成卡）',
      '配偶散在外面就是回到"两张卡挨着"的旧世界，夫妻紧贴的结论就丢了'
    );
    ok(
      /endReason\s*===\s*1/.test(homeSrc) && /endReason\s*===\s*3/.test(homeSrc),
      '离异（1）/ 婚姻无效（3）才加虚线标注，丧偶（2）不算',
      '把丧偶也画成虚线，等于说"这位先人跟我们家没关系了"'
    );
    ok(
      /\.fspouse-divorced[\s\S]{0,160}dashed/.test(homeWxss),
      '离异配偶行走虚线样式（不是只改文案）',
      '用户看的是卡片本身；只标注不虚线，扫一眼分不出婚姻现状'
    );
    ok(
      /truncated && truncated\.up/.test(homeWxmlSrc) &&
        /truncated && truncated\.down/.test(homeWxmlSrc),
      '流的首尾都有「还有世代」入口（不假装这就是全部）',
      '截断不可见 = 用户以为他家就这几个人，比截断本身更糟'
    );
    // 2026-10-29 实报：认领陈启明后，第五世唯一一张卡是林子轩（陈雨欣之子，
    // lineage_role 5 外孙）。他父亲是女婿、女婿内嵌配偶位不是卡，于是
    // "父亲不是卡则自己当卡"对外孙也成立——外姓后代顶进了我这一支。
    // 规则：role 1/2 血脉成卡；外姓只有"父母在主线上"（招赘随母入谱）才成卡。
    ok(
      /BLOOD\[n\.role\]/.test(homeSrc) &&
        /mainline\[n\.id\]/.test(homeSrc) &&
        /parentsOf\[n\.id\][\s\S]{0,80}mainline\[p\]/.test(homeSrc),
      '★ 成卡先过血脉关（role 1/2）；外姓只有主线子女（招赘随母）例外',
      '只按「父亲不是卡」判，招赘所出的外孙会顶成一代主卡'
    );
    // 亲子边收齐 type 1/3/4（过继、外姓随母也是亲子，母子边不是 type 1）——
    // 漏 type 4 = 招赘家庭的母子链在卡片流里断掉。
    ok(
      /e\.type === 3 \|\| e\.type === 4/.test(homeSrc) &&
        /e\.type === 1 \|\| e\.type === 3 \|\| e\.type === 4/.test(homeSrc),
      '亲子边收齐 type 1/3/4（过继、外姓随母也是亲子，母子边不是 type 1）',
      '漏 type 4 = 招赘家庭的母子链在卡片流里断掉'
    );
    // 2026-10-29 实报：卡片流里妻子那一行点不开（配偶不是"卡"，
    // onCardTap 把 openId 设成配偶 id，没有任何卡匹配得上，界面毫无反应）。
    // 配偶行必须直接开详情，且卡头要有独立的「详」入口。
    ok(
      /class="fspouse[\s\S]{0,240}catchtap="onSpouseTap"/.test(homeWxmlSrc) &&
        /onSpouseTap\s*\(\s*e\s*\)[\s\S]{0,200}openPerson\(/.test(homeSrc),
      '★ 配偶行点击直接开详情（配偶不是卡，不能靠展开态）',
      '配偶挂 onCardTap = 点了没反应，妻子永远看不了详情'
    );
    ok(
      /catchtap="onCardDetail"/.test(homeWxmlSrc) &&
        /onCardDetail\s*\(\s*e\s*\)[\s\S]{0,160}openPerson\(/.test(homeSrc),
      '卡头有独立「详」入口（详情不再藏在操作环里）',
      '详情只能从操作环两步进，等于没有入口'
    );
  }

  // ---------- 全族大宏图：Canvas 谱牌树（2026-10-29 定稿，1:1 不缩放） ----------
  {
    const homeWxmlOv = fs.readFileSync(path.join(MP, 'pages/home/home.wxml'), 'utf8');
    const homeWxssOv = fs.readFileSync(path.join(MP, 'pages/home/home.wxss'), 'utf8');
    const homeJsonOv = JSON.parse(fs.readFileSync(path.join(MP, 'pages/home/home.json'), 'utf8'));
    const personJs = fs.readFileSync(path.join(MP, 'pages/person/person.js'), 'utf8');
    ok(
      /\.bb-on\s*\{[\s\S]{0,200}?background:/.test(homeWxssOv) &&
        /bb \{\{view === 'mine' \? 'bb-on' : ''\}\}/.test(homeWxmlOv) &&
        /bb \{\{view === 'overview' \? 'bb-on' : ''\}\}/.test(homeWxmlOv),
      '★ 两个视图按钮只有「选中 / 未选中」两态（.bb-on 有实底样式，两边对称）',
      '类名写了 .bb-on 却不给样式 = 切换后颜色不变，用户以为没点着（2026-10-29 实报）'
    );
    const canvasJs = fs.readFileSync(
      path.join(MP, 'components/tree-canvas/tree-canvas.js'),
      'utf8'
    );
    ok(
      /<tree-canvas/.test(homeWxmlOv) && !!(homeJsonOv.usingComponents || {})['tree-canvas'],
      '全族大宏图是 Canvas 树状图（500+ 节点用 view 拼不出来，只能画）',
      '只挂组件不注册 usingComponents = 页面白屏'
    );
    ok(
      /fillText\(name/.test(canvasJs) &&
        /displayName\s*\|\|\s*n\.name/.test(canvasJs) &&
        /NODE_W\s*=\s*64/.test(canvasJs),
      '★ 节点是带姓名的谱牌（64×72，一屏横向 4 人），不是色块',
      '只有色块没有名字 = 用户认不出任何人（2026-10-29 实报）'
    );
    ok(
      /RAIL_W/.test(canvasJs) && /_drawRail/.test(canvasJs) && /stats/.test(canvasJs),
      '★ 左侧「世代丁口」固定栏（横向滑动不跑，纵向对行）'
    );
    ok(
      !/_pinch/.test(canvasJs) &&
        !/this\._view\.scale/.test(canvasJs) &&
        /this\._view\.x\s*=\s*this\._drag\.ox/.test(canvasJs),
      '★ 不缩放，只平移（上下左右滑；564 人整屏适配字号 0.47px 读不出字）',
      '恢复双指缩放 = 又回到"缩到最小一片糊点"'
    );
    ok(
      /_drawCard/.test(canvasJs) && /_hitTest/.test(canvasJs) &&
        /pt\.x\s*<\s*RAIL_W/.test(canvasJs),
      '点人命中时排除左栏（左栏是统计，不是人）'
    );
    ok(
      /view=overview&focus=/.test(personJs),
      '★ 详情页「看他在树上的位置」跳全族大宏图并带 focus',
      '只 reLaunch 回首页 = 我这一支里根本没有他，看起来就是没反应'
    );
    ok(
      /cx:\s*Math\.round\(best\.x/.test(canvasJs) &&
        /from:\s*'overview'/.test(homeSrc) &&
        /from === 'overview'[\s\S]{0,200}pages\/person\/person\?id=/.test(homeSrc),
      '★ 大宏图点人 = 浮出操作环（加家人/详），「详」直接进个人页',
      '点人直接开半屏详情会被铺满全屏的树压住，看起来像"点了没反应"（2026-11-02 实报）'
    );
    ok(
      /_query\.focus|q\.focus/.test(homeSrc) && /_startView/.test(homeSrc),
      '首页认 view / focus 入参（起始视图 + 定位高亮）'
    );
    ok(
      /focusId/.test(homeWxmlOv) && /_focusPerson/.test(canvasJs),
      'focus 落到组件：把那个人居中并烫金描边'
    );
    ok(
      /data-role="\{\{item\.lineageRole\}\}"/.test(homeWxmlOv) &&
        /focusPerson\(id,\s*role\)/.test(homeSrc) &&
        /role !== 1 && role !== 2[\s\S]{0,120}pages\/person\/person\?id=/.test(homeSrc) &&
        /this\._focusId = id;/.test(homeSrc),
      '★ 搜索命中 = 直达他在大宏图的位置（居中+烫金），不再先弹资料卡',
      '搜人要的是"他在族里哪儿"；媳妇/女婿不在树上，落个人页别让他对着始祖发呆（2026-11-02 实报）'
    );
  }

  // ---------- 谱文：已在 tabBar，首页不再挂入口 ----------
  console.log('\n【9】谱文（编修历史 / 迁徙源流）');

  const appJson = JSON.parse(fs.readFileSync(path.join(MP, 'app.json'), 'utf8'));
  ok(
    appJson.pages.includes('pages/articles/articles') &&
      appJson.pages.includes('pages/article/article'),
    'articles / article 两个页面已在 app.json 注册（漏一个 = 白屏）'
  );
  ok(
    (appJson.tabBar.list || []).some(
      (t) => t.pagePath === 'pages/articles/articles' && t.text === '谱文'
    ),
    '★ 谱文是 tabBar 的一格（2026-11-02 从首页家族卡搬过来）'
  );
  ok(
    !/fam-wen/.test(fs.readFileSync(path.join(MP, 'pages/home/home.wxml'), 'utf8')) &&
      !/loadArticles/.test(homeSrc),
    '首页不再挂谱文入口（同一个入口不该同时存在两处）'
  );

  // tab 页可以被直接点开（冷启动），此时 globalData 是空的。
  // 不先补身份就查族，会把"还没登录"说成"还没加入家族"。
  const articlesSrc = fs.readFileSync(path.join(MP, 'pages/articles/articles.js'), 'utf8');
  ok(
    /async load\(\)[\s\S]{0,600}?ensureLogin\(\)/.test(articlesSrc),
    '★ 谱文页 load 里先 ensureLogin 再读族（tab 直进时 globalData 还是空的）',
    '不补身份 = 冷启动点「谱文」显示"还没有加入任何家族"'
  );
  ok(
    /onShow\(\)[\s\S]{0,400}?this\.load\(\)/.test(articlesSrc),
    '谱文页 onShow 重拉（写完 / 删完退回来必须看见变化）'
  );
  const mineSrc = fs.readFileSync(path.join(MP, 'pages/mine/mine.js'), 'utf8');
  ok(
    /2:\s*'理事会成员'/.test(homeSrc) && /2:\s*'理事会成员'/.test(mineSrc),
    '首页与「我的」的角色文案都是「理事会成员」（与后端一致）',
    '三处各写一份就会出现"这里叫房长、那里叫理事会成员"。'
  );
  const artsSrc = fs.readFileSync(path.join(MP, 'pages/articles/articles.js'), 'utf8');
  ok(
    /onShow\(\)[\s\S]{0,220}this\.load\(\)/.test(artsSrc),
    '谱文列表 onShow 重拉（从详情页回来能看见新写的那篇）',
    '只有 onLoad 的话，返回列表时页面栈里还是旧数据。'
  );

  // ---- 分享：链接要"点开就进族"，并且带上分享人当锚点 ----
  //
  // 编辑码（codeType=2）已下线：链接**不携带权限**，写权限只来自认领。
  // 分享留下的唯一作用是视角——对方未认领时先看到分享人这一家。
  const landingSrc = fs.readFileSync(path.join(MP, 'pages/landing/landing.js'), 'utf8');
  const shareFn = (mineSrc.match(/onShareAppMessage\(\)\s*\{[\s\S]*?\n  \},/) || [''])[0];
  ok(
    /code=/.test(shareFn) && /\$\{anchor\}/.test(shareFn),
    '分享路径同时带族码和 anchor（点开直接进族，第一眼看到分享人）',
    '不带 code 对方就得手输家族码；不带 anchor 他就得从始祖那一屏开始找自己。'
  );
  ok(
    !/codeType:\s*2/.test(mineSrc) && !/onGenerate/.test(mineSrc),
    '「我的」页不再签发个人编辑码（权限只能来自认领）',
    '编辑码让链接携带权限，等于在身份系统外开了一条查不到人的后门；代录走理事会角色。'
  );
  ok(
    /shareAnchor/.test(appSrc) && /shareAnchor/.test(landingSrc) && /shareAnchor/.test(homeSrc),
    '锚点从链接一路传到「我的一家」（App → 落地页 → 首页）',
    '任何一环断掉，被分享人都会退回始祖那一屏，第一眼看不到认识的人。'
  );

  // 契约：读形状 + 权限行为
  const arts = await api(`/v1/family/${FAMILY_ID}/articles`, { token });
  ok(
    arts.code === 0 && Array.isArray((arts.data || {}).items),
    '谱文列表可拉取（items 数组）',
    `code=${arts.code} msg=${arts.msg}`
  );
  if ((arts.data.items || []).length) {
    const a0 = arts.data.items[0];
    ok(
      a0.category !== undefined && !!a0.categoryLabel && !!a0.title && a0.excerpt !== undefined,
      `谱文条目字段齐（${a0.categoryLabel} / ${a0.title}）`,
      `实际字段：${Object.keys(a0).join(',')}`
    );
  }
  // 陈启明是普通族人：写必须被拒。门禁在后端，但端上按钮的显隐也靠 role>=2，
  // 这条断言同时钉住"端上拿 role 判显隐"不是空谈。
  const deny = await api(`/v1/family/${FAMILY_ID}/articles`, {
    method: 'POST',
    token,
    body: { category: 6, title: '预检越权', content: 'x' },
  });
  ok(
    deny.code === 40300,
    '普通族人写谱文被拒 40300（房长门禁生效，端上按 role 显隐按钮）',
    `code=${deny.code} msg=${deny.msg}`
  );

  // ---------- 理事会工作台 / 认领审核（2026-10-29 新增两页） ----------
  console.log('\n【10】理事会工作台与认领审核');
  const readMp = (rel) => fs.readFileSync(path.join(MP, rel), 'utf8');
  const adminJs = readMp('pages/admin/admin.js');
  const adminWxml = readMp('pages/admin/admin.wxml');
  const claimsJs = readMp('pages/admin/claims/claims.js');
  const claimsWxml = readMp('pages/admin/claims/claims.wxml');
  const mineJs = readMp('pages/mine/mine.js');
  const mineWxml = readMp('pages/mine/mine.wxml');
  const claimJs = readMp('pages/claim/claim.js');

  ok(
    appJson.pages.includes('pages/admin/admin') &&
      appJson.pages.includes('pages/admin/claims/claims'),
    '两个管理页已注册进 app.json（漏注册的表现是点了没反应）',
    (appJson.pages || []).join(',')
  );
  ok(
    /\/admin\/dashboard/.test(adminJs) && /\/admin\/identify\/locks/.test(adminJs),
    '工作台拉看板 + 限速名单'
  );
  ok(/\/admin\/identify\/unlock/.test(adminJs), '工作台能一键解禁');
  ok(
    /retryAfterText/.test(adminWxml) && !/retryAfterSeconds/.test(adminWxml),
    '解禁面板显示「还要等多久」的人话，不把裸秒数甩给理事会'
  );
  ok(/\/admin\/claims/.test(claimsJs), '认领审核页拉队列');
  ok(
    /\/admin\/claim\/\$\{id\}\/review/.test(claimsJs) && /\/revert/.test(claimsJs),
    '认领审核页能通过 / 驳回 / 撤销'
  );
  ok(
    /placeholderText/.test(claimsJs) && /会通知到申请人/.test(claimsJs),
    '驳回必须写理由，且提示说明这句话会通知到申请人'
  );
  ok(
    /role < 2/.test(adminJs) && /role \|\| 1\) < 2/.test(claimsJs),
    '两个管理页在端上就拦住非理事会成员（后端仍会再拦一次）'
  );
  ok(
    /isReviewer/.test(mineJs) && /isReviewer/.test(mineWxml),
    '「我的」只在 role>=2 时显示理事会入口'
  );
  ok(
    /retryAfterSeconds/.test(claimJs) && /waitText/.test(claimJs),
    '答题页用 retryAfterSeconds 说明「还要等约 N 分钟」'
  );

  // 门禁在后端：前端隐藏按钮不是安全边界。陈启明是 role=1，必须被拒。
  const denyLocks = await api(`/v1/admin/identify/locks?familyId=${FAMILY_ID}`, { token });
  ok(
    denyLocks.code === 40300,
    '普通族人调限速名单被拒 40300（门禁在后端，不是靠隐藏按钮）',
    `code=${denyLocks.code} msg=${denyLocks.msg}`
  );



  // ---------- tabBar 结构（2026-11-02）：族谱 / 动态 / 谱文 / 我的 ----------
  console.log('\n【11】底部导航与待办归位');

  {
    const tabs = (appJson.tabBar.list || []).map((t) => `${t.pagePath}|${t.text}`);
    ok(
      tabs.length === 4 &&
        tabs[0] === 'pages/home/home|族谱' &&
        tabs[1] === 'pages/feed/feed|动态' &&
        tabs[2] === 'pages/articles/articles|谱文' &&
        tabs[3] === 'pages/mine/mine|我的',
      '★ 底 bar 四项：族谱 / 动态 / 谱文 / 我的',
      tabs.join(' , ')
    );
    ok(
      !tabs.some((t) => t.startsWith('pages/todo/todo')),
      '★ 待办不再是 tab（撤进「我的」，一级入口留给"看谱"这类同量级动作）'
    );
    ok(
      appJson.pages.includes('pages/feed/feed') &&
        fs.existsSync(path.join(MP, 'pages/feed/feed.wxml')),
      '动态页已注册且文件存在（tab 指向不存在的页 = 点开白屏）'
    );
    const feedWxml = fs.readFileSync(path.join(MP, 'pages/feed/feed.wxml'), 'utf8');
    const feedJs = fs.readFileSync(path.join(MP, 'pages/feed/feed.js'), 'utf8');
    ok(
      /占位|先占个位置/.test(feedWxml) && !/utils\/request/.test(feedJs),
      '动态页是占位：不调接口、不假装有数据（口径未定先不接）'
    );

    const mineWxmlSrc = fs.readFileSync(path.join(MP, 'pages/mine/mine.wxml'), 'utf8');
    const mineJsSrc = fs.readFileSync(path.join(MP, 'pages/mine/mine.js'), 'utf8');
    ok(
      /class="mtodo"/.test(mineWxmlSrc) && /\/person\/\$\{pid\}\/missing/.test(mineJsSrc),
      '★ 待办并在「我的」：完整度 + 待补项由 /person/{id}/missing 算（端上不自己拼）'
    );
    ok(
      /missingCount:\s*-1/.test(mineJsSrc),
      '拿不到缺口时置 -1（不是 0）——0 的意思是"确实补完了"，不能凭空给'
    );
    ok(
      /onTodoAll[\s\S]{0,120}pages\/todo\/todo/.test(mineJsSrc),
      '「逐条补完」仍能进原待办页（不再占 tab，但功能没丢）'
    );
    ok(
      JSON.parse(fs.readFileSync(path.join(MP, 'pages/home/home.json'), 'utf8'))
        .navigationBarTitleText === '族谱',
      '首页标题改成「族谱」（tab 名与页面标题一致，不再叫"我的一家"）'
    );
  }

  console.log('\n' + '='.repeat(68));
  if (failures.length === 0) {
    console.log(`结果：${passed} 项全部通过 —— 前端逻辑层与后端契约一致`);
    console.log('下一步：在开发者工具里只剩样式和交互，不会再遇到空白/NaN。');
  } else {
    console.log(`结果：${passed} 通过 / ${failures.length} 失败`);
    for (const f of failures) console.log(`  - ${f}`);
  }
  console.log('='.repeat(68));
  return failures.length ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error('\n预检脚本自身出错：');
    console.error(err);
    process.exit(3);
  }
);
