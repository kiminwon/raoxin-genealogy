# -*- coding: utf-8 -*-
"""第 3 步：整页 OCR + 版面归属（2026-11-14 重写）。

⚠️ 这一版推翻了「投影切列 → 竖排转横排 → 逐列识别」的原设计。
实测：手写投影切列把横排大字、格线切成碎片，14 列识别结果全空；
而 **整页直接送 PP-OCR，检测器自己就能把竖排中文列框成一条**，
识别器直接吐出「成化两戌年 / 壽一公長予 / 第一世 / 生子二」这类整列文本，
每页 40+ 框、平均置信 0.66、耗时 4-6 秒。既然模型原生支持竖排，
就不要自己造轮子——版面归属改为「拿 OCR 框坐标 + 横线分代」。

产出 segments.json：columns 已带 text/conf，按阅读顺序
（页序 → 世代行 → 列从右到左）排好。坐标是归一化 0..1，
provenance 可回原图裁片，05 复核台要用。

附带做两件事：
  1. 汉字率异常低的页自动用 180° 复跑对比，就地改方向（方向兜底）；
  2. 断点续跑：每页结果追加 ocr_lines.jsonl，重跑跳过已完成页。

用法：python 02_segment.py [页数上限] [起始页序号]
"""
from __future__ import annotations

import json, re, sys, time
from pathlib import Path

import numpy as np
from PIL import Image

import config as C
import ocrlib as L
from ocr_engine import OcrEngine

HAN = lambda s: sum(1 for ch in s if '\u4e00' <= ch <= '\u9fff')


def page_source():
    p = C.OUT / 'pages_resolved.json'
    return p if p.exists() else (C.OUT / 'pages.json')


def work_image(p, rotate=None):
    deg = p['rotate'] if rotate is None else rotate
    im = L.rotate_deg(Image.open(C.SRC / p['name']).convert('L'), deg)
    if im.width > C.WORK_W:
        im = im.resize((C.WORK_W, round(im.height * C.WORK_W / im.width)), Image.LANCZOS)
    return im


def row_bounds(work, boxes):
    """世代行边界（归一化）。

    实测：横线检测在齿录页几乎全失效（斜线、双描边、墨色淡），
    在源流页也会漏线（第一页 5 个世次只抓到 4 条）。所以优先级改成：

      1. **「第 N 世」印章**（OCR 框自带 y）——最强锚，源流页专用；
      2. 框顶 y 一维聚类 ——齿录页按人物块切，间隔 > 0.09 页高断开；
      3. 横线 ——只作辅证，落在聚类切点附近就采纳。

    返回 (bounds, source)。
    """
    h = work.height
    seals, seen = [], set()
    for b in boxes:
        m = re.search(r'第?([一二三四五六七八九十百]{1,4})世', b['text'])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            seals.append((b['y'] + b['h'] / 2) / h)
    seals.sort()
    if len(seals) >= 2 and all(seals[i] > seals[i - 1] + 0.03 for i in range(1, len(seals))):
        return [0.0] + [(seals[i - 1] + seals[i]) / 2 for i in range(1, len(seals))] + [1.0], 'seal'

    ys = sorted((b['y'] + b['h'] * 0.15) / h for b in boxes)
    if not ys:
        return [0.0, 1.0], 'empty'
    cuts = []
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] > 0.09:
            cuts.append((ys[i - 1] + ys[i]) / 2)
    # 太窄的行（<6% 页高）多半是同一行里起笔高低差，合并掉
    bounds = [0.0] + cuts + [1.0]
    bounds = [bounds[0]] + [b for i, b in enumerate(bounds[1:-1], 1)
                            if b - bounds[i - 1] > 0.06] + [1.0]
    return bounds, 'cluster'


def read_page(engine, work):
    """整页 OCR → [(x,y,w,h,text,conf)]，坐标是工作图像素。"""
    arr = np.asarray(work.convert('RGB'))
    result = engine.read_raw(arr)
    out = []
    for it in result or []:
        try:
            box, text, score = it[0], str(it[1]), float(it[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not text.strip():
            continue
        xs = [float(pt[0]) for pt in box]
        ys = [float(pt[1]) for pt in box]
        out.append(dict(x=min(xs), y=min(ys), w=max(xs) - min(xs),
                        h=max(ys) - min(ys), text=text, conf=round(score, 3)))
    return out


def quality(boxes):
    """一页的「识别质量」：汉字数 × 平均置信。倒着的页汉字会骤减。"""
    if not boxes:
        return 0.0
    han = sum(HAN(b['text']) for b in boxes)
    conf = sum(b['conf'] for b in boxes) / len(boxes)
    return han * conf


def assign(boxes, bounds, w, h):
    """把 OCR 框归到世代行，行内按 x 从右到左编号，输出归一化坐标。"""
    rows = {}
    for b in boxes:
        cy = (b['y'] + b['h'] / 2) / h
        ri = 0
        for i in range(len(bounds) - 1):
            if bounds[i] <= cy < bounds[i + 1]:
                ri = i
                break
        else:
            ri = len(bounds) - 2
        rows.setdefault(ri, []).append(b)
    out = []
    for ri in sorted(rows):
        for ci, b in enumerate(sorted(rows[ri], key=lambda x: -x['x'])):
            out.append(dict(
                row=ri, col=ci,
                x1=round(max(0.0, b['x'] / w), 5), x2=round(min(1.0, (b['x'] + b['w']) / w), 5),
                y1=round(max(0.0, b['y'] / h), 5), y2=round(min(1.0, (b['y'] + b['h']) / h), 5),
                text=b['text'], conf=b['conf'],
                vertical=bool(b['h'] > b['w']),
                box=[round(b['x']), round(b['y']), round(b['w']), round(b['h'])],
            ))
    return out


def main():
    t0 = time.time()
    d = json.loads(page_source().read_text(encoding='utf-8'))
    pages = [p for p in d['pages'] if not p.get('dupOf')]
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(pages)
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    pages = pages[start:start + limit]

    done = set()
    jl = C.OUT / 'ocr_lines.jsonl'
    if jl.exists():
        with jl.open(encoding='utf-8') as f:
            for line in f:
                try:
                    done.add(json.loads(line)['name'])
                except Exception:
                    pass

    engine = OcrEngine()
    flipped = []
    with jl.open('a', encoding='utf-8') as f:
        for pi, p in enumerate(pages):
            if p['name'] in done:
                continue
            work = work_image(p)
            boxes = read_page(engine, work)
            q = quality(boxes)

            # 方向兜底：汉字率过低（含整页空白）时用 180° 复跑，谁像中文用谁
            han_rate = (HAN(''.join(b['text'] for b in boxes)) /
                        max(1, sum(len(b['text']) for b in boxes)))
            if q < 12 or han_rate < 0.35:
                work2 = work_image(p, (p['rotate'] + 180) % 360)
                boxes2 = read_page(engine, work2)
                q2 = quality(boxes2)
                if q2 > q * 1.5:
                    work, boxes, q = work2, boxes2, q2
                    p['rotate'] = (p['rotate'] + 180) % 360
                    p['note'] = (p.get('note', '') + ' 整页OCR方向复核翻180').strip()
                    p['conf'] = 0.9
                    flipped.append(p['name'][21:27])

            w, h = work.size
            bounds, row_src = row_bounds(work, boxes)
            cols = assign(boxes, bounds, w, h)
            rec = dict(name=p['name'], rotate=p['rotate'], size=[w, h],
                       lines=bounds, rowSource=row_src, quality=round(q, 2),
                       hanRate=round(han_rate, 3), cols=cols)
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            f.flush()
            if (pi + 1) % 5 == 0:
                el = time.time() - t0
                print(f'  {pi+1}/{len(pages)}  {el:.0f}s  ~{el/(pi+1):.1f}s/页  翻向 {len(flipped)}',
                      flush=True)

    # 汇总：把每页的列拼成全局序列，重排 id/seq 保持下游契约
    recs = {}
    with jl.open(encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            recs[r['name']] = r
    columns, page_meta = [], []
    seq = 0
    ordered = [p for p in json.loads(page_source().read_text(encoding='utf-8'))['pages']
               if not p.get('dupOf')][start:start + limit]
    for pi, p in enumerate(ordered):
        r = recs.get(p['name'])
        if not r:
            continue
        for c in r['cols']:
            columns.append(dict(c, id=f'c{seq:06d}', seq=seq,
                                page=p['name'], pageIndex=pi,
                                rotate=r['rotate'], raw=c['text']))
            seq += 1
        page_meta.append(dict(name=p['name'], rotate=r['rotate'], rows=max(1, len(r['lines']) - 1),
                              cols=len(r['cols']), conf=p.get('conf', 0),
                              partial=p.get('partial'), cls=p.get('cls'),
                              rowSource=r.get('rowSource', '?'),
                              quality=r['quality'], hanRate=r['hanRate']))
        p['rotate'] = r['rotate']

    payload = dict(generated=time.strftime('%Y-%m-%d %H:%M:%S'),
                   totalPages=len(page_meta), totalColumns=len(columns),
                   pages=page_meta, columns=columns)
    (C.OUT / 'segments.json').write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    # 方向翻转要落回清单：05 复核台按 pages_resolved 的 rotate 现裁原图，
    # 不写回就会出现「文字按 180° 识别、裁片却按 0° 裁」的错位。
    if flipped:
        src = json.loads(page_source().read_text(encoding='utf-8'))
        fix = {p['name']: p for p in ordered}
        for q in src['pages']:
            if q['name'] in fix:
                q['rotate'] = fix[q['name']]['rotate']
                q['conf'] = fix[q['name']].get('conf', q['conf'])
                q['note'] = fix[q['name']].get('note', q.get('note', ''))
        src['directionResolvedByPageOcr'] = dict(flipped=flipped,
                                                 generated=time.strftime('%Y-%m-%d %H:%M:%S'))
        (C.OUT / 'pages_resolved.json').write_text(
            json.dumps(src, ensure_ascii=False, indent=1), encoding='utf-8')

    got = [c for c in columns if c['text']]
    confs = [c['conf'] for c in got]
    low = [c for c in got if c['conf'] < 0.60]
    print('\n=== 整页 OCR + 版面归属 ===')
    print('页数', len(page_meta), '文本列', len(got), '耗时 %.0fs' % (time.time() - t0))
    print('平均置信 %.3f  低置信(<0.60) %d / %d = %.1f%%' %
          (sum(confs) / max(1, len(confs)), len(low), len(got),
           100 * len(low) / max(1, len(got))))
    print('每页列数 min/中位/max:',
          min((p['cols'] for p in page_meta), default=0),
          sorted(p['cols'] for p in page_meta)[len(page_meta) // 2] if page_meta else 0,
          max((p['cols'] for p in page_meta), default=0))
    print('方向翻转', len(flipped), flipped[:10])
    print('低汉字率页(疑似空/倒/半页):',
          [p['name'][21:27] for p in page_meta if p['hanRate'] < 0.35][:12])
    print('样例:', [c['text'][:14] for c in got[:10]])
    print('产物:', C.OUT / 'segments.json', jl)


if __name__ == '__main__':
    main()
