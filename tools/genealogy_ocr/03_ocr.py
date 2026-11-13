# -*- coding: utf-8 -*-
"""第 4 步：词典/规则纠错 + 复核标记（2026-11-14 重写）。

原设计是「逐列 OCR」，现在 02 已整页识别完，这一步改为**后处理**：
PP-OCR 在雕版楷书上错字很规整，几乎都在固定套语里——
「成化两戌年 / 嘉靖庚予年 / 宏治戊申年 / 壽一公長予 / 娶哭氏 /
 十月社九日 / 寅畴 / 四月九H」。这些不是随机噪声，是有限词表的形近误识，
用规则修比换模型便宜得多，也更容易回溯。

三条原则：
  1. **只在上下文闭合时自动改**，其余只给 suggest，不擅动原字；
  2. 每条改动都留 fixes 记录（from/to/rule），复核时能看到原字；
  3. 「失考 / 未詳」是答案不是噪声，不进纠错队列。

输出 ocr.json（兼容 04_join / 05_review 的 columns schema）
     + ocr_report.json（可结构化率、待复核分布）。
"""
from __future__ import annotations

import json, re, time
from collections import Counter

import config as C

# ---------- 词表 ----------
REIGNS = ('洪武|建文|永樂|永乐|洪熙|宣德|正統|正统|景泰|天順|天顺|成化|宏治|弘治|'
          '正德|嘉靖|隆慶|隆庆|萬歷|万历|萬曆|萬懋|泰昌|天啟|天启|崇禎|崇祯|'
          '順治|顺治|康熙|雍正|乾隆|嘉慶|嘉庆|道光|咸豐|咸丰|同治|光緒|光绪|宣統|宣统')
GAN = '甲乙丙丁戊己庚辛壬癸'
ZHI = '子丑寅卯辰巳午未申酉戌亥'
MONTH = '正二三四五六七八九十冬臘腊'
DAY = '初一二三四五六七八九十廿卅'
SHICHEN = '子丑寅卯辰巳午未申酉戌亥'
FIELDS = ('生子|生女|娶|繼娶|继娶|糙娶|諱|讳|薛|字|各|名|號|号|塟|葬|塟於|葬於|'
          '終於|终于|卒於|卒于|生於|生于|失考|未詳|未详|合塟|合葬|適|适|出繼|出继|'
          '無出|无出|時|时|年|月|日|氏|公|長子|長予|世')
RANK = r'[一-龥]{1,2}[一二三四五六七八九十](?=公|氏|\s|$)'
GEN = r'第?([一二三四五六七八九十百]{1,4})世'


def valid_ganzhi(s):
    """是否合法干支（天干地支各占其位）。"""
    return s and s[0] in GAN and s[1] in ZHI and (GAN.index(s[0]) % 2) == (ZHI.index(s[1]) % 2)


# ---------- 自动纠错规则（模式, 替换, 规则名） ----------
HARD = [
    (re.compile(r'長予'), '長子', '長子'),
    (re.compile(r'(生子|生女|生予)([一二三四五六七八九十])'), None, '生子数'),  # 特判见下
    (re.compile(r'生予'), '生子', '生子'),
    (re.compile(r'宏治'), '弘治', '年号'),
    (re.compile(r'萬懋|萬歷|萬历'), '萬曆', '年号'),
    (re.compile(r'糙娶|継娶'), '繼娶', '繼娶'),
    (re.compile(r'([%s])予(?=年)' % GAN), r'\1子', '干支·子'),
    (re.compile(r'([%s])予(?=年|日|時|时)' % GAN), r'\1子', '干支·子'),
    (re.compile(r'王([申戌辰午子寅])'), r'壬\1', '干支·壬'),
    (re.compile(r'玉([寅申辰戌子午])'), r'壬\1', '干支·壬'),
    (re.compile(r'两([戌寅申辰子午])'), r'丙\1', '干支·丙'),
    (re.compile(r'([正二三四五六七八九十冬臘腊])月社([一二三四五六七八九])日'), r'\1月廿\2日', '廿日'),
    (re.compile(r'([正二三四五六七八九十冬臘腊])月北([一二三四五六七八九])日'), r'\1月廿\2日', '廿日'),
    (re.compile(r'([正二三四五六七八九十冬臘腊])月杜([一二三四五六七八九])日'), r'\1月廿\2日', '廿日'),
    (re.compile(r'入月(初[一二三四五六七八九十])日'), r'八月\1日', '八月'),
    (re.compile(r'([一二三四五六七八九十])月([0-9１-９])([十百]?[一二三四五六七八九])日'),
     None, 'digit'),  # 特判见下
    (re.compile(r'([一二三四五六七八九十]{1,3})H'), r'\1日', '日'),
    (re.compile(r'([%s])畴' % SHICHEN), r'\1時', '時辰'),
    (re.compile(r'([%s])脖' % SHICHEN), r'\1時', '時辰'),
    (re.compile(r'娶哭氏'), '娶吳氏', '姓氏·吳'),
    (re.compile(r'垒於|仝於|塟於'), '生於', '生於'),
    (re.compile(r'愿代源流魏世采|愿代源流總世系'), '歷代源流總世系', '卷首标题'),
    (re.compile(r'年年$'), '年', '叠字'),
]

# 只给建议、不自动改（异体/形近，自动改风险大）
SOFT = [
    (re.compile(r'^薛'), '諱', '字段头「諱」常被读成「薛」'),
    (re.compile(r'^各'), '名', '字段头「名」常被读成「各」'),
    (re.compile(r'^静|^麟'), '字', '字段头「字」疑似误识'),
    (re.compile(r'娶鄞氏|娶繁氏|娶郑氏'), '娶某氏', '姓氏待核（原谱字形）'),
    (re.compile(r'晶|塤|填|邦橘|志標'), None, '人名/地名生僻字，需对原图'),
]

# 只认明确的拉丁/数字/半角标点混入；不要写 [╪-￿] 这类大范围，
# 它会把 U+4E00–U+9FFF 的汉字全包进去（第一版踩过：476 列全部误报）。
GARBLE = re.compile(r'[A-Za-z0-9０-９①-⑩①②③④⑤⑥⑦⑧⑨⑩]')
OPEN_TAIL = re.compile(r'(生子|生女|娶|繼娶|继娶|諱|讳|字|塟|葬|終於|终于|生於|生于|失考)$')


def fix_text(raw):
    """返回 (纠正后文本, fixes, flags, suggests)。"""
    text, fixes, flags = raw, [], []
    for pat, rep, name in HARD:
        if rep is None:
            continue
        hit = pat.search(text)
        new = pat.sub(rep, text)
        if new != text:
            fixes.append(dict(rule=name, from_=hit.group(0) if hit else '', to=new[:20]))
            text = new
    # 数字混入日期：十月1十九日 → 十月十九日
    m = re.search(r'([一二三四五六七八九十]{1,3})月[0-9１-９]([十百]?[一二三四五六七八九])日', text)
    if m:
        old = m.group(0)
        new = m.group(1) + '月' + m.group(2) + '日'
        text = text.replace(old, new)
        fixes.append(dict(rule='digit', from_=old, to=new))

    suggests = []
    for pat, rep, why in SOFT:
        if pat.search(raw):
            suggests.append(dict(why=why, to=rep or ''))

    # 干支合法性：年号后跟两字，若非合法干支，标「待核」而不是猜
    for m in re.finditer(r'(%s)\s*([一-龥])([一-龥])\s*年' % REIGNS, text):
        gz = m.group(2) + m.group(3)
        if not valid_ganzhi(gz):
            flags.append('干支待核:' + gz)
    if GARBLE.search(text):
        flags.append('混入非汉字')
    if OPEN_TAIL.search(text):
        flags.append('字段未闭合')
    if len(text) <= 2:
        flags.append('短文本')
    return text, fixes, flags, suggests


TEMPLATES = [
    re.compile(r'(%s)\s*[%s][%s]\s*年' % (REIGNS, GAN, ZHI)),      # 年号干支纪年
    re.compile(r'([%s])月(?:初|廿|卅|[%s]){1,3}日' % (MONTH, DAY)),  # 日期
    re.compile(GEN),                                                # 世次印章
    re.compile(r'第[一二三四五六七八九十百]{1,4}世'),
    re.compile(r'生子[一二三四五六七八九十]|生女[一二三四五六七八九十]|生子|生女'),
    re.compile(r'(娶|繼娶|继娶)[一-龥]{1,2}氏'),
    re.compile(r'[一-龥]{1,3}公長子|[一-龥]{1,3}公|%s' % RANK),
    re.compile(r'(生於|生于|終於|终于|卒於|塟於|葬於|塟|葬|失考|未詳)'),
    re.compile(r'[%s]時|[%s]时' % (SHICHEN, SHICHEN)),
    re.compile(r'(諱|讳|字|號|号|名)[一-龥]{1,4}'),
]


def structure_rate(texts):
    """字段可解析率：能套进至少一条谱牒字段模板的列占比。

    没有金标准就算不了字符准确率。这个代理指标回答的是更实际的问题：
    「识别出来的列里，有多少能直接进结构化解析器」，剩下的才需要人工。
    """
    if not texts:
        return 0.0, 0, 0
    hit = sum(1 for t in texts if any(p.search(t) for p in TEMPLATES))
    han = sum(1 for t in texts for c in t if '\u4e00' <= c <= '\u9fff')
    return round(hit / len(texts), 3), han, hit


def main():
    t0 = time.time()
    seg = json.loads((C.OUT / 'segments.json').read_text(encoding='utf-8'))
    out = []
    for c in seg['columns']:
        raw = c.get('raw') or c.get('text') or ''
        text, fixes, flags, suggests = fix_text(raw)
        if c['conf'] < 0.60:
            flags.append('低置信')
        out.append(dict(c, raw=raw, text=text, fixes=fixes,
                        flags=flags, suggests=suggests,
                        needReview=bool(flags) or bool(fixes)))

    texts = [x['text'] for x in out if x['text']]
    rate, total_han, parsed = structure_rate(texts)
    report = dict(
        generated=time.strftime('%Y-%m-%d %H:%M:%S'),
        pages=seg['totalPages'], columns=len(out),
        withText=len(texts),
        hanChars=total_han,
        parseRate=rate, parsedCols=parsed,
        avgConf=round(sum(x['conf'] for x in out if x['text']) /
                      max(1, sum(1 for x in out if x['text'])), 3),
        lowConf=sum(1 for x in out if x['text'] and x['conf'] < 0.60),
        fixed=sum(1 for x in out if x['fixes']),
        flagged=sum(1 for x in out if x['needReview']),
        flagKinds=Counter(f.split(':')[0] for x in out for f in x['flags']),
        ruleHits=Counter(f['rule'] for x in out for f in x['fixes']),
    )
    (C.OUT / 'ocr.json').write_text(json.dumps(
        dict(generated=report['generated'], total=len(out), columns=out),
        ensure_ascii=False), encoding='utf-8')
    (C.OUT / 'ocr_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')

    print('=== 词典纠错 ===')
    print('列', len(out), '有文本', report['withText'], '汉字', total_han)
    print('平均置信 %.3f  低置信 %d  自动纠正 %d  待复核 %d' %
          (report['avgConf'], report['lowConf'], report['fixed'], report['flagged']))
    print('字段可解析率 %.1f%%（%d/%d 列套进谱牒字段模板）' %
          (rate * 100, parsed, len(texts)))
    print('规则命中:', dict(report['ruleHits']))
    print('复核原因:', dict(report['flagKinds']))
    print('纠正样例:')
    for x in [x for x in out if x['fixes']][:12]:
        print('  %-18s → %-18s [%s]' % (x['raw'], x['text'],
              ','.join(f['rule'] for f in x['fixes'])))
    print('产物:', C.OUT / 'ocr.json', C.OUT / 'ocr_report.json')


if __name__ == '__main__':
    main()
