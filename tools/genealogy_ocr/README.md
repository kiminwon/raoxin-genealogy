# 老谱识别流水线（本地离线）

把 `C:\Users\mayn\Desktop\王启明\家谱\源流` 下 420 张连续翻拍的老族谱照片，
变成「按阅读顺序拼回的完整段落」+「结构化草案」+「人工复核台」。

**源图只读**，所有产物写到 `家谱\_ocr_out\`；不碰小程序与后端代码。

## 环境

独立 venv：`tools/genealogy_ocr/.venv`（Python 3.13）。

引擎用 `rapidocr-onnxruntime==1.2.3`（PP-OCRv4 模型的 ONNX 版，模型随包自带，
全程离线）。**不是** PaddlePaddle 本体：本机 Python 3.13 没有 paddlepaddle 的 wheel，
装了 13 分钟一个包都没落地。两者模型同源、识别行为基本一致；接口隔离在
`ocr_engine.py`，将来换 Python 3.11/3.12 + PaddleOCR 3.x 只改这一个文件。

```
.venv/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    rapidocr-onnxruntime==1.2.3 numpy pillow
```

## 跑法（按顺序）

```
01_normalize.py [张数]        # 方向 / 拉正 / 尺度 / 去重 / 半页配对 → pages.json
02_segment.py   [页数]        # 整页 OCR + 版面归属 → segments.json（断点续跑）
03_ocr.py                     # 词典纠错 + 复核标记 → ocr.json / ocr_report.json
04_join.py                    # 跨照片接续 → joins.json
05_review.py                  # 生成 PC 复核台 → review/index.html
   （人工：双击 index.html，J 接上 / K 断开，导出 join_decisions.json）
06_apply.py  [决定文件]       # 并回人工决定、重切段落
06_export.py                  # 段落 → records.json / records.csv
07_report.py                  # 汇总 → REPORT.md
view_contact.py [张数] [文件名]  # 抽样联系表（人工核对方向 / 半页用）
```

`02_segment.py` 支持断点续跑：每页结果追加到 `ocr_lines.jsonl`，重跑跳过已完成页。
要全量重算就删掉 `ocr_lines.jsonl`。

## 为什么这么设计

| 决策 | 原因 |
|---|---|
| **不拼像素，拼列** | 相邻照片边缘相关仅 0.13–0.40，基本无重叠；尺度又各不相同，自动配准无缝拼接不可行 |
| **整页直接 OCR，不手写切列** | 实测投影切列把横排大字、格线切成碎片，14 列识别全空；而 PP-OCR 检测器自己就能把竖排中文列框成一条竖长框，识别器直接吐「成化丙戌年 / 壽一公長子 / 第一世 / 生子二」，每页 40+ 框、置信 0.66 |
| **世代行用「第 N 世印章 + y 聚类」，不用横线** | 横线检测在齿录页几乎全失效，源流页也会漏线（第一页 5 个世次只抓到 4 条） |
| **默认接上，只在有断点证据时断开** | 这批是连续翻拍、每张切在人物块中间；接错只是两个条目粘一起（后续按字段头还能切开），断错会把一个条目劈成两半、丢父子归属 |
| **跨页模板必须跨越拼接点** | B 首自己就完整的「鼎六公次子」不能证明上页被切断，第一版因此误判 |
| **错字只改上下文闭合的套语** | 年号干支、時辰、廿日、長子、姓氏是有限词表的形近误识；人名地名生僻字只给建议，不擅改 |
| **每条改动留 fixes / provenance** | 复核时能看到原字与原图裁片；确认过的「裁片 ↔ 正确字」是日后的微调标注 |

## 产物字段要点

- `segments.json` → `columns[]`：`page/pageIndex/row/col` + 归一化 `x1,y1,x2,y2` + `raw/text/conf/box`
- `ocr.json` → 同上再加 `fixes/flags/suggests/needReview`
- `joins.json` → `boundaries[]`（`decision=auto|break|review`、`weak` 建议抽查、`reason` 判定依据）
  + `paragraphs[]`（`text/columns/sources/pages`）
- `records.json` → 结构化草案，字段见 `06_export.py::CSV_COLS`

## 已知边界

- 没有人工金标准，报告里的「字段可解析率」是代理指标，**不是字符准确率**。
- 雕版异体字（夀、塤、廿、卅、巽山乾向）是模型训练域之外的东西，换 PP-OCRv5 也救不了。
- 一段含多人时标 `multiPerson`，需人工拆；排行名跨页实体对齐（鏡一公 ↔ 鏡一）尚未做。
- `records.csv` 是**待审草案**，不是可入谱数据。导入须走 `source_type=1`、
  `living_status` 显式 2、关系走 `family_union`、落成待审修订，理事会确认后才生效。
