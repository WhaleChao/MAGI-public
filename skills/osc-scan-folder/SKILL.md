---
name: osc-scan-folder
description: OSC 拆分技能：掃描單一資料夾（例如某案件的「法院通知或程序裁定」或「閱卷資料」）並解析待辦；寫 DB 或降級寫入佇列。不刪檔，可 headless 呼叫與自測。
author: CASPER
created: 2026-02-18
---

# osc-scan-folder

## 指令
1. `help`
1. `self_test`
1. `run {"root":"/abs/folder","case_number":"2025-0088","max_files":200}`（也支援 `掃描資料夾待辦 {...}`）

