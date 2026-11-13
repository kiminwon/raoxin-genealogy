# -*- coding: utf-8 -*-
"""老谱版面分析的公共图像函数。

只用 numpy + Pillow。所有分析都在缩小图上进行；原图只在导出裁片时打开。

术语：
  ink      二值墨迹图（1=有字/有线），先做局部背景校正，压掉纸色渐变和阴影
  U 类朝向 世代格横线是水平的（即 0° / 180° 两个候选）
  R 类朝向 需要转 ±90° 才是 U 类
  版心栏   线装书页面一侧那条贯穿全高的密集竖栏（书名/卷次），是判方向的强锚
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

import config as C


def load_gray(fp, width=C.ANALYZE_W):
    """读图 -> 等比缩小灰度图。EXIF 方向一律不理（RECTIFY 图已无 EXIF 旋转）。"""
    im = Image.open(fp).convert('L')
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.BILINEAR)
    return im


def ink_mask(gray, delta=C.INK_DELTA):
    """局部背景校正后的墨迹二值图。"""
    a = np.asarray(gray, dtype=np.float32)
    h, w = a.shape
    k = max(8, min(h, w) // 16)
    bg = np.asarray(
        gray.resize((max(1, w // k), max(1, h // k)), Image.BILINEAR)
        .resize((w, h), Image.BILINEAR),
        dtype=np.float32,
    )
    return ((bg - a) > delta).astype(np.uint8)


def line_density(ink, axis='h', block=C.LINE_BLOCK):
    """把每行/每列切成若干块，返回「每块墨迹密度」矩阵。

    真·实线：整行每一块都密；文字：只零星几块有墨。
    """
    m = ink if axis == 'h' else ink.T
    n = m.shape[1] // block
    if n < 2:
        return np.zeros((m.shape[0], 1), dtype=np.float32)
    return m[:, : n * block].reshape(m.shape[0], n, block).mean(axis=2).astype(np.float32)


def long_line_count(ink, axis='h', min_density=C.LINE_MIN_DENSITY):
    """贯穿整行/整列的实线条数。

    用「分块平均密度」而不是分块最小值：格线常有 ±1~2° 残余倾斜，
    跨 640px 会漂移十几个像素，min 会被斜线打散，mean 仍能抓到。
    文字行的平均密度通常 <0.3，不会误判成实线。
    """
    d = line_density(ink, axis)
    return int((d.mean(axis=1) > min_density).sum())


def line_positions(ink, axis='h', min_density=0.42):
    """返回贯穿实线的中心位置（归一化 0..1），用于世代行切分与对齐。"""
    d = line_density(ink, axis)
    hit = d.mean(axis=1) > min_density
    ys, start = [], None
    for i, v in enumerate(hit):
        if v and start is None:
            start = i
        elif not v and start is not None:
            ys.append((start + i - 1) / 2 / len(hit))
            start = None
    if start is not None:
        ys.append((start + len(hit) - 1) / 2 / len(hit))
    return ys


def edge_ink(ink, side, frac=C.EDGE_FRAC):
    k = max(3, int(ink.shape[1] * frac))
    band = ink[:, :k] if side == 'L' else ink[:, -k:]
    return float(band.mean())


def column_alignment(ink):
    """列首/列尾整齐度：正确朝向下，竖列都从格线起笔，顶部更齐。

    ⚠️ 必须先剔除贯穿实线（世代格横线）和贯穿竖栏，否则横线会在
    每一列顶部造出一个"假字"，两个 180 候选的顶部方差都≈0，信号全废。
    返回 (顶部方差, 底部方差)，方差小的那一侧是「上」。
    """
    m = ink.copy()
    hd = line_density(m, 'h')
    m[hd.mean(axis=1) > 0.42, :] = 0      # 去横线（含略斜的）
    col = m.sum(axis=0)
    col[col > m.shape[0] * 0.80] = 0      # 去版心栏/边框竖线
    keep = col > np.percentile(col, 60)
    sub = m[:, keep]
    if sub.shape[1] < 3:
        return 1.0, 1.0
    tops, bottoms = [], []
    for j in range(sub.shape[1]):
        ys = np.flatnonzero(sub[:, j])
        if len(ys) < 3:
            continue
        tops.append(ys[0] / sub.shape[0])
        bottoms.append(ys[-1] / sub.shape[0])
    if len(tops) < 3:
        return 1.0, 1.0
    return float(np.var(tops)), float(np.var(bottoms))


def content_bbox(ink, thr=0.015):
    """墨迹主体包围盒（归一化），用来识别半页：大片空白 + 单侧一条文字。

    先剔掉贯穿全高的竖栏线，再做平滑、取 2%/98% 分位抗零散噪点；
    阈值太松会把纸面脏点也算成正文，半页就被撑成满宽（踩过一次）。
    """
    h, w = ink.shape
    col = ink.sum(axis=0).astype(np.float32)
    col[col > h * 0.80] = 0          # 栏线不算正文
    col = np.convolve(col, np.ones(5) / 5, mode='same')
    row = ink.sum(axis=1).astype(np.float32)
    row = np.convolve(row, np.ones(5) / 5, mode='same')
    cx = np.flatnonzero(col > h * thr)
    cy = np.flatnonzero(row > w * thr)
    if len(cx) < 4 or len(cy) < 4:
        return (0.0, 0.0, 1.0, 1.0)
    return (float(np.percentile(cx, 2) / w), float(np.percentile(cy, 2) / h),
            float(np.percentile(cx, 98) / w), float(np.percentile(cy, 98) / h))


def dhash(gray, size=16):
    """感知哈希（差值哈希），用于同一页多次拍摄去重。"""
    a = np.asarray(gray.resize((size + 1, size), Image.BILINEAR), dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int(''.join('1' if b else '0' for b in bits), 2)


def hamming(a, b):
    return (a ^ b).bit_count()


def sharpness(gray):
    """拉普拉斯方差近似，用来在重复拍摄里挑最清晰的一张。"""
    e = np.asarray(gray.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    return float(e.var())


def rotate_deg(gray, deg):
    """按 90 的倍数旋转（无插值损失）。"""
    d = deg % 360
    if d == 0:
        return gray
    if d == 90:
        return gray.transpose(Image.Transpose.ROTATE_270)   # 顺时针 90
    if d == 180:
        return gray.transpose(Image.Transpose.ROTATE_180)
    if d == 270:
        return gray.transpose(Image.Transpose.ROTATE_90)    # 逆时针 90
    raise ValueError(deg)


def deskew_best(gray, lo=-2.0, hi=2.0, step=0.5):
    """小角度拉正：横线转正时能贯穿整行的实线条数最多。

    RECTIFY 图残余倾斜一般 <2°，只需粗搜；找不到改善就返回 0° 原图。
    """
    base = ink_mask(gray)
    best_score = long_line_count(base, 'h')
    best = (0.0, gray)
    a = lo
    while a <= hi + 1e-6:
        if abs(a) > 1e-6:
            r = gray.rotate(a, resample=Image.BILINEAR, expand=False, fillcolor=255)
            s = long_line_count(ink_mask(r), 'h')
            if s > best_score:
                best_score, best = s, (a, r)
        a += step
    return best
