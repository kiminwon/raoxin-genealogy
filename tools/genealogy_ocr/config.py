# -*- coding: utf-8 -*-
"""老谱 OCR 流水线：路径与可调参数。

源图只读，任何产物都写到 OUT 下；小程序项目代码不受影响。
"""
from pathlib import Path

# 原始照片（只读）
SRC = Path(r'C:\Users\mayn\Desktop\王启明\家谱\源流')

# 全部中间产物与交付物
OUT = Path(r'C:\Users\mayn\Desktop\王启明\家谱\_ocr_out')
NORMALIZED = OUT / 'normalized'     # 转正后的工作图（按需生成）
COLUMNS = OUT / 'columns'           # 逐列裁片
REVIEW = OUT / 'review'             # 复核台静态资源

# 分析用缩略图宽度（方向/切分都在这档上算，够用且快）
ANALYZE_W = 640
# 交付用工作图宽度（拉正、统一尺度后）
WORK_W = 1400

# 版面判定
INK_DELTA = 40          # 局部背景与灰度差 > 此值算墨迹
LINE_BLOCK = 20         # 长线检测的横向分块宽度
LINE_MIN_DENSITY = 0.55 # 一整行每个分块都达到此密度才算贯穿实线
EDGE_FRAC = 0.05        # 左右边缘条带占比（找版心栏）
PARTIAL_COVER = 0.62    # 墨迹包围盒宽度占比低于此值视为半页
PARTIAL_BIAS = 0.12     # 包围盒中心偏离页面中线超过此值才判左/右半

# 去重（转正后感知哈希汉明距离）
DUP_HAMMING = 7

# 竖排 OCR：单字切块后重排成横行的目标高度
GLYPH_ROW_H = 64
# 单列最多切多少字（超出报警，避免印章/栏线被当成字）
MAX_GLYPHS = 40

# 接续打分权重
W_LAYOUT = 0.35         # 横线 y 对齐
W_FIELD = 0.40          # 未闭合字段语法
W_ANCHOR = 0.25         # 排行名 + 世代实体锚
JOIN_AUTO = 0.72        # 高于此分自动接上
JOIN_REVIEW = 0.35      # 低于此分自动断开；中间进人工队列

# 顶部/底部对齐判定：正确朝向下，列首贴横线更整齐
ALIGN_WEIGHT = 0.6
