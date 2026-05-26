---
name: transcript-todo-extractor
description: 從法院筆錄中的法官指示、下次庭期、期限命令與候核辦記載建立 OSC 待辦事項。
---

# transcript-todo-extractor

用途：掃描已歸檔的法院筆錄 PDF，辨識可轉成 OSC 待辦的內容。

原則：

- 高信心項目可以直接寫入 `case_todos`，再由既有 Google 日曆同步流程推送。
- 中信心項目只列入待審清單，不自動寫入。
- `候核辦` 表示沒有下次庭期，但仍需追蹤，預設以筆錄日期加 7 日建立提醒。
- 筆錄中只有權利告知、身分資料、前案日期、例行問答時，不建立待辦。
- 未指定 `--path` 時，會同時讀取筆錄索引與近期實體筆錄資料夾；新筆錄不必等夜間索引完成才會進入待辦掃描。

常用指令：

```bash
python3 skills/transcript-todo-extractor/action.py --task dry_run --limit 30
python3 skills/transcript-todo-extractor/action.py --task apply --path "/path/to/筆錄.pdf"
```

環境變數：

| 名稱 | 說明 |
| --- | --- |
| `TRANSCRIPT_TODO_INDEX_DB` | 筆錄索引 JSON，預設 `.agent/transcript_index.json` |
| `TRANSCRIPT_TODO_LIMIT` | 預設掃描上限 |
| `TRANSCRIPT_TODO_TAIL_PAGES` | 只掃筆錄最後幾頁，預設 3 |
| `TRANSCRIPT_TODO_RECENT_DAYS` | 額外掃描最近幾天新增/修改的筆錄 PDF，預設 45 |
| `TRANSCRIPT_TODO_CASE_ROOTS` | 指定案件根目錄；未指定時使用 MAGI 路徑設定 |
| `TRANSCRIPT_TODO_LISTING_BUDGET_SEC` | 近期筆錄資料夾列舉時間上限，預設 120 秒 |
| `TRANSCRIPT_TODO_PDF_TIMEOUT_SEC` | 單份筆錄讀取上限，預設 25 秒，避免壞檔或雲端 placeholder 卡住排程 |
