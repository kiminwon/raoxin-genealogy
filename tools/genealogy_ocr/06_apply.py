# -*- coding: utf-8 -*-
"""第 7 步：把复核台的人工决定并回 joins.json，重切段落。

复核台导出 join_decisions.json 后运行：
    python 06_apply.py [join_decisions.json 路径]

找不到文件时按几个常见位置自动找；找到后原地更新 joins.json
（先备份 joins.before-review.json），段落按最终 decision 重算。
"""
from __future__ import annotations

import json, shutil, sys, time
from pathlib import Path

import config as C

CANDIDATES = [
    C.REVIEW / 'join_decisions.json',
    C.OUT / 'join_decisions.json',
    Path.home() / 'Downloads' / 'join_decisions.json',
    Path.home() / 'Desktop' / 'join_decisions.json',
]


def find_file():
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        return p if p.exists() else None
    return next((p for p in CANDIDATES if p.exists()), None)


def main():
    t0 = time.time()
    fp = find_file()
    if not fp:
        print('未找到 join_decisions.json。先在复核台点「导出决定」，'
              '把文件放到 review/ 或 Downloads/ 再跑。')
        return
    decisions = json.loads(fp.read_text(encoding='utf-8'))
    by_index = {int(d['index']): d['decision'] for d in decisions}

    jf = C.OUT / 'joins.json'
    joins = json.loads(jf.read_text(encoding='utf-8'))
    if not (C.OUT / 'joins.before-review.json').exists():
        shutil.copy2(jf, C.OUT / 'joins.before-review.json')

    changed = 0
    for b in joins['boundaries']:
        d = by_index.get(b['index'])
        if d in ('auto', 'break') and d != b['decision']:
            b['decision'] = d
            b['reason'] = '人工复核改判'
            b['reviewed'] = True
            changed += 1

    # 按最终 decision 重切段落：列摊平后按 (页,行) 还原人物块再切开，
    # 不依赖 04 的内存状态，随时可重跑。
    ocr = json.loads((C.OUT / 'ocr.json').read_text(encoding='utf-8'))['columns']
    by_id = {c['id']: c for c in ocr}
    flat = []
    for p in joins['paragraphs']:
        flat.extend(p['columns'])
    by_block = {}
    for cid in flat:
        c = by_id.get(cid)
        if c:
            by_block.setdefault((c['pageIndex'], c['row']), []).append(c)
    blocks = []
    for k in sorted(by_block):
        blocks.append(sorted(by_block[k], key=lambda x: x['col']))

    cross_map = {b['index']: b['decision'] for b in joins['boundaries']}
    # 04 的 boundary index 是「块序号」，这里保持一致
    paragraphs, cur = [], []

    def flush():
        if not cur:
            return
        paragraphs.append(dict(
            id=f'p{len(paragraphs):04d}',
            text=''.join(x['text'] for x in cur if x.get('text')),
            columns=[x['id'] for x in cur],
            sources=[dict(page=x['page'], row=x['row'], col=x['col'],
                          x1=x['x1'], y1=x['y1'], x2=x['x2'], y2=x['y2'])
                     for x in cur],
            pages=sorted(set(x['page'] for x in cur)),
        ))
        cur.clear()

    for i, blk in enumerate(blocks):
        cur.extend(blk)
        if cross_map.get(i) == 'break':
            flush()
    flush()

    joins['paragraphs'] = paragraphs
    joins['totalParagraphs'] = len(paragraphs)
    joins['reviewApplied'] = dict(source=str(fp), decisions=len(by_index),
                                  changed=changed,
                                  generated=time.strftime('%Y-%m-%d %H:%M:%S'))
    jf.write_text(json.dumps(joins, ensure_ascii=False, indent=1), encoding='utf-8')

    print('=== 并回人工决定 ===')
    print('决定', len(by_index), '改判', changed, '段落', len(paragraphs),
          '耗时 %.1fs' % (time.time() - t0))
    print('备份:', C.OUT / 'joins.before-review.json')
    print('更新:', jf)


if __name__ == '__main__':
    main()
