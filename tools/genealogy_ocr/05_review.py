# -*- coding: utf-8 -*-
"""第 6 步：生成 PC 复核台（纯静态 HTML，双击即可打开）。

只把「待人工复核」的边界摆出来：左右并排显示边界处的竖列原图裁片 +
OCR 文本 + 三信号明细，键盘 J 接上 / K 断开 / → 下一张。
改完导出 join_decisions.json，再由 06_apply.py 并回 joins.json。

不需要起服务、不联网、不依赖任何前端框架。
"""
from __future__ import annotations

import json, time
from pathlib import Path

from PIL import Image

import config as C
import ocrlib as L

TPL = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>老谱接续复核台</title>
<style>
:root{--paper:#F6F3EA;--ink:#1F2226;--dai:#2B574B;--seal:#A6321E;--line:#D4CCB8;--gold:#C9A961}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC",sans-serif}
header{position:sticky;top:0;z-index:9;background:var(--dai);color:#E9D7A6;padding:14px 22px;box-shadow:0 2px 10px rgba(0,0,0,.18)}
header h1{margin:0;font-size:19px;letter-spacing:3px;font-weight:600}
.bar{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin-top:10px;font-size:13px;color:#DCEAE2}
.bar b{color:#fff;font-size:15px}
.bar select,.bar button,.bar input{font:inherit;padding:4px 10px;border:1px solid var(--gold);background:transparent;color:#E9D7A6;border-radius:4px;cursor:pointer}
.bar button.solid{background:var(--gold);color:var(--dai);border-color:var(--gold);font-weight:700}
.tabs{display:flex;gap:4px;padding:10px 22px 0}
.tabs button{font:inherit;padding:8px 18px;border:1px solid var(--line);background:#fff;color:var(--dai);cursor:pointer;border-radius:6px 6px 0 0}
.tabs button.on{background:#fff;border-bottom-color:#fff;font-weight:700}
main{padding:14px 22px 60px}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px;margin:0 auto 18px;max-width:1200px;box-shadow:0 2px 8px rgba(43,87,75,.06)}
.card.done{opacity:.55}
.chead{display:flex;justify-content:space-between;align-items:center;border-bottom:1px dashed var(--line);padding-bottom:10px;margin-bottom:14px;font-size:13px;color:#666}
.score{font-size:22px;font-weight:700;color:var(--dai)}
.score.low{color:var(--seal)}
.parts{display:flex;gap:12px;font-size:12px;margin-top:6px;color:#777}
.grid{display:grid;grid-template-columns:1fr 130px 1fr;gap:14px;align-items:start}
.side{background:#FCFAF4;border:1px solid var(--line);border-radius:6px;padding:10px;min-height:150px}
.side h4{margin:0 0 8px;font-size:12px;color:var(--dai);letter-spacing:2px}
.cols{display:flex;gap:8px;flex-wrap:wrap}
.col{width:76px;text-align:center}
.col img{width:100%;height:120px;object-fit:contain;background:#fff;border:1px solid #E7E1D2;border-radius:3px}
.col div{font-size:11px;line-height:1.35;margin-top:4px;word-break:break-all;color:#333;min-height:30px}
.mid{text-align:center;font-size:12px;color:#777;padding-top:36px;line-height:2}
.mid .arrow{font-size:26px;color:var(--gold)}
.acts{display:flex;gap:10px;justify-content:center;margin-top:16px}
.acts button{font:inherit;padding:8px 22px;border-radius:5px;cursor:pointer;border:1px solid var(--dai);background:#fff;color:var(--dai)}
.acts button.j{background:var(--dai);color:#E9D7A6}
.acts button.k{border-color:var(--seal);color:var(--seal)}
.acts button:disabled{opacity:.4;cursor:default}
.evi{font-size:12px;color:#8a7a5a;margin-top:10px;text-align:center}
pre{background:#FCFAF4;border:1px solid var(--line);padding:12px;border-radius:6px;white-space:pre-wrap;word-break:break-all;font-size:13px;line-height:1.8}
.p-item{border-left:3px solid var(--gold);padding:8px 12px;margin:10px 0;background:#fff}
.p-item small{color:#888}
.empty{text-align:center;padding:60px;color:#888}
kbd{background:#eee;border:1px solid #ccc;border-radius:3px;padding:1px 6px;font-family:inherit}
</style></head><body>
<header>
  <h1>老谱接续复核台</h1>
  <div class="bar">
    <span>跨照片边界 <b id="nTotal">0</b></span>
    <span>建议抽查 <b id="nReview">0</b></span>
    <span>已决定 <b id="nDone">0</b></span>
    <select id="filter"><option value="weak">建议抽查（证据弱）</option><option value="all">全部边界</option><option value="break">已断开</option><option value="done">已决定</option></select>
    <button onclick="step(1)">← 上一张</button><button onclick="step(-1)">下一张 →</button>
    <button class="solid" onclick="exportDecisions()">导出决定</button>
    <span style="margin-left:auto"><kbd>J</kbd> 接上 · <kbd>K</kbd> 断开 · <kbd>→</kbd> 下一条</span>
  </div>
</header>
<div class="tabs"><button id="tabB" class="on" onclick="tab('b')">边界复核</button><button id="tabP" onclick="tab('p')">完整段落</button></div>
<main>
  <div id="viewB"></div>
  <div id="viewP" style="display:none"></div>
</main>
<script>
const DATA = __DATA__;
const PARAS = __PARAS__;
let decisions = {}, cursor = 0;
try{ decisions = JSON.parse(localStorage.getItem('gene_join_decisions')||'{}'); }catch(e){}

function visible(){ const f=document.getElementById('filter').value;
  return DATA.filter(b=>{
    if(f==='all') return true;
    if(f==='done') return decisions[b.index]!==undefined;
    if(decisions[b.index]!==undefined) return false;
    if(f==='weak') return b.weak;
    return b.decision===f;
  }); }
function refreshStats(){
  document.getElementById('nTotal').textContent=DATA.length;
  document.getElementById('nReview').textContent=DATA.filter(b=>b.weak&&decisions[b.index]===undefined).length;
  document.getElementById('nDone').textContent=Object.keys(decisions).length;
}
function colHtml(id,text){ return '<div class="col"><img src="imgs/'+id+'.png" onerror="this.style.display=\'none\'"><div>'+(text||'—')+'</div></div>'; }
function cardHtml(b){
  const d = decisions[b.index];
  const label = d===undefined ? '待复核' : (d==='auto'?'已接上':'已断开');
  return '<div class="card '+(d!==undefined?'done':'')+'" id="card'+b.index+'">'+
   '<div class="chead"><span>#'+b.index+' &nbsp; '+b.aPage.slice(-10,-4)+' → '+b.bPage.slice(-10,-4)+' &nbsp; <b>'+label+'</b></span>'+
   '<span class="score '+(b.score<0.5?'low':'')+'">'+b.score.toFixed(2)+'</span></div>'+
   '<div class="parts"><span>版式 '+b.parts.layout+'</span><span>字段 '+b.parts.field+'</span><span>实体锚 '+b.parts.anchor+'</span>'+
   '<span style="color:var(--seal)">'+(b.reason||'')+'</span></div>'+
   '<div class="grid"><div class="side"><h4>上一页末尾（右起向左读）</h4><div class="cols">'+
     b.aCols.map((id,i)=>colHtml(id,b.aText[i])).join('')+'</div></div>'+
   '<div class="mid"><div class="arrow">⟷</div>'+b.decision+'<br><span style="font-size:11px">是否同一段落</span></div>'+
   '<div class="side"><h4>下一页开头</h4><div class="cols">'+
     b.bCols.map((id,i)=>colHtml(id,b.bText[i])).join('')+'</div></div></div>'+
   '<div class="evi">'+b.evidence+'</div>'+
   '<div class="acts"><button class="j" onclick="decide('+b.index+',\'auto\')">接上 (J)</button>'+
   '<button class="k" onclick="decide('+b.index+',\'break\')">断开 (K)</button>'+
   '<button onclick="clearOne('+b.index+')">撤销</button></div></div>';
}
function render(){
  const list=visible(); refreshStats();
  const box=document.getElementById('viewB');
  if(!list.length){ box.innerHTML='<div class="empty">这个筛选下没有待办了。可以切到「全部」抽查，或点右上角导出决定。</div>'; return; }
  if(cursor>=list.length) cursor=0;
  const b=list[cursor]; box.innerHTML=cardHtml(b);
  localStorage.setItem('gene_join_decisions',JSON.stringify(decisions));
}
function decide(i,v){ decisions[i]=v; localStorage.setItem('gene_join_decisions',JSON.stringify(decisions)); step(-1); }
function clearOne(i){ delete decisions[i]; localStorage.setItem('gene_join_decisions',JSON.stringify(decisions)); render(); }
function step(delta){ const n=visible().length; if(!n)return; cursor=(cursor+delta+n)%n; render(); }
function exportDecisions(){
  const out=DATA.filter(b=>decisions[b.index]!==undefined).map(b=>({index:b.index,aPage:b.aPage,bPage:b.bPage,decision:decisions[b.index]}));
  const blob=new Blob([JSON.stringify(out,null,1)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='join_decisions.json'; a.click();
}
function tab(t){
  document.getElementById('viewB').style.display=t==='b'?'':'none';
  document.getElementById('viewP').style.display=t==='p'?'':'none';
  document.getElementById('tabB').className=t==='b'?'on':'';
  document.getElementById('tabP').className=t==='p'?'on':'';
  if(t==='p') document.getElementById('viewP').innerHTML='<div class="card"><h3>合并后的完整段落（'+PARAS.length+' 段）</h3>'+
    PARAS.slice(0,200).map(p=>'<div class="p-item"><small>'+p.pages.join(' / ')+' · '+p.columns.length+' 列</small><pre>'+p.text+'</pre></div>').join('')+'</div>';
}
document.getElementById('filter').onchange=()=>{cursor=0;render();};
document.addEventListener('keydown',e=>{
  if(e.key==='ArrowRight'||e.key==='ArrowDown') step(-1);
  else if(e.key==='ArrowLeft'||e.key==='ArrowUp') step(1);
  else if(e.key==='j'||e.key==='J'){ const b=visible()[cursor]; if(b)decide(b.index,'auto'); }
  else if(e.key==='k'||e.key==='K'){ const b=visible()[cursor]; if(b)decide(b.index,'break'); }
});
render();
</script></body></html>
"""


def build_images(columns, boundaries, pages_by_name):
    """只为复核边界涉及的列生成裁片。"""
    (C.REVIEW / 'imgs').mkdir(parents=True, exist_ok=True)
    need = set()
    for b in boundaries:
        need.update(b['aCols']); need.update(b['bCols'])
    idx = {c['id']: c for c in columns}
    cache = {}
    for cid in need:
        c = idx.get(cid)
        if not c:
            continue
        p = pages_by_name.get(c['page'])
        if not p:
            continue
        key = c['page']
        if key not in cache:
            im = L.rotate_deg(Image.open(C.SRC / c['page']).convert('L'), p['rotate'])
            if im.width > C.WORK_W:
                im = im.resize((C.WORK_W, round(im.height * C.WORK_W / im.width)), Image.LANCZOS)
            cache[key] = im
        im = cache[key]
        w, h = im.size
        crop = im.crop((int(c['x1'] * w), int(c['y1'] * h),
                        max(int(c['x1'] * w) + 4, int(c['x2'] * w)),
                        max(int(c['y1'] * h) + 8, int(c['y2'] * h))))
        crop.thumbnail((120, 260))
        crop.convert('RGB').save(C.REVIEW / 'imgs' / f'{cid}.png')


def main():
    t0 = time.time()
    joins = json.loads((C.OUT / 'joins.json').read_text(encoding='utf-8'))
    ocr = json.loads((C.OUT / 'ocr.json').read_text(encoding='utf-8'))['columns']
    pd = json.loads((C.OUT / page_source_name()).read_text(encoding='utf-8'))
    pages_by_name = {p['name']: p for p in pd['pages']}
    idx = {c['id']: c for c in ocr}

    # 只摆「跨照片边界」：同一张照片内的相邻行天然是不同条目，不用人看。
    cross = [b for b in joins['boundaries'] if not b.get('samePage')]
    for b in cross:
        b['aText'] = [idx[i]['text'] if i in idx else '' for i in b['aCols']]
        b['bText'] = [idx[i]['text'] if i in idx else '' for i in b['bCols']]

    targets = [b for b in cross if b.get('weak') or b['decision'] == 'review']
    build_images(ocr, targets, pages_by_name)

    html = (TPL.replace('__DATA__', json.dumps(cross, ensure_ascii=False))
               .replace('__PARAS__', json.dumps(joins['paragraphs'], ensure_ascii=False)))
    (C.REVIEW / 'index.html').write_text(html, encoding='utf-8')

    print('=== 复核台 ===')
    print('跨照片边界', len(cross), '建议抽查', len(targets),
          '裁片', len(list((C.REVIEW / 'imgs').glob('*.png'))),
          '耗时 %.0fs' % (time.time() - t0))
    print('打开:', C.REVIEW / 'index.html')


def page_source_name():
    return 'pages_resolved.json' if (C.OUT / 'pages_resolved.json').exists() else 'pages.json'


if __name__ == '__main__':
    main()
