# 2026-05-12 Public Docs Current-State Audit

目的：對照 2026-05-07 至 2026-05-12 的功能修復與文件內容，補齊 README、操作手冊、使用者手冊與維運手冊中仍停留在舊狀態的說明。

## 已補進文件的變更

| 範圍 | 文件更新 |
|---|---|
| 公開 / 商用門檻 | README、操作手冊、Commercial Readiness、Operator Runbook 補上 public audit、smoke50、production-live、commercial-release、live gate 與 runtime 私有資料不得進 git |
| 法扶消債 | 操作手冊補上 OSC 條件邏輯、可複製待補文字、所得清單依年度自動更新 |
| 法扶結案 | 操作手冊補上強制執行案件可用判決書資料夾內執行命令、同名不同程序不得只靠姓名、已結案仍可開資料夾/檔案 |
| 法扶活動計數 | README、操作手冊、USER_GUIDE、Commercial Readiness 補上開庭、會議、律見、閱卷、電話聯繫統計來源與同名消歧 |
| Google Calendar | README、操作手冊、USER_GUIDE、Operator Runbook 補上 OSC 編號前綴、法扶 DB 身分判斷、同名多案跳過規則 |
| PDF / OCR | README、操作手冊補上信封頁排除、多引擎 OCR 共識、法律文字修正與人工命名回饋 |
| 書狀產生 | README、操作手冊、USER_GUIDE 補上 Word/PDF 排版保護與同案由修正學習 |
| 帳務 | README、操作手冊補上 Google Sheets 週一/週五匯入、非本人標識排除、固定支出去重 |
| 實務見解 MCP | README、操作手冊、USER_GUIDE 補上台灣法律 MCP 補強與查不到即回查不到 |
| 所務總覽 / 網頁版 | README、USER_GUIDE 補上整合入口；把舊網頁總覽名稱改為 MAGI 網頁版 / MAGI |
| NAS / 磁碟 | README、操作手冊、Operator Runbook 補上低水位、快取清理、不可刪 Paperclip 單機版 JSON/pickle/db/sqlite、避免 lumi-1/homes-1 |
| 通知 / 測試 | README、操作手冊、Operator Runbook 補上通知分流與 live gate，不再只看單點成功 |

## 仍需用 live gate 持續守住

- 法扶 portal、閱卷、筆錄三模組仍屬外部網站依賴，正式提交前需確認碼或人工確認。
- Google Calendar 匯入需定期 dry-run，避免同事新增的日曆格式破壞法扶計數。
- OCR/PDF 命名品質需持續抽測法院通知、程序裁定、判決、對方歷次書狀與判決書資料夾。
- 帳務匯入需定期檢查固定支出與試算表項目是否重複。
- 公開版與私用版分支不得混推；公開版不得包含私有實務見解來源 / 私有 runtime / token / 案件資料。
