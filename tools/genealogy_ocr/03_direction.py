# -*- coding: utf-8 -*-
"""方向复核（内容驱动）：对 01 步低置信的页，用 OCR 在 0°/180° 间定夺。

01 的图像信号在半页、缺版心栏的页上会打架（全库约四成）。这里不猜笔画，
而是让识别器自己投票：同一条竖列在正确朝向下能读出更多汉字、置信度更高，
倒过来只会得到零散符号。只跑 1 条探针列 × 2 个候选，成本很低。

输出 pages_resolved.json；02 步优先读它。
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path

from PIL import Image

import config as C
import ocrlib as L
from ocr_engine import OcrEngine, read_column


def probe_column(im: Image.Image):
    """挑一条最有字的竖窄条作为方向探针（避开空白页和栏线）。"""
    small = im.resize((240, max(1, round(im.height * 240 / im.width))), Image.BILINEAR)
    ink = L.ink_mask(small)
    w = ink.shape[1]
    k = max(12, w // 10)
    best, bi = -1, 0
    colsum = ink.sum(axis=0)
    colsum[colsum > ink.shape[0] * 0.8] = 0        # 栏线不算
    for x in range(0, w - k, max(2, k // 3)):
        s = float(colsum[x:x + k].sum())
        if s > best:
            best, bi = s, x
    x1 = int(bi / w * im.width)
    x2 = min(im.width, int((bi + k) / w * im.width))
    y1, y2 = int(im.height * 0.12), int(im.height * 0.88)
    return im.crop((x1, y1, max(x1 + 4, x2), y2))


def score_direction(engine, im, rotate):
    r = L.rotate_deg(im, rotate)
    crop = probe_column(r)
    text, conf, n, bad = read_column(engine, crop)
    # 汉字占比也算分：倒着的字常被识别成拉丁字母/符号
    han = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return dict(rotate=rotate % 360, text=text[:24], conf=round(conf, 3),
                glyphs=n, han=han, score=round(han * conf + conf, 3))


def main():
    t0 = time.time()
    src = C.OUT / 'pages.json'
    d = json.loads(src.read_text(encoding='utf-8'))
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.7
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10 ** 9
    targets = [p for p in d['pages'] if p['conf'] < threshold and not p.get('dupOf')][:limit]
    print(f'待复核 {len(targets)} 页 / 共 {len(d["pages"])} 页', flush=True)

    engine = OcrEngine()
    changed, ambiguous = [], []
    for i, p in enumerate(targets):
        im = L.load_gray(C.SRC / p['name'], 900)
        a = score_direction(engine, im, p['rotate'])
        b = score_direction(engine, im, (p['rotate'] + 180) % 360)
        win = b if b['score'] > a['score'] else a
        gap = abs(a['score'] - b['score']) / max(0.001, max(a['score'], b['score']))
        if gap < 0.12:
            ambiguous.append(p['name'])
            p['note'] = (p['note'] + ' OCR 方向两可').strip()
            p['conf'] = min(p['conf'], 0.5)
        else:
            if win['rotate'] != p['rotate']:
                changed.append((p['name'][21:27], p['rotate'], win['rotate']))
                p['rotate'] = win['rotate']
            p['conf'] = 0.9
            p['note'] = 'OCR 方向复核通过'
            p['dirProbe'] = dict(a=a, b=b)
        if (i + 1) % 20 == 0:
            print(f'  {i+1}/{len(targets)}  {time.time()-t0:.0f}s  改向 {len(changed)}', flush=True)

    d['directionResolved'] = dict(checked=len(targets), changed=changed,
                                  ambiguous=ambiguous,
                                  generated=time.strftime('%Y-%m-%d %H:%M:%S'))
    (C.OUT / 'pages_resolved.json').write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')

    print('\n=== 方向复核 ===')
    print('检查', len(targets), '改向', len(changed), '两可', len(ambiguous),
          '耗时 %.0fs' % (time.time() - t0))
    print('改向示例:', changed[:10])
    print('产物:', C.OUT / 'pages_resolved.json')


if __name__ == '__main__':
    main()
