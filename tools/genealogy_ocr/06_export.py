# -*- coding: utf-8 -*-
"""第 8 步：段落 → 结构化记录（供理事会复核，不直接写正式表）。

产物是**待审草案**，不是可直接入谱的数据：OCR 错字、字段归属、
一人多名都还没定案。按项目既定口径，导入走 source_type=1、
living_status 显式 2、关系走 family_union、落成待审修订。

输出 records.json / records.csv（Excel 可直接开）/ extract_report.json。
"""
from __future__ import annotations

import csv, json, re, time
from collections import Counter

import config as C

REIGN = (r'(洪武|建文|永樂|永乐|洪熙|宣德|正統|正统|景泰|天順|天顺|成化|弘治|宏治|'
         r'正德|嘉靖|隆慶|隆庆|萬曆|萬歷|万历|泰昌|天啟|天启|崇禎|崇祯|順治|顺治|'
         r'康熙|雍正|乾隆|嘉慶|嘉庆|道光|咸豐|咸丰|同治|光緒|光绪|宣統|宣统)')
GAN = r'[甲乙丙丁戊己庚辛壬癸]'
ZHI = r'[子丑寅卯辰巳午未申酉戌亥]'
NUM = r'[一二三四五六七八九十百]'
MON = r'[正二三四五六七八九十冬臘腊]'
SHI = r'[子丑寅卯辰巳午未申酉戌亥]'

YEAR = re.compile(REIGN + GAN + ZHI + r'年')
DATE = re.compile(MON + r'月(?:初|廿|卅)?' + NUM + r'{1,3}日')
SHICHEN = re.compile(SHI + r'時')
GEN = re.compile(r'第(' + NUM + r'{1,4})世')
RANK = re.compile(r'([一-龥]{1,2}[一二三四五六七八九十])公')
FATHER = re.compile(r'([一-龥]{1,2}[一二三四五六七八九十])公(長子|次子|三子|四子|五子|長予|次予)')
SPOUSE = re.compile(r'(繼娶|继娶|糙娶|娶)([一-龥]{1,2})氏')
CHILD = re.compile(r'生子(' + NUM + r'?)')
DAUGHTER = re.compile(r'生女(' + NUM + r'?)')
APPELL = re.compile(r'(諱|讳|薛|字|各|名|號|号)([一-龥]{1,4})')
BURY = re.compile(r'(塟|葬|塟於|葬於|合塟|合葬)([一-龥]{0,12}?)(?=生於|終於|卒於|娶|生子|生女|$)')
OFFICE = re.compile(r'(知府|知縣|知县|縣城|县城|順天|顺天|授信州|遂家|遷|迁|上饒|上饶)')
UNKNOWN = re.compile(r'(失考|未詳|未详|葬間俱|生葬間俱)')
TITLE = re.compile(r'(歷代源流總世系|歷代源流|源流總世系|卷之)')

CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8,
          '九': 9, '十': 10}


def n2i(s):
    if not s:
        return None
    if s == '十':
        return 10
    if '十' in s:
        a, _, b = s.partition('十')
        return CN_NUM.get(a, 1) * 10 + CN_NUM.get(b, 0)
    return CN_NUM.get(s) if len(s) == 1 else None


def first(pat, text, group=0):
    m = pat.search(text)
    return m.group(group) if m else ''


def extract(para, col_idx):
    t = para['text']
    cols = [col_idx[c] for c in para['columns'] if c in col_idx]
    conf = round(sum(c['conf'] for c in cols) / max(1, len(cols)), 3)
    flags = sorted({f.split(':')[0] for c in cols for f in c.get('flags', [])})

    page_type = '源流總世系' if (GEN.search(t) or TITLE.search(t)) and not SPOUSE.search(t) \
        else '齒錄'
    gen = GEN.search(t)
    ranks = list(dict.fromkeys(RANK.findall(t)))
    father = FATHER.search(t)
    spouses = [dict(seq=i + 1, kind=m.group(1), surname=m.group(2))
               for i, m in enumerate(SPOUSE.finditer(t))]
    child = CHILD.search(t)
    daughter = DAUGHTER.search(t)
    years = [m.group(0) for m in YEAR.finditer(t)]
    dates = [m.group(0) for m in DATE.finditer(t)]
    shichen = [m.group(0) for m in SHICHEN.finditer(t)]
    bury = BURY.search(t)
    apps = {}
    for k, v in APPELL.findall(t):
        key = {'薛': '諱(疑)', '各': '名(疑)'}.get(k, k)
        apps.setdefault(key, v)

    rec = dict(
        id=para['id'], pageType=page_type,
        generation=n2i(gen.group(1)) if gen else None,
        rankNames=ranks[:8],
        mainRank=ranks[0] if ranks else '',
        fatherRank=father.group(1) if father else '',
        birthOrder=father.group(2) if father else '',
        appellations=apps,
        name=apps.get('諱') or apps.get('諱(疑)') or '',
        birthYear=years[0] if years else '',
        deathYear=years[1] if len(years) > 1 else '',
        birthDate=dates[0] if dates else '',
        deathDate=dates[1] if len(dates) > 1 else '',
        shichen=shichen[:3],
        spouses=spouses,
        childCount=n2i(child.group(1)) if child else None,
        daughterCount=n2i(daughter.group(1)) if daughter else None,
        burial=(bury.group(0) if bury else ''),
        unknownNote=first(UNKNOWN, t),
        officeMigration='、'.join(dict.fromkeys(OFFICE.findall(t))),
        avgConf=conf, flags=flags,
        multiPerson=len(ranks) > 3 or len(years) > 3,
        charCount=len(t),
        pages=para['pages'],
        text=t,
        provenance=para['sources'],
    )
    rec['review'] = bool(flags) or conf < 0.65 or rec['multiPerson'] or not rec['name']
    return rec


CSV_COLS = [
    ('id', '段落ID'), ('pageType', '页型'), ('generation', '世代'),
    ('mainRank', '排行名'), ('fatherRank', '父排行名'), ('birthOrder', '行第'),
    ('name', '諱'), ('birthYear', '生年'), ('birthDate', '生月日'),
    ('deathYear', '卒年'), ('deathDate', '卒月日'),
    ('spouseText', '配偶'), ('childCount', '生子数'), ('daughterCount', '生女数'),
    ('burial', '葬地'), ('unknownNote', '失考'), ('officeMigration', '官历迁徙'),
    ('avgConf', '平均置信'), ('review', '待复核'), ('pagesText', '来源页'), ('text', '原文'),
]


def main():
    t0 = time.time()
    joins = json.loads((C.OUT / 'joins.json').read_text(encoding='utf-8'))
    ocr = json.loads((C.OUT / 'ocr.json').read_text(encoding='utf-8'))['columns']
    col_idx = {c['id']: c for c in ocr}

    records = [extract(p, col_idx) for p in joins['paragraphs']]
    for r in records:
        r['spouseText'] = '、'.join(
            f"{s['seq']}.{s['kind']}{s['surname']}氏" for s in r['spouses'])
        r['pagesText'] = ' '.join(x[21:27] for x in r['pages'])

    (C.OUT / 'records.json').write_text(
        json.dumps(dict(generated=time.strftime('%Y-%m-%d %H:%M:%S'),
                        total=len(records), records=records),
                   ensure_ascii=False), encoding='utf-8')

    with (C.OUT / 'records.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([label for _, label in CSV_COLS])
        for r in records:
            w.writerow([r.get(k, '') if not isinstance(r.get(k), (list, dict))
                        else json.dumps(r.get(k), ensure_ascii=False)
                        for k, _ in CSV_COLS])

    cover = {k: sum(1 for r in records if r.get(k)) for k in
             ('generation', 'mainRank', 'name', 'birthYear', 'birthDate',
              'spouses', 'childCount', 'burial', 'fatherRank')}
    report = dict(
        generated=time.strftime('%Y-%m-%d %H:%M:%S'),
        paragraphs=len(records),
        pageTypes=dict(Counter(r['pageType'] for r in records)),
        fieldCoverage=cover,
        fieldCoveragePct={k: round(100 * v / max(1, len(records)), 1)
                          for k, v in cover.items()},
        review=sum(1 for r in records if r['review']),
        multiPerson=sum(1 for r in records if r['multiPerson']),
        avgConf=round(sum(r['avgConf'] for r in records) / max(1, len(records)), 3),
        elapsed=round(time.time() - t0, 1),
    )
    (C.OUT / 'extract_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')

    print('=== 结构化导出 ===')
    print('段落', len(records), report['pageTypes'], '待复核', report['review'])
    print('字段覆盖率:', report['fieldCoveragePct'])
    print('产物:', C.OUT / 'records.json', C.OUT / 'records.csv',
          C.OUT / 'extract_report.json')


if __name__ == '__main__':
    main()
