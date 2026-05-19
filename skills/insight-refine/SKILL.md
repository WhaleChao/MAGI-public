---
name: insight-refine
description: INSIGHT 文字精煉（用 CASPER 分散式推理清理 OCR/判決內容成可用見解），可 headless 呼叫與自測。
author: CASPER
created: 2026-02-13
---

# insight-refine

## 指令
1. `help`
1. `self_test`
1. `refine { ... }`

`refine` payload（JSON）常用欄位：
- `raw_text`（必填）
- `case_reason_context`（可選）
- `url`（可選）
- `examples_text`（可選，Few-shot 範例）
- `prompt_template`（可選；未提供則使用內建預設）

