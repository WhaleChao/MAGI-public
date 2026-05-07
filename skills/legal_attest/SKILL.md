---
name: legal_attest
description: 存證信函產生器 — 透過多步驟對話收集寄收件人資訊與內文，自動排版產出符合郵局格式的存證信函 PDF。
license: MIT
compatibility: Python 3.10+, reportlab, TW-Kai font
metadata:
  author: MAGI
  version: "1.0"
  sage: keeper
---

# legal_attest

互動式存證信函產生器。透過多步驟對話流程（chatbot 模式）逐步收集寄件人、收件人資訊與信函內文，完成後自動排版並輸出符合台灣郵局雙掛號格式的 PDF 檔案。

## Capabilities

- 多步驟對話流程：依序詢問寄件人姓名/地址、收件人姓名/地址、信函內文
- 支援分段輸入長篇內文，回覆「OK」後合併產出
- 自動排版至郵局存證信函格式（使用台灣楷體字型）
- PDF 輸出至 `/Users/ai/Desktop/MAGI_v2/exports/` 並產生下載連結
- 每位使用者獨立對話狀態，支援多人同時使用
- 狀態持久化至 `.agent/legal_attest_state.json`

## Usage

透過 LINE/DC 或 MAGI chat 介面觸發：

```
使用者：存證信函          → 啟動流程
使用者：王大明            → 寄件人姓名
使用者：台北市信義區...    → 寄件人地址
使用者：李小華            → 收件人姓名
使用者：新北市板橋區...    → 收件人地址
使用者：茲因台端於...      → 信函內文（可多次傳送）
使用者：OK               → 產出 PDF
```

## Commands

1. `init` — 開始新的存證信函對話流程
2. （依對話步驟自動推進：寄件人姓名 → 寄件人地址 → 收件人姓名 → 收件人地址 → 內文 → 產出）
3. `取消` — 隨時中止流程並重置狀態

## Dependencies

- `generator/core.py` — 存證信函排版與合併邏輯
- `generator/pdfpainter.py` — PDF 繪製引擎
- `generator/pdfpage.py` — 頁面版面配置
- `generator/res/tw_lal.pdf` — 郵局存證信函底板
- `generator/res/TW-Kai-98_1.ttf` — 台灣楷體字型
