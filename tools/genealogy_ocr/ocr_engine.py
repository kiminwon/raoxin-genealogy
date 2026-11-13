# -*- coding: utf-8 -*-
"""本地 OCR 引擎：PP-OCR 模型（RapidOCR / ONNX Runtime 离线推理）。

为什么不是 paddlepaddle 本体：本机是 Python 3.13，PaddlePaddle 没有对应
wheel（安装卡了 13 分钟未落一个包）。RapidOCR 跑的是同一套 PP-OCR 训练
模型的 ONNX 版，模型文件随包自带、全程离线、识别结果等价。

竖排识别的关键技巧 —— **竖排转横排**：
老谱一个竖列里的字本身是正的，只是自上而下排列。整列丢给横排 OCR 会被
压成一条糊线。所以先按字切块，再把字块横向拼成一行，交给常规中文识别器。
这样既拿到整行的语言上下文，又不用为竖排单独训模型。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

import config as C
import ocrlib as L


class OcrEngine:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.rapid = RapidOCR()

    def read_raw(self, arr):
        """整页识别，返回原始 [[box, text, score], ...]。

        ⚠️ 主链路走这里：PP-OCR 的检测器会把竖排中文列框成一条竖长框，
        识别器直接吐整列文本（实测 40+ 框/页、置信 0.66）。
        下面的 read()/read_column() 是早期「切字重排」方案的遗留，
        实测 14 列全空，只作为个别漏检裁片的兜底，不要当主路径。
        """
        result, _ = self.rapid(arr)
        return result or []

    def read(self, gray: Image.Image):
        """识别一张横排文字行图，返回 (text, score)。"""
        arr = np.asarray(gray.convert('RGB'))
        result = self.read_raw(arr)
        if not result:
            return '', 0.0
        texts, scores = [], []
        for item in result:
            try:
                texts.append(str(item[1]))
                scores.append(float(item[2]))
            except (IndexError, TypeError, ValueError):
                continue
        return ''.join(texts), (sum(scores) / len(scores) if scores else 0.0)


def smooth(a, k=3):
    return np.convolve(np.asarray(a, dtype=np.float32), np.ones(k) / k, mode='same')


def glyph_spans(ink, col_w):
    """一个竖列里按水平投影切出每个字块。

    先试投影切字；雕版字距过密切不开时，退回按字高固定步长切。
    返回 [(y1,y2), ...]。
    """
    h = ink.shape[0]
    row = smooth(ink.sum(axis=1), 3)
    hit = row > max(1.0, 0.06 * col_w)
    spans, start = [], None
    for i, v in enumerate(hit):
        if v and start is None:
            start = i
        elif not v and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, h))
    spans = [(a, b) for a, b in spans if b - a >= max(3, col_w * 0.25)]

    # 过高的块 = 没切开的连字/印章/栏线，按固定步长再切
    step = max(6.0, col_w * 1.02)
    fixed = []
    for a, b in spans:
        if b - a > col_w * 1.9:
            n = max(1, round((b - a) / step))
            for k in range(n):
                y1 = a + k * (b - a) / n
                y2 = a + (k + 1) * (b - a) / n
                fixed.append((int(y1), int(y2)))
        else:
            fixed.append((a, b))
    spans = fixed

    ys = np.flatnonzero(ink.sum(axis=1) > 0)
    if len(ys) < 2:
        return []
    expect = (ys[-1] - ys[0]) / step
    # 切得太少说明字粘在一起了，改用固定步长从头切
    if len(spans) < max(2, expect * 0.55):
        y0, y1 = int(ys[0]), int(ys[-1]) + 1
        n = max(1, round((y1 - y0) / step))
        spans = [(int(y0 + k * (y1 - y0) / n), int(y0 + (k + 1) * (y1 - y0) / n))
                 for k in range(n)]
    return [(a, b) for a, b in spans if b > a]


def reflow_column(crop: Image.Image, glyph_h=C.GLYPH_ROW_H, max_glyphs=C.MAX_GLYPHS):
    """竖列裁片 -> 横排文字行图（字块横向拼接）。

    返回 (横行图, 字块数, 是否异常)。异常（超长列/字块过多）会降置信。
    """
    ink = L.ink_mask(crop)
    w = ink.shape[1]
    if w < 4:
        return None, 0, True
    spans = glyph_spans(ink, w)[:max_glyphs]
    bad = len(spans) == 0 or len(spans) >= max_glyphs
    if not spans:
        return None, 0, True

    pad = 6
    tiles = []
    for a, b in spans:
        g = crop.crop((0, max(0, a - 1), crop.width, min(crop.height, b + 1)))
        # 补成正方形，避免字被拉伸变形
        side = max(g.width, g.height) + pad
        sq = Image.new('L', (side, side), 255)
        sq.paste(g, ((side - g.width) // 2, (side - g.height) // 2))
        tiles.append(sq.resize((glyph_h, glyph_h), Image.LANCZOS))

    gap = 3
    row = Image.new('L', (len(tiles) * (glyph_h + gap) + 12, glyph_h + 12), 255)
    for i, t in enumerate(tiles):
        row.paste(t, (6 + i * (glyph_h + gap), 6))
    return row, len(spans), bad


def read_column(engine, crop, chunk=12):
    """一列可能几十字，长行识别会掉准确率：按 chunk 个字分段识别再拼。"""
    row, n, bad = reflow_column(crop)
    if row is None:
        return '', 0.0, 0, True
    # 按字块宽度切段（每块 glyph_h + gap）
    unit = C.GLYPH_ROW_H + 3
    total = row.width
    texts, scores = [], []
    for x0 in range(0, total, unit * chunk):
        x1 = min(total, x0 + unit * chunk + 6)
        piece = row.crop((max(0, x0 - 6), 0, x1, row.height))
        t, s = engine.read(piece)
        if t:
            texts.append(t)
            scores.append(s)
    text = ''.join(texts)
    return text, (sum(scores) / len(scores) if scores else 0.0), n, bad
