# -*- coding: utf-8 -*-
"""第 0/1/2 步：方向归一、去重、半页配对。

输出 _ocr_out/pages.json —— 后续所有步骤只认这份清单，不再猜方向。
原图只读；本步不生成大图，转正结果按需在 02 步生成。

方向判定的两个独立信号（都不需要 OCR）：
  A 列首整齐度：竖列都从格线起笔，正确朝向下顶部方差 < 底部方差
  B 版心栏偏侧：线装书版心栏/标题栏在页面右侧，右边缘墨迹 > 左边缘
两票一致 = 高置信；打架 = 低置信进人工队列（通常出现在半页/空白页）。
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path

from PIL import Image, ImageDraw

import config as C
import ocrlib as L


def analyze_one(fp: Path):
    g = L.load_gray(fp)
    w0, h0 = Image.open(fp).size
    ink = L.ink_mask(g)
    hl, vl = L.long_line_count(ink, 'h'), L.long_line_count(ink, 'v')
    # U/R 类只看宽高比：谱页本身是竖长方形，横躺的一定是拍摄时转了 90°。
    # 不能用「横线多还是竖线多」判——源流总世系是表格，横竖实线都有，
    # 实测会把正常竖版页误判成需要转 90°。
    ratio = w0 / h0
    if ratio < 0.90:
        cls, cands = 'U', (0, 180)
    elif ratio > 1.90:
        cls, cands = 'W', (0, 180)      # 极扁：可能是展开双页，先按 U 处理并标记
    else:
        cls, cands = 'R', (90, 270)

    scored = []
    for deg in cands:
        r = L.rotate_deg(g, deg)
        ri = L.ink_mask(r)
        tv, bv = L.column_alignment(ri)
        align = tv - bv                       # 越负 = 顶部越齐 = 越像正确朝向
        asym = L.edge_ink(ri, 'R') - L.edge_ink(ri, 'L')
        bbox = L.content_bbox(ri)
        scored.append(dict(deg=deg, align=align, asym=asym, bbox=bbox,
                           gray=r, ink=ri))

    # 主判据 = 版心栏/标题栏在右（实测这批图上 asym 双峰极干净：
    # 正向 +0.14~0.24，倒向 -0.15~-0.21，几乎没有中间值）。
    # 辅判据 = 列首整齐度，只用来标"建议 OCR 复核"，不推翻主判据。
    b_best = max(scored, key=lambda x: x['asym'])
    a_best = min(scored, key=lambda x: x['align'])
    pick = b_best
    conf = 0.9 if pick['asym'] > 0.08 else 0.55
    note = ''
    gap = abs(scored[0]['align'] - scored[1]['align'])
    if a_best['deg'] != b_best['deg']:
        conf = min(conf, 0.6)
        note = '列首信号与版心栏不一致，建议 OCR 复核'
    if gap < 0.002:
        note = (note + ' 列首信号弱').strip()

    b = pick['bbox']
    cover_x, cover_y = b[2] - b[0], b[3] - b[1]
    cover = min(cover_x, cover_y)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    partial, miss_axis = None, None
    # ⚠️ 半页可能缺在横向，也可能缺在纵向：照片横躺时原本的"左右半页"
    # 转正 90° 后会变成"上下半页"。只看 x 会把这批全漏掉（实测踩过）。
    if cover < C.PARTIAL_COVER:
        if cover_x <= cover_y:
            miss_axis = 'x'
            partial = ('L' if cx < 0.5 - C.PARTIAL_BIAS else
                       'R' if cx > 0.5 + C.PARTIAL_BIAS else 'C')
        else:
            miss_axis = 'y'
            partial = ('T' if cy < 0.5 - C.PARTIAL_BIAS else
                       'B' if cy > 0.5 + C.PARTIAL_BIAS else 'C')

    if cls == 'W':
        conf = min(conf, 0.5)
        note = (note + ' 疑似展开双页，需人工确认是否要旋转').strip()

    return dict(
        name=fp.name, w=w0, h=h0, ratio=round(ratio, 3),
        rotate=pick['deg'], cls=cls,
        hLong=hl, vLong=vl, alignGap=round(gap, 5),
        asym=round(pick['asym'], 4),         bbox=[round(x, 4) for x in b],
        cover=round(cover, 4), coverX=round(cover_x, 4), coverY=round(cover_y, 4),
        partial=partial, missAxis=miss_axis,
        conf=conf, note=note,
        hash=L.dhash(pick['gray']), sharp=round(L.sharpness(pick['gray']), 1),
    )


def mark_duplicates(recs, window=4):
    """同一页连着拍了几次：留最清晰的一张，其余标 dupOf。"""
    for i, r in enumerate(recs):
        for j in range(i + 1, min(i + window + 1, len(recs))):
            s = recs[j]
            if L.hamming(r['hash'], s['hash']) <= C.DUP_HAMMING:
                keep, drop = (r, s) if r['sharp'] >= s['sharp'] else (s, r)
                if drop.get('dupOf') is None:
                    drop['dupOf'] = keep['name']
                    drop['note'] = (drop['note'] + ' 重复拍摄').strip()
                elif keep.get('dupOf') is None and keep['sharp'] > r['sharp']:
                    pass
    return recs


def pair_partials(recs):
    """相邻且左右互补的半页合成一个逻辑页。

    竖排右起：正文偏右的那半先读，偏左的后读。这里只记阅读顺序，
    不拼像素——拼列才是本流水线的基本原则。
    """
    live = [r for r in recs if not r.get('dupOf')]
    used = set()
    pairs = []
    # 互补规则：x 向 R↔L（竖排右起，右半先读）；y 向 T↔B（上半先读）
    complement = {'R': 'L', 'L': 'R', 'T': 'B', 'B': 'T'}
    first_side = {'R', 'T'}
    for i, a in enumerate(live):
        pa = a.get('partial')
        if a['name'] in used or pa not in complement:
            continue
        for j in range(i + 1, min(i + 3, len(live))):
            b = live[j]
            pb = b.get('partial')
            if b['name'] in used or pb != complement[pa]:
                continue
            if a.get('missAxis') != b.get('missAxis'):
                continue
            first, second = (a, b) if pa in first_side else (b, a)
            pairs.append(dict(first=first['name'], second=second['name'],
                              axis=a.get('missAxis'),
                              sides=[pa, pb],
                              timeOrder=[a['name'], b['name']],
                              flipped=(first['name'] != a['name'])))
            used.add(a['name']); used.add(b['name'])
            a['pair'] = b['name']; b['pair'] = a['name']
            break
    return pairs, [r['name'] for r in live if r['partial'] and r['name'] not in used]


def contact_sheet(recs, out: Path, limit=24):
    """转正后的核对联系表：一眼扫方向有没有判反。"""
    cell_w, cell_h, cols = 200, 300, 6
    rows = (min(limit, len(recs)) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * cell_w, rows * (cell_h + 20)), 'white')
    d = ImageDraw.Draw(sheet)
    for i, r in enumerate(recs[:limit]):
        src = C.SRC / r['name']
        g = L.rotate_deg(L.load_gray(src, 400), r['rotate']).convert('RGB')
        g.thumbnail((cell_w - 8, cell_h - 6))
        x, y = (i % cols) * cell_w, (i // cols) * (cell_h + 20)
        sheet.paste(g, (x + 4, y + 18))
        d.text((x + 4, y + 3), f"{r['name'][21:27]} r{r['rotate']:>3} c{r['conf']:.2f}",
               fill='red' if r['conf'] < 0.6 else 'black')
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=82)


def main():
    t0 = time.time()
    C.OUT.mkdir(parents=True, exist_ok=True)
    fs = sorted(f for f in C.SRC.iterdir() if f.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(fs)
    fs = fs[:limit]
    print(f'扫描 {len(fs)} 张 ...', flush=True)

    recs = []
    for i, fp in enumerate(fs):
        recs.append(analyze_one(fp))
        if (i + 1) % 50 == 0:
            print(f'  {i+1}/{len(fs)}  {time.time()-t0:.0f}s', flush=True)

    for r in recs:
        r.pop('gray', None); r.pop('ink', None)
    mark_duplicates(recs)
    pairs, orphan_partials = pair_partials(recs)

    # 全库自检：转正后版心栏应整体偏右，否则方向信号可能整体判反
    asyms = [r['asym'] for r in recs if not r.get('dupOf')]
    median_asym = sorted(asyms)[len(asyms) // 2] if asyms else 0

    payload = dict(
        src=str(C.SRC), generated=time.strftime('%Y-%m-%d %H:%M:%S'),
        total=len(recs),
        medianAsym=round(median_asym, 4),
        directionWarning=(median_asym < 0),
        pairs=pairs, orphanPartials=orphan_partials,
        pages=recs,
    )
    (C.OUT / 'pages.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    contact_sheet(recs, C.OUT / 'contact_direction.jpg')

    from collections import Counter
    print('\n=== 方向归一结果 ===')
    print('总数', len(recs), '耗时 %.0fs' % (time.time() - t0))
    print('需旋转 90/270:', sum(1 for r in recs if r['rotate'] in (90, 270)))
    print('需旋转 180   :', sum(1 for r in recs if r['rotate'] == 180))
    print('R 类(原本横躺):', sum(1 for r in recs if r['cls'] == 'R'))
    print('低置信待复核  :', sum(1 for r in recs if r['conf'] < 0.6))
    print('重复拍摄      :', sum(1 for r in recs if r.get('dupOf')))
    print('半页          :', sum(1 for r in recs if r['partial']))
    print('半页成对      :', len(pairs), ' 落单:', len(orphan_partials))
    print('版心栏中位偏移:', round(median_asym, 4),
          '⚠ 整体判反' if median_asym < 0 else '（正向，正常）')
    print('\n半页配对示例:', pairs[:6])
    print('\n低置信示例:', [(r['name'][21:27], r['note']) for r in recs if r['conf'] < 0.6][:8])
    print('\n产物:', C.OUT / 'pages.json', C.OUT / 'contact_direction.jpg')


if __name__ == '__main__':
    main()
