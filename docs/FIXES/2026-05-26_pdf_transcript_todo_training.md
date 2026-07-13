# PDF 與筆錄待辦訓練流程補強

日期：2026-05-26

## 問題

部分新法院通知、程序裁定、判決書或筆錄已進入案件資料夾，但 OSC 待辦與 Google 日曆沒有同步建立。根因不是單一案件，而是來源分散：

- PDF 待辦掃描依賴法院通知/程序裁定/「判決書或終局裁定及處分」資料夾與舊快取；歷史「判決書」資料夾仍會被相容辨識。
- 筆錄待辦抽取器原先主要依賴 `.agent/transcript_index.json`，新筆錄若尚未夜間索引，六小時待辦更新會漏掃。
- 規則更新後，舊的「無待辦」快取可能讓同一批 PDF 在 14 天內不再重新評估。

## 修正

1. 筆錄待辦抽取器改為雙路徑 discovery：
   - 先掃近期實體筆錄資料夾。
   - 再補讀既有筆錄索引。
   - 去重後以 mtime 新檔優先。
   - 單份筆錄讀取加入 timeout，避免壞檔或雲端 placeholder 卡住整輪待辦更新。

2. PDF 待辦快取加入規則版本：
   - 規則更新後，不再沿用舊 no-todo 快取。
   - 重新掃描後會寫入新版本，避免無限重掃。

3. 新增訓練匯出器：
   - `scripts/ops/pdf_todo_training_export.py`
   - 來源包含法院通知/程序裁定/判決書與筆錄。
   - 產出 `.runtime/pdf_todo_training_latest.jsonl` 與摘要 JSON。
   - 標記 `todo_extracted`、`todo_like_name_no_hit`、`needs_text_or_ocr_review`、`transcript_high_confidence_todo` 等，供後續規則迭代。

## 排程

`job_osc_events_refresh` 仍是主流程，每 6 小時執行：

- OSC 掃描。
- PDF 待辦掃描。
- 筆錄待辦掃描。
- Google 日曆匯入與 OSC 待辦推送。

另於筆錄同步後 30 分鐘補 `job_transcript_indexer`，使搜尋與摘要索引也能跟上新筆錄。

## 驗證重點

- 劉信義新筆錄可抽出 2026-07-30 09:30 開庭待辦。
- 新增單元測試涵蓋近期未索引筆錄、PDF 規則版本快取失效、訓練匯出摘要。
