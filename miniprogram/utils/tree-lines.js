// 把服务端下发的 edges 翻译成可以直接渲染的线段。
//
// 为什么用 view 画线而不是 canvas：
//   canvas 在小程序里是原生组件，层级永远盖在最上面，
//   节点卡片会被它压住，点击也收不到。用细长的 view 拼折线没有这个问题，
//   代价是每段线一个 DOM 节点，在 600 节点以内的规模上完全可接受。
//
// 连线形状用家谱的传统画法：父 → 中线 → 子，正交折线，不用斜线。
// 斜线在人多的时候会糊成一片麻，正交线看起来才像一张谱。
//
// 多配偶的两条规则（2026-10-21）：
//   1. 婚姻线改成卡片上方的「横梁」——直接连两个人的中心会横穿中间那位夫人的卡片；
//   2. 亲子线从**生母**正下方下垂（服务端在边上带 motherId）——孩子本来就排在
//      生母那一列下面，从父亲斜拉过去会横着跨过好几列。

const EDGE = {
  PARENT: 1,   // 父子：实线
  MARRY: 2,    // 婚姻：金色横线
  ADOPT: 3,    // 过继：虚线
  EXT: 4,      // 外姓（女婿 / 外孙）：更淡的虚线
};

const HALF = 0.75; // 1.5px 线宽的一半，让线段中线落在坐标上而不是左边缘

function buildLines(nodes, edges) {
  const map = {};
  for (const n of nodes) map[n.id] = n;

  const lines = [];
  let seq = 0;
  const push = (cls, style) => lines.push({ key: 'ln' + seq++, cls, style });
  const cx = (n) => n.x + n.w / 2;

  // ---------- 婚姻：抬到卡片上方的横梁 ----------
  // 多妻时按 unionSeq 分高度（每段差 4px），各自一条，互不覆盖；
  // 已结束的（离异/丧偶）画虚线，但人照常留在图上。
  for (const e of edges) {
    if (e.type !== EDGE.MARRY) continue;
    const a = map[e.from];
    const b = map[e.to];
    if (!a || !b) continue;
    if (Math.abs(a.y - b.y) > 2) continue; // 不在同一行：数据异常，宁可不画

    const topY = Math.min(a.y, b.y);
    const railY = topY - 7 - ((e.unionSeq || 1) - 1) * 4;
    const x1 = Math.min(cx(a), cx(b));
    const x2 = Math.max(cx(a), cx(b));
    const ended = !!e.ended;

    const hCls = 'ln-h ln-marry' + (ended ? ' ln-marry-ended' : '');
    push(hCls, `left:${x1}px;top:${railY}px;width:${x2 - x1}px;`);

    const vCls = 'ln-v ln-marry' + (ended ? ' ln-marry-ended' : '');
    for (const n of [a, b]) {
      push(vCls, `left:${cx(n) - HALF}px;top:${railY}px;height:${topY - railY}px;`);
    }
  }

  // ---------- 亲子：正交折线（竖 — 横 — 竖） ----------
  for (const e of edges) {
    if (e.type === EDGE.MARRY) continue;
    const a = map[e.from];
    const b = map[e.to];
    if (!a || !b) continue;

    // 生母在屏上就从她下垂：孩子本来就排在她那一列。
    const mother = e.motherId ? map[e.motherId] : null;
    const src = mother && Math.abs(mother.y - a.y) < 2 ? mother : a;

    const sx = cx(src);
    const sy = src.y + src.h;
    const tx = cx(b);
    const ty = b.y;

    // 世代数据异常（子不在父下方）时宁可不画，也不画出一条回溯的乱线
    if (ty - sy <= 1) continue;

    const midY = sy + (ty - sy) / 2;
    const base =
      e.type === EDGE.ADOPT ? 'ln ln-adopt' : e.type === EDGE.EXT ? 'ln ln-ext' : 'ln';

    push(base + ' ln-v', `left:${sx - HALF}px;top:${sy}px;height:${midY - sy}px;`);

    if (Math.abs(tx - sx) > 1) {
      const lx = Math.min(sx, tx);
      push(
        base + ' ln-h',
        `left:${lx}px;top:${midY - HALF}px;width:${Math.abs(tx - sx)}px;`
      );
    }

    push(base + ' ln-v', `left:${tx - HALF}px;top:${midY}px;height:${ty - midY}px;`);
  }

  return lines;
}

module.exports = { EDGE, buildLines };
