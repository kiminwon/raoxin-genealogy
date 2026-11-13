# -*- coding: utf-8 -*-
"""按 pages.json 的转正结果生成抽样联系表，供人工扫一眼方向判得对不对。

用法：python view_contact.py [抽样步长] [输出名]
"""
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw

import config as C
import ocrlib as L

step = int(sys.argv[1]) if len(sys.argv) > 1 else 18
out = C.OUT / (sys.argv[2] if len(sys.argv) > 2 else 'contact_sample.jpg')
d = json.loads((C.OUT / 'pages.json').read_text(encoding='utf-8'))
recs = d['pages'][::step][:24]

cw, ch, cols = 210, 300, 6
rows = (len(recs) + cols - 1) // cols
sheet = Image.new('RGB', (cw * cols, rows * (ch + 20)), 'white')
draw = ImageDraw.Draw(sheet)
for i, r in enumerate(recs):
    g = L.rotate_deg(L.load_gray(C.SRC / r['name'], 400), r['rotate']).convert('RGB')
    g.thumbnail((cw - 8, ch - 6))
    x, y = (i % cols) * cw, (i // cols) * (ch + 20)
    sheet.paste(g, (x + 4, y + 18))
    draw.text((x + 4, y + 3),
              f"{r['name'][21:27]} r{r['rotate']:>3} {r['cls']} c{r['conf']:.2f}",
              fill='red' if r['conf'] < 0.6 else 'black')
sheet.save(out, quality=82)
print(out, sheet.size, len(recs))
