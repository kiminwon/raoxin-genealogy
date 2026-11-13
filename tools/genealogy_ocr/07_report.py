# -*- coding: utf-8 -*-
"""第 9 步：汇总全链路统计，出一份 Markdown 报告。

只读各步产物，不重跑任何计算。报告落在 _ocr_out/REPORT.md。
"""
from __future__ import annotations

import json, time
from collections import Counter
from pathlib import Path

import config as C


def load(name):
    p = C.OUT / name
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}


def main():
    pages = load('pages.json')
    resolved = load('pages_resolved.json') or pages
    seg = load('segments.json')
    ocr = load('ocr_report.json')
    joins = load('joins.json')
    ext = load('extract_report.json')
    cols = load('ocr.json').get('columns', [])

    rot = Counter(p['rotate'] for p in resolved.get('pages', []))
    dup = sum(1 for p in resolved.get('pages', []) if p.get('dupOf'))
    partial = Counter(str(p.get('partial')) for p in resolved.get('pages', []))
    flipped = resolved.get('directionResolvedByPageOcr', {}).get('flipped', [])
    rowsrc = Counter(p.get('rowSource') for p in seg.get('pages', []))
    confs = [c['conf'] for c in cols if c.get('text')]

    L = []
    L.append('# 老谱识别与结构化 · 全链路报告')
    L.append('')
    L.append(f'生成时间：{time.strftime("%Y-%m-%d %H:%M:%S")}　|　'
             f'源图目录：`{C.SRC}`　|　产物：`{C.OUT}`')
    L.append('')
    L.append('> 本报告是**工程实测**，不是识别准确率鉴定。字符准确率需要人工标注的'
             '金标准才能算（见文末「下一步」）；文中用的是置信度、字段可解析率等代理指标。')
    L.append('')
    L.append('## 1. 输入与归一化')
    L.append('')
    L.append('| 项目 | 数值 |')
    L.append('|---|---|')
    L.append(f'| 原始照片 | {len(resolved.get("pages", []))} 张 |')
    L.append(f'| 方向判定 | 0° {rot.get(0,0)} · 90° {rot.get(90,0)} · '
             f'180° {rot.get(180,0)} · 270° {rot.get(270,0)} |')
    L.append(f'| 整页 OCR 复核翻向 | {len(flipped)} 张 |')
    L.append(f'| 重复拍摄去重 | {dup} 张 |')
    L.append(f'| 半页判定 | {dict(partial)} |')
    L.append(f'| 半页自动配对 | {len(pages.get("pairs", []))} 对 |')
    L.append('')
    L.append('## 2. 识别')
    L.append('')
    L.append('| 项目 | 数值 |')
    L.append('|---|---|')
    L.append(f'| 已处理页 | {seg.get("totalPages", 0)} |')
    L.append(f'| 文本列（OCR 框） | {seg.get("totalColumns", 0)} |')
    L.append(f'| 平均置信 | {ocr.get("avgConf", 0):.3f} |')
    L.append(f'| 低置信列（<0.60） | {ocr.get("lowConf", 0)} '
             f'（{100*ocr.get("lowConf",0)/max(1,ocr.get("withText",1)):.1f}%） |')
    L.append(f'| 字段可解析率 | {100*ocr.get("parseRate",0):.1f}% '
             f'（{ocr.get("parsedCols",0)}/{ocr.get("withText",0)} 列套进谱牒字段模板） |')
    L.append(f'| 词典自动纠正 | {ocr.get("fixed", 0)} 列 |')
    L.append(f'| 世代分块依据 | {dict(rowsrc)} |')
    L.append('')
    L.append('规则命中 TOP：')
    L.append('')
    for k, v in list(ocr.get('ruleHits', {}).items())[:12]:
        L.append(f'- `{k}` × {v}')
    L.append('')
    L.append('## 3. 接续（把被照片切断的内容拼回来）')
    L.append('')
    L.append('| 项目 | 数值 |')
    L.append('|---|---|')
    L.append(f'| 人物块（世代行） | {joins.get("totalBlocks", 0)} |')
    L.append(f'| 跨照片边界 | {joins.get("crossPhotoBoundaries", 0)} |')
    L.append(f'| 决策分布 | {joins.get("crossDecisions", {})} |')
    L.append(f'| 建议抽查（证据弱） | '
             f'{sum(1 for b in joins.get("boundaries", []) if b.get("weak"))} |')
    L.append(f'| 合并后完整段落 | {joins.get("totalParagraphs", 0)} |')
    L.append('')
    L.append('判定依据 TOP：')
    L.append('')
    for k, v in list(joins.get('reasons', {}).items())[:8]:
        L.append(f'- {k} × {v}')
    L.append('')
    L.append('## 4. 结构化草案')
    L.append('')
    L.append('| 字段 | 覆盖段落 | 覆盖率 |')
    L.append('|---|---|---|')
    for k, v in ext.get('fieldCoverage', {}).items():
        L.append(f'| {k} | {v} | {ext.get("fieldCoveragePct", {}).get(k, 0)}% |')
    L.append('')
    L.append(f'待复核段落 {ext.get("review", 0)} / {ext.get("paragraphs", 0)}，'
             f'疑似一段含多人 {ext.get("multiPerson", 0)}。')
    L.append('')
    L.append('## 5. 交付物')
    L.append('')
    L.append('| 文件 | 用途 |')
    L.append('|---|---|')
    L.append('| `review/index.html` | PC 复核台：看边界两侧原图裁片，J 接上 / K 断开 |')
    L.append('| `records.csv` | 结构化草案，Excel 直接打开，理事会逐条核 |')
    L.append('| `records.json` | 同上，带 provenance（页码 + 坐标） |')
    L.append('| `joins.json` | 边界决策 + 合并后完整段落 |')
    L.append('| `ocr.json` | 每列的原文 / 纠正文 / 置信 / 复核标记 |')
    L.append('| `segments.json` | 版面归属（页 / 行 / 列 / 归一化坐标） |')
    L.append('')
    L.append('## 6. 已知局限与下一步')
    L.append('')
    L.append('1. **没有金标准，就没有字符准确率。** 建议人工标 10 页（含源流页与齿录页各半），'
             '再算 CER；没有这个数，换模型、调 prompt 都无法判断是变好还是变坏。')
    L.append('2. **雕版异体字是真短板**。PP-OCRv4 是现代印刷体训练域，换成 v5 也救不了'
             '「夀 / 塤 / 廿 / 卅 / 巽山乾向」。词典已吃掉年号干支、時辰、廿日、長子等高频套语，'
             '剩下的要靠复核确认的「裁片 ↔ 正确字」攒标注、微调老谱专用识别头。')
    L.append('3. **字段归属仍靠位置 + 词类**，一段含多人时（`multiPerson`）需人工拆。'
             '源流页与齿录页的排行名对齐（鏡一公 ↔ 鏡一）还没做实体解析。')
    L.append('4. **草案不能直接入谱**：导入走 `source_type=1`、`living_status` 显式 2、'
             '关系走 `family_union`、落成待审修订，理事会确认后才生效。')
    L.append('')
    L.append('## 7. 复核闭环')
    L.append('')
    L.append('```')
    L.append('双击 review/index.html → 按 J / K 判边界 → 导出 join_decisions.json')
    L.append('→ python 06_apply.py <导出的文件>   # 并回并重切段落')
    L.append('→ python 06_export.py               # 重出 records.csv')
    L.append('→ python 07_report.py               # 重出本报告')
    L.append('```')
    L.append('')

    out = C.OUT / 'REPORT.md'
    out.write_text('\n'.join(L), encoding='utf-8')
    print('=== 报告 ===')
    print(out)
    print('报告行数', len(L), '| 合并段落', len(joins.get('paragraphs', [])))


if __name__ == '__main__':
    main()
