---
name: statutes-vdb
description: 依案件（案件資料夾/類型/案由）推斷相關法規，從法務部全國法規資料庫 API 抓取條文並寫入向量資料庫（MemBridge），供 CASPER 對話查詢使用。適合掛在夜間任務或排程巡檢。
author: CASPER
created: 2026-02-17
triggers:
  - "法規入庫"
  - "條文入庫"
  - "更新法規"
  - "案件法規"
  - "向量庫法規"
---

# statutes-vdb

把「案件可能相關的法規」抓下來，整理成可檢索的 chunk，寫入向量資料庫。

## 資料來源

- 全國法規資料庫官方 API：`https://law.moj.gov.tw/api/Ch/Law/json`（zip 內含 `ChLaw.json`）

## 主要任務

### 1) 夜間更新（推薦）
由排程（例如 `magi-autopilot nightly`）傳入案件清單後，針對每個案件：

- 解析案件資料夾名稱中的法規字串（例如：`毒品危害防制條例`）
- 依案件領域補齊基本法（刑事/民事/行政…）
- 對每部法規：按條分段 chunk → `MemBridge.remember()` 寫入向量庫
- 另外寫入「案件 ↔ 法規清單」的對應記憶，方便後續對話呼叫

CLI：
```bash
python3 action.py --task 'update_cases {"cases":[{"case_number":"2025-0088","case_path":"/path/to/case"}] }'
```

### 2) 查詢（本機自測用）
```bash
python3 action.py --task 'search {"query":"毒品危害防制條例 第 11 條", "top_k": 5}'
```

## 輸出/狀態

回傳 JSON（摘要）：
- `ok`
- `dataset_update`
- `cases_processed`
- `laws_linked`
- `laws_ingested` / `laws_skipped` / `laws_missing`


## 呼叫格式
觸發詞：法規、法條、查法條
參數：query=查詢內容

## 呼叫範例
使用者：民法第 184 條的規定
→ 查法條 query=民法第184條

使用者：強制執行法管轄規定
→ 查法條 query=強制執行法管轄
