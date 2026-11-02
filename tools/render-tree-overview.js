// 把后端下发的「全族总览」渲染成一张可以双击打开的世系图，用来肉眼核对。
//
// 为什么要有这个：`preflight-frontend.js` 断言的是「字段在不在、有没有 NaN」，
// 但它看不见「这张谱长得对不对」——父子线接没接上、夫妻是不是挨着、
// 世次有没有错行、哪一枝断了。这些只有画出来一眼能看出来。
//
// 关键点：**连线和节点用的是小程序端的真代码**（utils/tree-lines.js、
// utils/format.js），不是另写一套。所以这张图里出现的问题，
// 就是端上会看到的问题；这张图里没问题，端上基本就没问题。
//
// 视角（--as）：
//   guest（默认）→ 未登录访客，在世者脱敏（端上游客真正看到的样子）
//   admin        → 族管理员，全部真名
//   其他账号名   → 认领过的普通族人。**从 2026-10-16 起这一档也是全真名**，
//                  因为姓名脱敏的分界线从"是不是近亲"改成了"有没有认领"。
//                  例：--as=mock_new_member（seed 里已认领陈雨欣的那个账号）
//                  注：未认领的账号仍然脱敏，脚本会提示你。
//
// 用法：
//   node tools/render-tree-overview.js                            # 云端 · 游客视角
//   node tools/render-tree-overview.js --as=admin                 # 云端 · 全名
//   node tools/render-tree-overview.js --as=mock_new_member       # 云端 · 认领族人视角
//   node tools/render-tree-overview.js http://127.0.0.1:8000      # 打本机
//   node tools/render-tree-overview.js <base> <familyId>
//
// 输出：_verify/tree-overview.html（游客）
//       _verify/tree-overview-full.html（admin）
//       _verify/tree-overview-<账号>.html（其他身份）

const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const dns = require('dns');
const dnsp = require('dns').promises;

const ROOT = path.resolve(__dirname, '..');
const { buildLines, EDGE } = require(path.join(ROOT, 'miniprogram/utils/tree-lines.js'));
const { cnNumber, nodeLabel } = require(path.join(ROOT, 'miniprogram/utils/format.js'));

/** 去掉 --as=xxx 这类选项后的位置参数。 */
const ARGV = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const FLAGS = Object.fromEntries(
  process.argv.slice(2).filter((a) => a.startsWith('--'))
    .map((a) => {
      const [k, v] = a.replace(/^--/, '').split('=');
      return [k, v === undefined ? true : v];
    }),
);

const BASE = (ARGV[0] || 'https://api.wangqiming.top').replace(/\/+$/, '');
const FAMILY_ID = ARGV[1] || '1';
// --as=guest（默认）/ admin / 任意演示账号的 openid 或 dev: 后面的名字。
// 2026-10-16 起姓名脱敏的分界线是「有没有认领」，所以拿全名不再只有管理员
// 一条路——`--as=mock_new_member` 就是一个认领过的普通族人。
const VIEW_AS = String(FLAGS.as || 'guest');
const LOGIN_CODE = VIEW_AS === 'admin'
  ? 'dev:mock_family_admin'          // seed 里的族管理员（国栋）
  : `dev:${VIEW_AS.replace(/^dev:/, '')}`;
/** 非 guest 都是"看得到全名"的视角。 */
const SHOW_NAMES = VIEW_AS !== 'guest';

// ---------------------------------------------------------------- 拉数据

/** 系统 DNS 解析。失败返回 null（新加记录时本机常还缓存着 NXDOMAIN）。 */
async function systemLookup(host) {
  if (/^\d+(\.\d+){3}$/.test(host)) return host;
  try {
    const r = await dnsp.lookup(host);
    return r.address;
  } catch (e) {
    return null;
  }
}

/** 绕过本机递归，直接问公共 DNS。 */
function lookupViaPublicDns(host) {
  const servers = ['119.29.29.29', '223.5.5.5', '180.76.76.76'];
  return new Promise((resolve) => {
    let i = 0;
    const next = () => {
      if (i >= servers.length) return resolve(null);
      const server = servers[i++];
      const r = new dns.Resolver({ timeout: 4000, tries: 2 });
      r.setServers([server]);
      r.resolve4(host, (err, addrs) => {
        if (err || !addrs || !addrs.length) return next();
        resolve({ ip: addrs[0], via: server });
      });
    };
    next();
  });
}

function rawRequest(url, opts, body) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https:') ? https : http;
    const req = lib.request(url, opts, (res) => {
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { buf += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: buf }));
    });
    req.on('timeout', () => { req.destroy(new Error('请求超时')); });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

/**
 * 组装请求选项，必要时补上「绕过本机 DNS」的兜底。
 *
 * 本机解析不出域名时（DNS 负缓存，新加 A 记录的常见现象），只把**底层连接用的
 * IP** 换成公共 DNS 查到的那个 —— URL 里的域名不变，所以 SNI 与证书校验用的
 * 仍是真域名，结论和正常解析完全一致。
 */
async function prepareOpts(url, extra) {
  const u = new URL(url);
  const opts = Object.assign({ timeout: 20000 }, extra || {});
  opts.headers = Object.assign({}, opts.headers);

  if (!(await systemLookup(u.hostname))) {
    const alt = await lookupViaPublicDns(u.hostname);
    if (!alt) {
      throw new Error(`解析不出 ${u.hostname}：本机 DNS 和公共 DNS 都失败，` +
        '先确认 A 记录配好了');
    }
    console.log(`  注：本机解析不出 ${u.hostname}（DNS 负缓存还没过期），` +
      `改问 ${alt.via} → ${alt.ip}。域名照旧参与证书校验。`);
    opts.lookup = (h, o, cb) => cb(null, alt.ip, 4);
    opts.servername = u.hostname;
    opts.headers.Host = u.hostname;
  }
  return opts;
}

async function requestJson(url, { method = 'GET', headers = {}, body = null } = {}) {
  const payload = body ? JSON.stringify(body) : null;
  const opts = await prepareOpts(url, {
    method,
    headers: Object.assign({ Accept: 'application/json' }, headers),
  });
  if (payload) {
    opts.headers['Content-Type'] = 'application/json';
    opts.headers['Content-Length'] = Buffer.byteLength(payload);
  }

  const res = await rawRequest(url, opts, payload);
  let parsed;
  try {
    parsed = JSON.parse(res.body);
  } catch (e) {
    throw new Error(`返回的不是 JSON（HTTP ${res.status}）：${res.body.slice(0, 300)}`);
  }
  if (parsed.code !== 0) {
    throw new Error(`接口失败（HTTP ${res.status}）：code=${parsed.code} msg=${parsed.msg}`);
  }
  return parsed;
}

/**
 * 以指定身份登录，换一个 token。
 *
 * 只在 `WX_MOCK_LOGIN=true` 时可用（开发/演示期）；真实环境里拿不到这些身份，
 * 那时核对谱系得用管理员或已认领的账号在小程序里看。
 */
async function loginAs(code) {
  const resp = await requestJson(`${BASE}/v1/auth/login`, {
    method: 'POST',
    body: { code },
  });
  const d = resp.data || {};
  if (!d.token) throw new Error('登录成功但没拿到 token');
  return { token: d.token, user: d.user || {} };
}

// ---------------------------------------------------------------- 核对

/** 生成一份「这张谱能不能看」的体检报告。 */
function check(data, viewAs) {
  const nodes = data.nodes;
  const edges = data.edges;
  const byId = {};
  for (const n of nodes) byId[n.id] = n;

  const issues = [];
  const notes = [];

  // 悬空边：两端必须都在节点表里，否则端上画线时会静默跳过
  const dangling = edges.filter((e) => !byId[e.from] || !byId[e.to]);
  if (dangling.length) {
    issues.push(`悬空边 ${dangling.length} 条（端点不在本次返回的节点里）：` +
      dangling.map((e) => `${e.from}→${e.to}`).join('、'));
  }

  // 孤立节点：一个点都没有任何边，等于凭空挂了一个人
  const linked = new Set();
  for (const e of edges) { linked.add(e.from); linked.add(e.to); }
  const lonely = nodes.filter((n) => !linked.has(n.id));
  if (lonely.length) {
    issues.push(`孤立节点 ${lonely.length} 个（没有任何亲属关系）：` +
      lonely.map((n) => `${n.id} ${nodeLabel(n)}`).join('、'));
  }

  // 坐标/尺寸必须是有限数字，否则端上是空白或 NaN
  const badGeom = nodes.filter((n) =>
    ![n.x, n.y, n.w, n.h].every((v) => typeof v === 'number' && Number.isFinite(v)));
  if (badGeom.length) {
    issues.push(`坐标/尺寸异常 ${badGeom.length} 个（会是 NaN 或 undefined）`);
  }

  // 亲子边方向：子女的 y 必须比父母大，否则层次画反了
  const reversed = edges.filter((e) => {
    if (e.type === EDGE.MARRY) return false;
    const a = byId[e.from]; const b = byId[e.to];
    return a && b && !(b.y > a.y);
  });
  if (reversed.length) {
    issues.push(`亲子边上下颠倒 ${reversed.length} 条（子不在父下方，端上会拒画）：` +
      reversed.map((e) => `${e.from}→${e.to}`).join('、'));
  }

  // 世次与 y 是否一致：同一 gen 必须落在同一条横线上
  const genY = {};
  let genMismatch = 0;
  for (const n of nodes) {
    if (genY[n.gen] === undefined) genY[n.gen] = n.y;
    else if (genY[n.gen] !== n.y) genMismatch++;
  }
  if (genMismatch) {
    issues.push(`同一世次 y 不一致的节点 ${genMismatch} 个（谱会错行）`);
  }

  // 配偶是否挨着：婚姻边的两端如果隔着别人，说明排布把夫妻拆开了
  let farCouples = 0;
  for (const e of edges) {
    if (e.type !== EDGE.MARRY) continue;
    const a = byId[e.from]; const b = byId[e.to];
    if (!a || !b) continue;
    const gap = Math.abs(a.x - b.x) - a.w;
    if (gap > 40) farCouples++;
  }
  if (farCouples) {
    notes.push(`有 ${farCouples} 对夫妻之间隔了其他节点（间距 > 40px）`);
  }

  // 脱敏：口径随视角而变。游客视角在世必须打码；管理员视角必须全是真名。
  const living = nodes.filter((n) => n.living === 1);
  const deceased = nodes.filter((n) => n.living === 2);
  const maskedOk = living.filter((n) => n.displayName.includes('*')).length;
  const fullOk = deceased.filter((n) => !n.displayName.includes('*')).length;
  if (viewAs !== 'guest') {
    // 现在非 guest 都是"已认领及以上的身份"（含 --as=mock_new_member 这种普通族人），
    // 按 2026-10-16 的口径，这一档在世者姓名就该全是真名。
    const stillMasked = living.filter((n) => n.displayName.includes('*'));
    if (stillMasked.length) {
      issues.push(`${viewAs} 视角下仍有 ${stillMasked.length} 个在世者被打码` +
        `（已认领身份应当看到真名）：` +
        stillMasked.map((n) => `${n.id} ${n.displayName}`).join('、'));
    }
  } else {
    if (living.length && maskedOk !== living.length) {
      issues.push(`在世者没打码 ${living.length - maskedOk} 个`);
    }
    if (deceased.length && fullOk !== deceased.length) {
      issues.push(`已故者被打了码 ${deceased.length - fullOk} 个`);
    }
  }

  // 边类型分布
  const edgeCount = {};
  const EDGE_NAME = { 1: '父子', 2: '婚姻', 3: '过继', 4: '外姓' };
  for (const e of edges) edgeCount[e.type] = (edgeCount[e.type] || 0) + 1;

  // 世次分布
  const genCount = {};
  for (const n of nodes) genCount[n.gen] = (genCount[n.gen] || 0) + 1;

  return {
    issues,
    notes,
    edgeCount,
    EDGE_NAME,
    genCount,
    stats: {
      nodes: nodes.length,
      edges: edges.length,
      living: living.length,
      deceased: deceased.length,
      maskedOk,
      fullOk,
      dangling: dangling.length,
      lonely: lonely.length,
    },
  };
}

// ---------------------------------------------------------------- 渲染

/** 与端上 tree-node 组件一致地算出节点该带的 class。 */
function nodeCls(n) {
  const role = n.role || 1;
  const isExternal = role === 4 || role === 5;
  const isDivorced = !!n.isSpouse && (n.endReason === 1 || n.endReason === 3);
  return [
    isExternal ? 'tn-external' : '',
    n.living === 2 ? 'tn-deceased' : '',
    n.isSpouse ? 'tn-spouse' : '',
    n.flatRight ? 'tn-flat-r' : '',
    n.flatLeft ? 'tn-flat-l' : '',
    isDivorced ? 'tn-divorced' : '',
    n.isSelf ? 'tn-self' : '',
    !n.isSelf && n.onPath ? 'tn-path' : '',
  ].filter(Boolean).join(' ');
}

const esc = (s) => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function renderNodes(nodes, viewAs) {
  return nodes.map((n) => {
    const cls = nodeCls(n);
    const genText = n.gen ? `${cnNumber(n.gen)}世` : '';
    const count = n.badge && n.badge.count;
    const pendingText = count ? String(count) : (n.hasPending ? '补' : '');
    // 管理员视角下"我"不是启明本人（是拿来换 token 的族管理员），
    // 仍标"我"会误导，改成"登录"。
    const selfFlag = n.isSelf ? (viewAs !== 'guest' ? '登录' : '我') : '';
    return `<div class="tn ${cls}" data-id="${n.id}" ` +
      `style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px;">` +
      `<div class="tn-card">` +
      `<div class="tn-name">${esc(nodeLabel(n))}</div>` +
      `<div class="tn-foot"><span class="tn-gen">${genText}</span></div>` +
      `</div>` +
      (selfFlag ? `<div class="tn-flag">${selfFlag}</div>` : '') +
      (pendingText ? `<div class="tn-pending">${pendingText}</div>` : '') +
      `</div>`;
  }).join('\n');
}

function renderLines(lines) {
  return lines.map((l) =>
    `<div class="${l.cls}" style="${l.style}"></div>`).join('\n');
}

/** 每一世的横线 + 标签，帮人确认"有没有错行"。
 *  y 必须 minus minGeneration —— overview 从第 1 世开始看不出差别，
 *  mine 视图锚点在第 3 世时每条 band 都会往下漂 2 个 genGap。 */
function renderGenBands(canvas) {
  const out = [];
  for (let g = canvas.minGeneration; g <= canvas.maxGeneration; g++) {
    const y = 40 + (g - canvas.minGeneration) * canvas.genGap;
    out.push(`<div class="gen-band" style="top:${y - 14}px;"></div>`);
    out.push(`<div class="gen-tag" style="top:${y - 14}px;">第${cnNumber(g)}世</div>`);
  }
  return out.join('\n');
}

// ---------------------------------------------------------------- 模板

function buildHtml(data, lines, report, ctx) {
  const viewAs = ctx.viewAs;
  const canvas = data.canvas;
  const nodesHtml = renderNodes(data.nodes, viewAs);
  const linesHtml = renderLines(lines);
  const bandsHtml = renderGenBands(canvas);

  const ROLE_NAME = { 1: '本族男丁', 2: '本族女儿', 3: '媳妇', 4: '女婿', 5: '外孙' };
  const LIVING_NAME = { 1: '在世', 2: '已故', 3: '不详' };

  // 人物清单：按世次排，方便从上到下一一核对
  const rosterRows = data.nodes.slice()
    .sort((a, b) => (a.gen - b.gen) || (a.x - b.x))
    .map((n) => {
      const flags = [
        n.isSelf ? '<span class="tag tag-self">我</span>' : '',
        n.living === 2 ? '<span class="tag tag-dead">故</span>' : '',
        n.role === 4 || n.role === 5 ? '<span class="tag tag-ext">外</span>' : '',
      ].filter(Boolean).join('');
      return `<tr><td>${n.id}</td><td class="nm">${esc(nodeLabel(n))}${flags}</td>` +
        `<td>${n.gen}</td><td>${ROLE_NAME[n.role] || n.role}</td>` +
        `<td>${LIVING_NAME[n.living] || n.living}</td></tr>`;
    }).join('');

  const genRows = Object.keys(report.genCount).sort((a, b) => a - b)
    .map((g) => `<tr><td>第${cnNumber(Number(g))}世</td><td>${report.genCount[g]}</td></tr>`)
    .join('');

  const edgeRows = Object.keys(report.edgeCount).sort()
    .map((t) => `<tr><td>${report.EDGE_NAME[t] || '类型' + t}</td><td>${report.edgeCount[t]}</td></tr>`)
    .join('');

  const issuesHtml = report.issues.length
    ? report.issues.map((s) => `<li class="bad">${esc(s)}</li>`).join('')
    : '<li class="good">没有发现问题</li>';

  const notesHtml = report.notes.length
    ? report.notes.map((s) => `<li class="warn">${esc(s)}</li>`).join('')
    : '<li class="good">没有需要留意的地方</li>';

  const s = report.stats;

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>全族总览 · 世系图核对</title>
<style>
:root{
  --paper:#F6F3EA; --paper-2:#EDE8DB; --card:#FFFFFF;
  --ink:#1F2226; --ink-2:#4E5459; --ink-3:#85817A;
  --line:#E3DDCE; --line-2:#D4CCB8;
  --dai:#2B574B; --dai-2:#447A6C; --dai-3:#8FB0A5; --dai-bg:#E6EEE9;
  --seal:#A6321E; --seal-bg:#F7E9E5; --jade:#3F6B57; --gold:#B8860B; --warn:#B0741C;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header{padding:18px 24px 14px;border-bottom:1px solid var(--line);background:var(--paper)}
h1{margin:0;font-size:19px;font-weight:600}
h2{margin:0 0 10px;font-size:14px;font-weight:600;padding-left:10px;position:relative}
h2::before{content:'';position:absolute;left:0;top:3px;width:3px;height:14px;
  background:var(--dai);border-radius:2px}
.sub{color:var(--ink-3);font-size:13px;margin-top:4px}
/* 头部那条数据来源 URL 有 40+ 字符且无空格，窄屏下不强制断行会把
   整个页面撑宽（aside 一起被顶出屏幕，数值列看着像"消失"） */
.sub code{word-break:break-all}
.viewas{display:inline-block;margin-left:8px;padding:2px 9px;border-radius:20px;
  font-size:11px;font-weight:400;vertical-align:2px}
.va-admin{background:var(--dai-bg);color:var(--dai);border:1px solid #C6D3E6}
.va-guest{background:#EEF3F0;color:var(--jade);border:1px solid #CFE0D8}
table.roster{font-size:12px}
table.roster th{text-align:left;font-weight:600;color:var(--ink-3);
  padding:4px 0;border-bottom:1px solid var(--line-2);font-size:11px}
table.roster td{padding:3px 0;border-bottom:1px solid var(--line)}
table.roster td.nm{font-weight:600;color:var(--ink);text-align:left}
table.roster td:nth-child(3),table.roster td:nth-child(4),
table.roster td:nth-child(5){text-align:left;color:var(--ink-2);white-space:nowrap}
.tag{display:inline-block;margin-left:4px;padding:0 4px;border-radius:4px;
  font-size:10px;line-height:14px;vertical-align:1px}
.tag-self{background:var(--dai);color:#FFF}
.tag-dead{background:#EEF3F0;color:var(--jade)}
.tag-ext{background:var(--paper-2);color:var(--ink-3)}
.bar{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
button{font:13px/1 inherit;padding:7px 14px;border-radius:8px;cursor:pointer;
  border:1px solid var(--line-2);background:var(--card);color:var(--ink)}
button:hover{border-color:var(--seal);color:var(--seal)}
.scale-read{font-size:12px;color:var(--ink-3);margin-left:4px}
.wrap{display:flex;align-items:flex-start;gap:0}
.viewport{flex:1;min-width:0;overflow:auto;max-height:calc(100vh - 118px);
  background:var(--paper-2);
  background-image:radial-gradient(circle,var(--line) 1px,transparent 1px);
  background-size:20px 20px}
.sizer{position:relative}
.canvas{position:relative;transform-origin:0 0}
.gen-band{position:absolute;left:0;width:100%;height:0;
  border-top:1px dashed rgba(138,131,120,.28)}
.gen-tag{position:absolute;left:8px;font-size:11px;color:var(--ink-3);
  background:var(--paper-2);padding:0 6px;line-height:16px;border-radius:4px}

/* ---------- 连线：与 pages/home/home.wxss 一一对应 ---------- */
.ln-v{position:absolute;width:1.5px;background:rgba(43,87,75,.34)}
.ln-h{position:absolute;height:1.5px;background:rgba(43,87,75,.34)}
.ln-adopt{background:none!important}
.ln-adopt.ln-v{width:0;border-left:1.5px dashed rgba(31,34,38,.45)}
.ln-adopt.ln-h{height:0;border-top:1.5px dashed rgba(31,34,38,.45)}
.ln-ext{background:none!important}
.ln-ext.ln-v{width:0;border-left:1.5px dashed #BFC5CE}
.ln-ext.ln-h{height:0;border-top:1.5px dashed #BFC5CE}
.ln-marry{background:var(--gold)!important}
.ln-marry.ln-h{height:2px}
/* 已结束的婚姻（离异/丧偶）与端上一致：金色虚线 */
.ln-marry-ended{background:none!important}
.ln-marry-ended.ln-h{height:0;border-top:2px dashed var(--gold)}
.ln-marry-ended.ln-v{width:0;border-left:2px dashed var(--gold)}

/* ---------- 节点：与 components/tree-node/tree-node.wxss 一一对应 ---------- */
.tn{position:absolute;box-sizing:border-box}
.tn-card{width:100%;height:100%;border-radius:6px;background:#FFFFFF;
  border:1px solid #D4CCB8;display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:3px 4px;overflow:hidden}
.tn-name{font-size:13px;line-height:1.25;font-weight:600;color:#1F2226;
  text-align:center;word-break:break-all;max-height:34px;overflow:hidden}
.tn-foot{margin-top:2px;display:flex;align-items:center;gap:3px;max-width:100%;overflow:hidden}
.tn-gen{font-size:9px;color:#85817A;line-height:1;white-space:nowrap}
.tn-deceased .tn-card{background:#F7F5F0}
.tn-deceased .tn-name{color:#3F6B57}
.tn-external .tn-card{border-style:dashed;background:#FAF8F1;opacity:.94}
.tn-external .tn-name{color:#5C5C5C}
.tn-spouse .tn-card{background:#F8F5ED}
/* 夫妻紧贴：接缝两侧切平圆角，否则两个圆角对在一起会凹出缺口 */
.tn-flat-r .tn-card{border-top-right-radius:0;border-bottom-right-radius:0}
.tn-flat-l .tn-card{border-top-left-radius:0;border-bottom-left-radius:0;
  border-left-color:#EDE6D8}
/* 离异：虚线框（丧偶不算） */
.tn-divorced .tn-card{border-style:dashed;border-color:#BCAEB0;background:#FBF9F9}
.tn-divorced .tn-name{color:#6E6470}
.tn-path .tn-card{border-color:#D9BE72}
.tn-self .tn-card{border:1.5px solid #2B574B;background:#EEF2F8;
  box-shadow:0 1px 5px rgba(166,50,30,.18)}
.tn-self .tn-name{color:#2B574B}
.tn-flag{position:absolute;top:-5px;right:-5px;min-width:15px;height:15px;
  padding:0 3px;border-radius:8px;background:#A6321E;color:#FFF;font-size:9px;
  line-height:15px;text-align:center;box-sizing:border-box}
.tn-pending{position:absolute;bottom:-5px;left:-5px;min-width:15px;height:15px;
  padding:0 3px;border-radius:8px;background:#B0741C;color:#FFF;font-size:9px;
  line-height:15px;text-align:center;box-sizing:border-box}
.tn:hover .tn-card{box-shadow:0 0 0 2px rgba(166,50,30,.35);cursor:pointer}

/* 夫妻样式示意区 */
.cd-row{position:relative;width:176px;height:118px;margin:8px 0 16px}
.cd-note{font-size:12px;color:var(--ink-3);margin:10px 0 0}

/* ---------- 右侧核对面板 ---------- */
aside{width:296px;flex-shrink:0;padding:18px 20px 40px;border-left:1px solid var(--line);
  max-height:calc(100vh - 118px);overflow:auto;background:var(--paper)}
aside section{margin-bottom:22px}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:4px 0;border-bottom:1px solid var(--line)}
td:last-child{text-align:right;color:var(--ink-2);font-variant-numeric:tabular-nums}
ul{margin:0;padding-left:18px;font-size:13px}
li{margin:5px 0}
li.bad{color:#A32D2D}
li.warn{color:var(--warn)}
li.good{color:var(--jade);list-style:none;margin-left:-18px}
.legend{display:flex;flex-direction:column;gap:9px;font-size:13px;color:var(--ink-2)}
.legend div{display:flex;align-items:center;gap:9px}
.sw{width:30px;height:0;flex-shrink:0}
.sw-parent{border-top:1.5px solid var(--line-2)}
.sw-adopt{border-top:1.5px dashed var(--ink-3)}
.sw-marry{border-top:2px solid var(--gold)}
.chip{display:inline-block;padding:1px 7px;border-radius:5px;font-size:12px;
  background:var(--paper-2);color:var(--ink-3)}
#tip{position:fixed;pointer-events:none;display:none;background:#1F2226;color:#FFF;
  font-size:12px;line-height:1.5;padding:8px 11px;border-radius:8px;max-width:260px;z-index:9}
code{font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--ink-2)}

/* ---------- 窄屏（手机 / 小平板）：说明移到图的下方 ----------
   原来的右侧固定 296px 在手机上会把画布挤到几乎为零宽（aside 是 shrink:0），
   所以窄屏改成纵向堆叠：图在上、说明在下。 */
@media (max-width:900px){
  .wrap{flex-direction:column;align-items:stretch}
  .viewport{width:100%;flex:none;max-height:66vh;
    border-bottom:1px solid var(--line)}
  aside{width:100%;max-height:none;border-left:none;overflow:visible;
    padding:16px 14px 44px}
  header{padding:14px 14px 12px}
  h1{font-size:17px;display:flex;flex-direction:column;
    align-items:flex-start;gap:6px}
  .viewas{margin-left:0;font-size:10px}
  .sub{font-size:12px;word-break:break-all}
  .sub code{font-size:11px}
  h2{font-size:13px}
  aside section{margin-bottom:18px}
  button{padding:8px 13px}
  .scale-read{font-size:11px}
  table.roster{font-size:12px}
  #tip{max-width:calc(100vw - 28px)}
}
@media (max-width:400px){
  .viewport{max-height:62vh}
  aside{padding:14px 12px 40px}
}
</style>
</head>
<body>
<header>
  <h1>全族总览 · 世系图核对
    <span class="viewas ${viewAs === 'guest' ? 'va-guest' : 'va-admin'}">
      ${viewAs === 'guest' ? '游客视角 · 在世者脱敏' : `${esc(viewAs)} 视角 · 真名`}
    </span>
  </h1>
  <div class="sub">
    数据来自 <code>${esc(BASE)}/v1/tree/${esc(FAMILY_ID)}?view=overview</code>
    · viewerScope=<code>${esc(data.viewerScope)}</code>
    · rev=${data.rev}${data.cached ? ' · cached' : ''}
    ${ctx.loginUser ? `· 登录身份 <code>${esc(ctx.loginUser.nickname || '')}</code>（role=${ctx.loginUser.role}）` : ''}
    · 连线与节点样式复刻端上代码
  </div>
  <div class="bar">
    <button data-z="fit">适应宽度</button>
    <button data-z="hfit">适应高度</button>
    <button data-z="1">100%</button>
    <button data-z="in">放大</button>
    <button data-z="out">缩小</button>
    <span class="scale-read" id="scaleRead"></span>
  </div>
</header>

<div class="wrap">
  <div class="viewport" id="viewport">
    <div class="sizer" id="sizer">
      <div class="canvas" id="canvas" style="width:${canvas.width}px;height:${canvas.height}px;">
${bandsHtml}
${linesHtml}
${nodesHtml}
      </div>
    </div>
  </div>

  <aside>
    <section>
      <h2>规模</h2>
      <table>
        <tr><td>节点</td><td>${s.nodes}</td></tr>
        <tr><td>连线</td><td>${s.edges}</td></tr>
        <tr><td>画布</td><td>${canvas.width}×${canvas.height}</td></tr>
        <tr><td>世次跨度</td><td>${canvas.minGeneration}—${canvas.maxGeneration} 世</td></tr>
        <tr><td>截断</td><td>${data.truncated.up || data.truncated.down ? '有' : '无'}</td></tr>
      </table>
    </section>

    <section>
      <h2>世次分布</h2>
      <table>${genRows}</table>
    </section>

    <section>
      <h2>关系类型</h2>
      <table>${edgeRows}</table>
    </section>

    <section>
      <h2>脱敏（${viewAs === 'guest' ? '游客视角' : `${esc(viewAs)} 视角`}）</h2>
      <table>
        ${viewAs === 'guest'
          ? `<tr><td>在世打码</td><td>${s.maskedOk}/${s.living}</td></tr>
        <tr><td>已故显全名</td><td>${s.fullOk}/${s.deceased}</td></tr>`
          : `<tr><td>在世显示真名</td><td>${s.living - s.maskedOk}/${s.living}${s.maskedOk ? ' ⚠' : ''}</td></tr>
        <tr><td>已故显示真名</td><td>${s.fullOk}/${s.deceased}</td></tr>`}
      </table>
    </section>

    <section>
      <h2>人物清单（${data.nodes.length} 人）</h2>
      <table class="roster">
        <tr><th>id</th><th>姓名</th><th>世</th><th>身份</th><th>状态</th></tr>
        ${rosterRows}
      </table>
    </section>

    <section>
      <h2>体检</h2>
      <ul>${issuesHtml}</ul>
      <ul style="margin-top:8px">${notesHtml}</ul>
    </section>

    <section>
      <h2>夫妻样式</h2>
      <div class="cd-note">正常：零间距贴住，接缝圆角切平、横梁实线</div>
      <div class="cd-row">
        <div class="ln ln-h ln-marry" style="left:44px;top:7px;width:88px"></div>
        <div class="ln ln-v ln-marry" style="left:43.25px;top:7px;height:7px"></div>
        <div class="ln ln-v ln-marry" style="left:131.25px;top:7px;height:7px"></div>
        <div class="tn tn-flat-r" style="left:0;top:14px;width:88px;height:96px">
          <div class="tn-card"><div class="tn-name">陈文山</div>
            <div class="tn-foot"><span class="tn-gen">三世</span></div></div>
        </div>
        <div class="tn tn-spouse tn-flat-l" style="left:88px;top:14px;width:88px;height:96px">
          <div class="tn-card"><div class="tn-name">李桂芳</div>
            <div class="tn-foot"><span class="tn-gen">三世</span><span class="tn-role">元配</span></div></div>
        </div>
      </div>
      <div class="cd-note">离异：卡片虚线框，横梁也画虚线，人仍留在图上</div>
      <div class="cd-row">
        <div class="ln ln-h ln-marry ln-marry-ended" style="left:44px;top:7px;width:88px"></div>
        <div class="ln ln-v ln-marry ln-marry-ended" style="left:43.25px;top:7px;height:7px"></div>
        <div class="ln ln-v ln-marry ln-marry-ended" style="left:131.25px;top:7px;height:7px"></div>
        <div class="tn tn-flat-r" style="left:0;top:14px;width:88px;height:96px">
          <div class="tn-card"><div class="tn-name">陈文山</div>
            <div class="tn-foot"><span class="tn-gen">三世</span></div></div>
        </div>
        <div class="tn tn-spouse tn-flat-l tn-divorced" style="left:88px;top:14px;width:88px;height:96px">
          <div class="tn-card"><div class="tn-name">张继配</div>
            <div class="tn-foot"><span class="tn-gen">三世</span><span class="tn-role">已离异</span></div></div>
        </div>
      </div>
    </section>

    <section>
      <h2>图例</h2>
      <div class="legend">
        <div><span class="sw sw-parent"></span>父子</div>
        <div><span class="sw sw-marry"></span>婚姻</div>
        <div><span class="sw sw-adopt"></span>过继</div>
        <div><span class="chip">青绿字</span>已故（显示全名）</div>
        <div><span class="chip">名字带 *</span>在世（脱敏）</div>
        <div><span class="chip">左下角标</span>待补全（数字 = 缺几项）</div>
        <div><span class="chip">虚线框</span>外姓（女婿 / 外孙）</div>
      </div>
      ${viewAs === 'guest'
        ? ''
        : '<div style="margin-top:10px;font-size:12px;color:var(--warn)">' +
          '这一档身份（已认领及以上）在世者姓名全是真名；' +
          '但生日精度、职业、生平仍只对近亲开放。</div>'}
    </section>

    <section>
      <h2>用法</h2>
      <div style="font-size:13px;color:var(--ink-2)">
        点节点看现场数据（手机上也支持）。改完布局算法后重跑：<br>
        <code>node tools/render-tree-overview.js</code><br>
        要看真名：<code>--as=admin</code>，或任意**已认领**的账号：<br>
        <code>node tools/render-tree-overview.js --as=mock_new_member</code>
      </div>
    </section>
  </aside>
</div>

<div id="tip"></div>

<script>
(function () {
  var CANVAS_W = ${canvas.width};
  var CANVAS_H = ${canvas.height};
  var canvas = document.getElementById('canvas');
  var sizer = document.getElementById('sizer');
  var viewport = document.getElementById('viewport');
  var read = document.getElementById('scaleRead');
  var scale = 1;

  function apply(s, keepCenter) {
    var vw = viewport.clientWidth, vh = viewport.clientHeight;
    var cx = (viewport.scrollLeft + vw / 2) / scale;
    var cy = (viewport.scrollTop + vh / 2) / scale;
    scale = Math.min(3, Math.max(0.2, s));
    canvas.style.transform = 'scale(' + scale + ')';
    sizer.style.width = (CANVAS_W * scale) + 'px';
    sizer.style.height = (CANVAS_H * scale) + 'px';
    read.textContent = Math.round(scale * 100) + '%';
    if (keepCenter) {
      viewport.scrollLeft = cx * scale - vw / 2;
      viewport.scrollTop = cy * scale - vh / 2;
    }
  }

  document.querySelectorAll('button[data-z]').forEach(function (b) {
    b.addEventListener('click', function () {
      var z = b.getAttribute('data-z');
      if (z === 'fit') apply((viewport.clientWidth - 48) / CANVAS_W);
      else if (z === 'hfit') apply((viewport.clientHeight - 24) / CANVAS_H);
      else if (z === 'in') apply(scale * 1.25, true);
      else if (z === 'out') apply(scale / 1.25, true);
      else apply(1);
    });
  });

  var tip = document.getElementById('tip');
  var NODES = ${JSON.stringify(data.nodes)};
  var byId = {};
  NODES.forEach(function (n) { byId[n.id] = n; });
  var ROLE = { 1: '本族男丁', 2: '本族女儿', 3: '媳妇', 4: '女婿', 5: '外孙' };
  var LIVING = { 1: '在世', 2: '已故', 3: '不详' };

  function showTip(el, e) {
    var n = byId[el.getAttribute('data-id')];
    if (!n) return;
    tip.dataset.id = n.id;
    tip.innerHTML = '<b>' + (n.displayName || n.name || '未详') + '</b><br>' +
      'id ' + n.id + ' · 第' + n.gen + '世 · ' + (ROLE[n.role] || n.role) + '<br>' +
      (LIVING[n.living] || '') + ' · ' +
      (n.gender === 1 ? '男' : n.gender === 2 ? '女' : '未知') + '<br>' +
      'x=' + n.x + ' y=' + n.y + ' w=' + n.w + ' h=' + n.h;
    tip.style.display = 'block';
    move(e);
  }

  document.querySelectorAll('.tn').forEach(function (el) {
    el.addEventListener('mouseenter', function (e) { showTip(el, e); });
    el.addEventListener('mousemove', move);
    el.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
    // 手机上没有 hover：点一下看，再点一下或点空白处收起
    el.addEventListener('click', function (e) {
      e.stopPropagation();
      var n = byId[el.getAttribute('data-id')];
      if (n && tip.style.display === 'block' && tip.dataset.id === String(n.id)) {
        tip.style.display = 'none';
        return;
      }
      showTip(el, e);
    });
  });
  document.addEventListener('click', function () { tip.style.display = 'none'; });

  function move(e) {
    var x = e.clientX + 14, y = e.clientY + 14;
    if (x + 270 > window.innerWidth) x = e.clientX - 270;
    if (y + 110 > window.innerHeight) y = e.clientY - 110;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  // 手机上按宽度全览只有 0.3 左右，姓名糊成一片；改成"适应高度"：
  // 纵向完整、横向可滑，至少字能看清。宽屏仍按宽度全览。
  var initScale = (viewport.clientWidth - 48) / CANVAS_W;
  if (viewport.clientWidth < 700) {
    initScale = Math.max(initScale,
      Math.min(1, (viewport.clientHeight - 24) / CANVAS_H));
  }
  apply(initScale);
  // 放大到超出视口时，从中间开始看（始祖在画布中间）
  viewport.scrollLeft = Math.max(0,
    (CANVAS_W * initScale - viewport.clientWidth) / 2);

  window.addEventListener('resize', function () {
    if (scale > 0) apply(scale);
  });
})();
</script>
</body>
</html>
`;
}

// ---------------------------------------------------------------- 主流程

(async function main() {
  // 全名要靠"已认领 / 管理员"这个身份 —— 脱敏在服务端按 viewer 算，
  // 未登录访客一律脱敏，改前端没用。
  let headers = {};
  let loginUser = null;
  if (VIEW_AS !== 'guest') {
    console.log(`以 ${VIEW_AS} 身份登录（code=${LOGIN_CODE}）…`);
    const r = await loginAs(LOGIN_CODE);
    headers = { Authorization: `Bearer ${r.token}` };
    loginUser = r.user;
    console.log(`  登录成功：${loginUser.nickname || '(无昵称)'} · role=${loginUser.role} ` +
      `· familyId=${loginUser.familyId}`);
    if (String(loginUser.familyId) !== String(FAMILY_ID)) {
      console.log(`  注：该账号 familyId=${loginUser.familyId}，与要看的族 ${FAMILY_ID} 不同，` +
        '视角可能不生效。');
    }
    // role >= 2 是房长及以上（is_admin），1 是普通族人
    if (!loginUser.claimedPersonId && Number(loginUser.role) < 2) {
      console.log('  注：该账号既不是管理员、也没认领节点 → 仍会被当成未认领，' +
        '在世者照旧脱敏。换一个认领过的账号，或加 --as=admin。');
    }
  }

  // --view=mine --anchor=12：看「我的一家」（含媳妇/女婿，夫妻紧贴的效果只在这里看得见）
  const VIEW = String(FLAGS.view || 'overview');
  const ANCHOR = FLAGS.anchor ? String(FLAGS.anchor) : '';
  const url = VIEW === 'mine' || VIEW === 'lineage'
    ? `${BASE}/v1/tree/${FAMILY_ID}?view=${VIEW}&anchor=${ANCHOR}`
    : `${BASE}/v1/tree/${FAMILY_ID}?view=${VIEW}`;
  console.log(`拉取 ${url}`);

  const resp = await requestJson(url, { headers });
  const data = resp.data;

  // 与 pages/home/home.js 一样：先标出"谁的右侧有配偶"，组件才知道哪边切平圆角
  const hasSpouseRight = {};
  data.nodes.forEach((n) => {
    if (n.spouseOf) hasSpouseRight[n.spouseOf] = true;
  });
  data.nodes.forEach((n) => {
    n.flatRight = !!hasSpouseRight[n.id];
    n.flatLeft = !!n.spouseOf;
  });

  const lines = buildLines(data.nodes, data.edges);
  const report = check(data, VIEW_AS);

  // guest 期望 guest，其余期望"至少是 close"（已认领）——管理员给 admin。
  if (VIEW_AS === 'guest' && data.viewerScope !== 'guest') {
    report.notes.push(`期望 viewerScope=guest，实际拿到 ${data.viewerScope}` +
      '（脱敏口径可能跟预期不符）');
  } else if (SHOW_NAMES && data.viewerScope === 'guest') {
    report.notes.push(`以 ${VIEW_AS} 登录了，但 viewerScope 仍是 guest —— ` +
      '这个人应该没认领节点、也不是管理员，姓名照旧脱敏。');
  }

  const outDir = path.join(ROOT, '_verify');
  fs.mkdirSync(outDir, { recursive: true });
  // guest 与 admin 保持历史文件名（README、记忆里都这么引用），其余按身份命名
  const outName = VIEW !== 'overview'
    ? `tree-${VIEW}-${ANCHOR || 'none'}.html`
    : VIEW_AS === 'guest' ? 'tree-overview.html'
      : VIEW_AS === 'admin' ? 'tree-overview-full.html'
        : `tree-overview-${VIEW_AS.replace(/[^\w.-]/g, '_')}.html`;
  const outFile = path.join(outDir, outName);
  fs.writeFileSync(outFile, buildHtml(data, lines, report, { viewAs: VIEW_AS, loginUser }), 'utf8');

  console.log('');
  console.log(`视角 ${VIEW_AS} · viewerScope=${data.viewerScope}`);
  console.log(`节点 ${data.nodes.length} · 连线 ${data.edges.length} · ` +
    `画布 ${data.canvas.width}×${data.canvas.height}`);
  console.log(`buildLines 产出线段 ${lines.length} 段`);
  console.log('');
  if (report.issues.length) {
    console.log('发现问题：');
    report.issues.forEach((s) => console.log('  [X] ' + s));
  } else {
    console.log('体检通过：没有悬空边 / 孤立点 / 坐标异常 / 世次错行');
  }
  if (report.notes.length) {
    report.notes.forEach((s) => console.log('  [!] ' + s));
  }
  console.log('');
  console.log(`已生成 ${outFile}`);
})().catch((e) => {
  console.error('失败：' + e.message);
  process.exit(1);
});
