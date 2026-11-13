# -*- coding: utf-8 -*-
"""第 5 步：自动接续 —— 把被照片切断的连续内容拼回完整段落。

⚠️ 2026-11-14 重写。第一版沿用了「像素拼接」的思路：默认断开，
靠三信号（横线对齐 / 未闭合字段 / 实体锚）证明「该接」。实测 19 个边界
18 个判 break，但证据全是「A末: 麟亮興生於 ⟂ B首: 正德甲戌年」这种
明显该接的连续字段。两个错：

  1. 先验反了。这批是**连续拍摄、每张切在人物块中间**，默认就该接上；
     只有「换卷 / 换世 / 明确新人物起始且上页已收尾」才该断。
  2. layout 信号无效。跨页的两列 y 本来就不相等（不同人物块），
     用它打分等于给所有边界判 0，把上限压到 0.4，阈值 0.72 永远够不到。

现在的判定是**显式规则优先**，分数只用于复核排序与展示：
  BREAK   卷首标题 / 世次回跳 / 上页字段已闭合且下页是新人起始
  AUTO    未闭合字段 / 跨页拼成字段模板 / 排行名父子链 / 世次连续
  REVIEW  其余（拿不准的进复核台，人工按 J/K 两个键）

输出 joins.json：边界决策 + 合并后的「完整段落」，每段带 provenance。
"""
from __future__ import annotations

import json, re, time
from collections import Counter

import config as C

# ---------- 词类 ----------
REIGN = r'(洪武|建文|永樂|永乐|洪熙|宣德|正統|正统|景泰|天順|天顺|成化|弘治|宏治|正德|嘉靖|隆慶|隆庆|萬曆|萬歷|万历|萬懋|泰昌|天啟|天启|崇禎|崇祯|順治|顺治|康熙|雍正|乾隆|嘉慶|嘉庆|道光|咸豐|咸丰|同治|光緒|光绪|宣統|宣统|崇|萬|蘭|矗)'
GAN = r'[甲乙丙丁戊己庚辛壬癸王玉两]'
ZHI = r'[子丑寅卯辰巳午未申酉戌亥予]'
NUM = r'[一二三四五六七八九十百廿卅]'

# 「后面还有内容」的字段尾巴（未闭合）
OPEN_TAIL = re.compile(
    r'(生於|生于|終於|终于|卒於|卒于|塟於|葬於|塟|葬|娶|繼娶|继娶|糙娶|'
    r'諱|讳|薛|字|各|名|號|号|生子|生女|生予|失考|未詳|未详|'
    r'生子' + NUM + r'|生女' + NUM + r'|第' + NUM + r'{1,4}世|' + REIGN + r')$')
# 完整收尾（字段已闭合）
CLOSED_TAIL = re.compile(
    r'([' + '子丑寅卯辰巳午未申酉戌亥' + r']時|' + NUM + r'{1,3}日|'
    r'[一-龥]{1,2}氏|公|失考|年|向|考)$')
# 新人物块起始
PERSON_HEAD = re.compile(
    r'^([一-龥]{1,3}公(長子|次子|三子|四子|長予|次予)|生子' + NUM +
    r'|第' + NUM + r'{1,4}世|' + NUM + r'{1,3}公)')
# 卷首 / 换卷
VOLUME_HEAD = re.compile(r'(歷代源流|歷代|源流總世系|卷之|卷[一二三四五六七八九十]|目錄|目录|重修|譜序|谱序)')
# 年号干支纪年（B 首常见）
YEAR_HEAD = re.compile(r'^' + REIGN + r'?' + GAN + ZHI + r'年')
DATE_HEAD = re.compile(r'^([' + '正二三四五六七八九十冬臘腊入尤' + r']月|' + NUM + r'{1,3}月|初' + NUM + r')')
# 排行名 + 世代
RANK_RE = re.compile(r'([一-龥]{1,2}[一二三四五六七八九十])公')
GEN_RE = re.compile(r'第?(' + NUM + r'{1,4})世')
HAN_RE = re.compile(r'[\u4e00-\u9fff]')
CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8,
          '九': 9, '十': 10, '百': 100}


def gen_value(s):
    """中文数字转 int（够用到几十世），失败返回 None。"""
    if not s:
        return None
    if s == '十':
        return 10
    if '十' in s:
        a, _, b = s.partition('十')
        return CN_NUM.get(a, 1) * 10 + CN_NUM.get(b, 0)
    if len(s) == 1:
        return CN_NUM.get(s)
    return None


def page_gens(texts):
    out = set()
    for t in texts:
        for m in GEN_RE.finditer(t):
            v = gen_value(m.group(1))
            if v:
                out.add(v)
    return out


def page_ranks(texts):
    out = set()
    for t in texts:
        out.update(RANK_RE.findall(t))
    return out


def cross_template(a, b):
    """跨页拼起来才能组成完整字段 = 硬证据。

    ⚠️ 必须要求模板**跨越拼接点**：B 首自己就完整匹配（如「鼎六公次子」）
    只能说明下页是新人起始，不能证明上页被切断。第一版没做这个检查，
    把「七三 ⟂ 鼎亚公次子」误判成接续。
    """
    ta, tb, j = a[-10:], b[:10], a[-10:] + b[:10]
    pats = [
        r'(生於|生于|終於|终于|卒於|塟|葬)[一-龥]{0,2}' + REIGN + GAN + ZHI + r'年',
        r'(娶|繼娶|继娶|糙娶)[一-龥]{1,2}氏',
        r'(生子|生女|生予)' + NUM + r'?[一-龥]{1,3}(公|氏|、|，)',
        REIGN + GAN + ZHI + r'年',
        r'第' + NUM + r'{1,4}世[一-龥]{1,3}公',
        r'(塟|葬|塟於|葬於)[一-龥]{0,3}(失考|山|向|土名)',
        r'[' + '正二三四五六七八九十冬臘腊' + r']月[初廿卅' + NUM + r']{1,3}日',
    ]
    for i, p in enumerate(pats):
        rx = re.compile(p)
        if rx.search(j) and not rx.search(ta) and not rx.search(tb):
            return 'T' + str(i)
    return ''


def decide(A, B, a_texts, b_texts):
    """返回 (decision, score, parts, reason, evidence)。"""
    a_last = next((t for t in reversed(a_texts) if t.strip()), '')
    b_first = next((t for t in b_texts if t.strip()), '')
    if not a_last or not b_first:
        return 'review', 0.0, dict(field=0, anchor=0, layout=0), '一侧没有可读文本', ''

    tail, head = a_last[-12:], b_first[:12]
    open_tail = bool(OPEN_TAIL.search(a_last))
    closed_tail = bool(CLOSED_TAIL.search(a_last)) and not open_tail
    person_head = bool(PERSON_HEAD.search(b_first))
    year_head = bool(YEAR_HEAD.search(b_first)) or bool(DATE_HEAD.search(b_first))
    volume_head = bool(VOLUME_HEAD.search(''.join(b_texts[:3])))
    tpl = cross_template(a_last, b_first)

    ag, bg = page_gens(a_texts), page_gens(b_texts)
    ar, br = page_ranks(a_texts), page_ranks(b_texts)
    gen_back = bool(ag and bg and max(bg) < min(ag) - 1)
    gen_cont = bool(ag and bg and (ag & bg or max(bg) - max(ag) in (0, 1)))
    chain = bool(br & ar) or bool(re.search(
        r'(' + '|'.join(re.escape(x) for x in ar) + r')公', ''.join(b_texts[:4]))) if ar else False

    # ---- 断点信号（优先级最高）----
    if volume_head:
        return 'break', 0.08, dict(field=0, anchor=0, layout=0), '下页是卷首/标题（换卷）', ''
    if gen_back:
        return 'break', 0.10, dict(field=0, anchor=0, layout=0), \
            f'世次回跳 {sorted(ag)} → {sorted(bg)}（翻页错位或另起一卷）', ''
    if person_head and closed_tail and not tpl and not year_head:
        return 'break', 0.18, dict(field=0.1, anchor=0, layout=0), \
            '上页字段已收尾，下页是另一个人起始', ''

    # ---- 接上信号 ----
    parts = dict(
        field=round(0.95 if open_tail else (0.75 if tpl else 0.25), 2),
        anchor=round(1.0 if chain else (0.7 if gen_cont else 0.0), 2),
        layout=round(1.0 if year_head else 0.35, 2),
    )
    score = round(C.W_FIELD * parts['field'] + C.W_ANCHOR * parts['anchor'] +
                  C.W_LAYOUT * parts['layout'], 3)
    reasons = []
    if tpl:
        reasons.append('跨页拼成字段模板 ' + tpl)
    if open_tail:
        reasons.append('上页末是未闭合字段')
    if year_head:
        reasons.append('下页首是纪年/日期（接上页生卒）')
    if chain:
        reasons.append('排行名父子链')
    if gen_cont:
        reasons.append('世次连续')
    if len(reasons) >= 1 and (open_tail or tpl or chain or (year_head and not person_head)):
        return 'auto', score, parts, ' + '.join(reasons), tpl

    # 拿不准时**保守接上**，不把用户拖进几百个边界。
    # 取舍：接错 = 两个条目粘成一段，后续按字段头还能切开；
    #       断错 = 一个条目被劈成两半，父子归属直接丢。后者代价大得多。
    # 这批又是连续翻拍，页边界天然落在条目中间，先验本就偏「接」。
    return 'auto', score, parts, '证据不足｜连续拍摄先验：保守接上（建议抽查）', tpl


def main():
    t0 = time.time()
    ocr = json.loads((C.OUT / 'ocr.json').read_text(encoding='utf-8'))['columns']

    # 最小单位 = (页, 世代行/人物块)。段落不是「一整页」，而是一个条目；
    # 跨照片被切断的条目才需要接回来。页内相邻块一律是不同条目，硬断。
    by_block = {}
    for c in ocr:
        by_block.setdefault((c['pageIndex'], c['row']), []).append(c)
    blocks = []
    for key in sorted(by_block):
        cols = sorted(by_block[key], key=lambda x: x['col'])
        blocks.append(dict(pageIndex=key[0], row=key[1],
                           page=cols[0]['page'], cols=cols))

    boundaries, cross = [], []
    for i in range(len(blocks) - 1):
        A, B = blocks[i], blocks[i + 1]
        same_page = A['pageIndex'] == B['pageIndex']
        ca = [x for x in A['cols'] if x.get('text')][-6:]
        cb = [x for x in B['cols'] if x.get('text')][:6]
        if same_page:
            # 同一张照片内的两个块 = 本来就分开的两行，不需要人工
            dec, score, parts, reason, tpl = 'break', 0.0, \
                dict(field=0, anchor=0, layout=0), '同一张照片内的相邻行（不同条目）', ''
        else:
            dec, score, parts, reason, tpl = decide(A, B, [x['text'] for x in ca],
                                                    [x['text'] for x in cb])
            cross.append(i)
        weak = (not same_page and
                (score < 0.35 or reason.startswith('证据不足')))
        boundaries.append(dict(
            index=i, aPage=A['page'], bPage=B['page'], samePage=same_page,
            aRow=A['row'], bRow=B['row'], weak=weak,
            score=score, parts=parts, decision=dec, reason=reason,
            evidence=f"A末: {ca[-1]['text'][-14:] if ca else '—'} ⟂ "
                     f"B首: {cb[0]['text'][:14] if cb else '—'}",
            aCols=[x['id'] for x in ca[-3:]], bCols=[x['id'] for x in cb[:3]],
        ))

    # 按决策切段：break 处断开；auto 与 review 暂按接上（review 由复核台改）
    paragraphs, cur = [], []

    def flush():
        if cur:
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
        cur.extend(blk['cols'])
        bd = boundaries[i] if i < len(boundaries) else None
        if bd and bd['decision'] == 'break':
            flush()
    flush()

    cross_dec = Counter(b['decision'] for b in boundaries if not b['samePage'])
    payload = dict(generated=time.strftime('%Y-%m-%d %H:%M:%S'),
                   totalBlocks=len(blocks), totalBoundaries=len(boundaries),
                   crossPhotoBoundaries=len(cross), crossDecisions=dict(cross_dec),
                   totalParagraphs=len(paragraphs),
                   decisions=dict(Counter(b['decision'] for b in boundaries)),
                   reasons=dict(Counter(b['reason'].split(' + ')[0] for b in boundaries
                                        if not b['samePage'])),
                   boundaries=boundaries, paragraphs=paragraphs)
    (C.OUT / 'joins.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')

    print('=== 自动接续 ===')
    print('人物块', len(blocks), '跨照片边界', len(cross), payload['crossDecisions'],
          '段落', len(paragraphs), '耗时 %.0fs' % (time.time() - t0))
    print('判定依据 TOP:', list(payload['reasons'].items())[:6])
    for b in [x for x in boundaries if not x['samePage']][:8]:
        print('  %-6s %.2f  %-24s %s' %
              (b['decision'], b['score'], b['reason'][:24], b['evidence']))
    print('待人工复核:', sum(1 for x in boundaries if x['decision'] == 'review'))
    print('产物:', C.OUT / 'joins.json')


if __name__ == '__main__':
    main()
