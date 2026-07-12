# MAGI 舊案雙向同步與新案處理設計

日期：2026-05-22

## 目前結論

- OSC 是案件主資料的唯一權威來源：案件編號、案件狀態、案件類型、案由、Windows 優先路徑都以 OSC DB 為準。
- NAS 是正式案件檔案的主要保存位置；Google Drive `案件辦理` 是同事補放資料與外部協作來源。
- Google Drive 與 NAS 的同步只能補齊檔案，不可以覆蓋 OSC 對案件狀態與資料夾路徑的人工判斷。
- `2025-0034 中央選舉委員會／海上賽鴿案` 已依使用者確認改回進行中，DB 路徑使用 `Z:\<NAS_ACCOUNT>\01_案件\...` 形式保存。
- 2026-05-22 的 Drive/NAS 盤點中，44 件待確認資料夾已依使用者指示處理：除 `黃日霖` 外，其餘 43 件列入本機 runtime 排除清單，不進同步、不建 NAS 資料夾、不下載。

## 設計原則

1. **OSC DB 優先**
   - 新案案件編號只能由 OSC 產生。
   - 人工在 OSC 點選「結案」或改為「進行中」後，掃描器不得再用舊資料夾位置覆寫狀態。
   - DB 內保存 Windows 路徑：進行中用 `Z:\<NAS_ACCOUNT>\...`，結案用 `Y:\lumi\...`；Mac 本機路徑只在讀寫時轉換。

2. **不覆蓋、不刪除、不猜測**
   - 雲端有、NAS 沒有：先列入下載計畫，只有在唯一匹配且未被排除時才下載。
   - NAS 有、雲端沒有：先列入上傳計畫，只有在開啟寫入授權且唯一匹配時才上傳。
   - 同路徑但大小或雜湊不同：進入衝突清單，不自動覆蓋。
   - 同名多案且沒有案號、法扶案號、法院案號、備註或別名足以判斷時，保留人工確認。

3. **保留兩邊原本資料夾邏輯**
   - NAS/OSC 仍使用正式案件資料夾規則，例如 `10_判決書或終局裁定及處分`、`09_法院通知或程序裁定`、`08_筆錄`。
   - Google Drive 仍使用同事習慣的協作資料夾規則，例如 `法院判決`、`法院通知`、`閱卷資料/筆錄`。
   - MAGI 只在同步邊界做轉換，不硬改任一邊的資料夾名稱，避免 OSC、Windows 路徑、Synology Drive 與同事雲端習慣互相破壞。
   - 預設轉換如下：
     - Drive `法院判決` ↔ NAS `10_判決書或終局裁定及處分`（舊 `10_判決書` 僅作相容讀取/上傳）
     - Drive `法院通知`、`法院通知與程序裁定`、`程序裁定` ↔ NAS `09_法院通知或程序裁定`
     - Drive `閱卷資料/筆錄` ↔ NAS `08_筆錄`
     - Drive `閱卷資料` ↔ NAS `06_閱卷資料`
     - Drive `結案酬金領款單` ↔ NAS `03_結案資料`
     - Drive `我方書狀` ↔ NAS `04_我方歷次書狀`
     - Drive `對造書狀` ↔ NAS `05_對方歷次書狀`

4. **同步範圍可稽核**
   - `諮詢案件`、`縣府調解案件` 沒有 OSC 案號者不進同步範圍。
   - Aaron 單獨資料夾若 NAS 沒有唯一對應，不自動建立 NAS 資料夾。
   - `陪偵`、`消債` 是案件種類層，不是案件資料夾。
   - 私有別名與排除清單存放在 `.runtime/drive_sync/`，不提交到公開版。

5. **資源保護**
   - Google Drive API 設定 timeout，避免單次盤點卡死。
   - 深度讀取 Drive/NAS 子資料夾只在 DB、案號、備註、別名無法判斷時才啟動。
   - 每輪同步要有檔案數、位元組、單案深度、總耗時上限。

## 舊案雙向同步流程

### 第一階段：唯讀盤點

輸入：

- Google Drive：`案件辦理`
- NAS 進行中：`Z:\<NAS_ACCOUNT>\01_案件`
- NAS 結案：`Y:\lumi\03_工作資料\10_結案`
- OSC DB：案件表、當事人、備註、對造資料、法扶案號、法院案號
- Runtime：別名清單、排除清單

輸出：

- 已唯一匹配
- 雲端有、NAS 無
- NAS 有、雲端無
- 同名多案待判斷
- 排除同步範圍
- 檔案差異與衝突清單

逐檔差異報告必須同時輸出 Markdown 與 CSV。CSV 欄位至少包含：

- `case_number`
- `diff_type`：`drive_has_nas_missing`、`nas_has_drive_missing`、`same_path_conflict`
- `relative_path`
- `drive_path`
- `local_path`
- `drive_id`
- `drive_size`
- `local_size`
- `reason`
- `web_url`

這份 CSV 是給日常協作使用的主要報表：同事習慣放在 Google Drive 時，MAGI 要能列出 Google 有但 NAS 缺的檔案；律師端或 MAGI 自動歸檔放在 NAS 時，也要能列出 NAS 有但 Google 缺的檔案。

### 第二階段：Drive → NAS 補檔

條件：

- 案件已唯一匹配。
- 目標案件未被排除。
- 目標 NAS 路徑由 OSC DB 決定。
- 目標檔案不存在。

動作：

- Google Docs、Sheets、Slides 先匯出成 DOCX、XLSX、PPTX。
- 一般檔案直接下載。
- 下載前先把 Drive 相對路徑轉成 NAS/OSC 相對路徑，例如 `法院判決/a.pdf` 會落在 `10_判決書或終局裁定及處分/a.pdf`。
- 下載到暫存檔，完成後再改名，避免半檔。
- 不覆蓋既有檔案。
- 寫入 manifest，記錄 Drive file id、雜湊、大小、下載時間與目標路徑。

### 第三階段：NAS → Drive 補檔

條件：

- 需要另外開啟 Google Drive 寫入授權。
- 案件已唯一匹配，且已有 Drive folder id。
- NAS 檔案不在忽略清單內。
- Drive 端沒有同名同路徑檔案。

動作：

- 只上傳 NAS 有、Drive 沒有的檔案。
- 上傳前先把 NAS/OSC 相對路徑轉成 Drive 相對路徑，例如 `10_判決書或終局裁定及處分/a.pdf` 會上傳到 `法院判決/a.pdf`；歷史路徑 `10_判決書/a.pdf` 也會映射到同一個 Drive 目的地。
- 不刪除 Drive 既有檔案。
- 若 Drive 端已有同名但大小不同，列入衝突，不覆蓋。
- 上傳後更新 manifest。

現階段若尚未開啟 Google Drive 寫入授權，MAGI 仍必須列報 `nas_has_drive_missing`，讓使用者知道同事的 Google Drive 版本缺少哪些 NAS 正式檔案。

已開啟寫入授權後，MAGI 可執行 `--execute-uploads`，將 `nas_has_drive_missing` 的檔案補上傳至該案件的 Google Drive 對應資料夾。上傳只處理缺檔，不覆蓋既有雲端檔案；若缺少中間資料夾，只建立該檔案必要的父層資料夾。

### 第四階段：衝突處理

衝突類型：

- 同一路徑大小不同。
- 同一路徑雜湊不同。
- 非 Google 原生檔案同名同大小但 MD5 不同。
- Google Docs、Sheets、Slides 兩邊都有但尚未匯出逐字節驗證。
- Drive 與 NAS 都有同名但修改時間差距大。
- 案件資料夾同名但對應不同 OSC 案件。

處理方式：

- 預設只列報，不自動處理。
- 網頁端提供「以 NAS 為準」「以 Drive 為準」「另存副本」「永久排除」四種操作。
- 所有操作寫入稽核紀錄，包含操作者、時間、來源、目的路徑。

## 新案處理流程

### OSC 建案

新案只能從 OSC 建立，OSC 必須：

1. 自動產生案件編號。
2. 建立 Windows 優先正式路徑。
3. 寫入案件類型、案件種類、審級、案由、法院、股別、備註。
4. 依案件狀態決定進行中或結案根目錄。

### NAS 建資料夾

- NAS 可用時，直接在正式 NAS 路徑建立。
- NAS 不可用時，建立在本機暫存佇列，不直接讓 Synology Drive 生空殼案件資料夾。
- MAGI 偵測 NAS 恢復後再搬到正式路徑。

### Drive 對接

新案建立後，MAGI 進行三步驟：

1. 以 OSC 案號搜尋 Drive 是否已有對應資料夾。
2. 若找到唯一資料夾，建立 case cloud link。
3. 若找不到，依設定決定：
   - 私用版：可自動建立 Drive 對應資料夾。
   - 公開版：預設不建立，僅提供設定精靈選項。

Drive 端若出現沒有 OSC 案號的新資料夾：

- 不自動建案。
- 不自動建立 NAS 資料夾。
- 進入「待歸戶」清單，由使用者選擇既有案件或建立新案。

目前私用版已先落地的行為：

- OSC 新案或手動「建立案件資料夾」成功後，會以 best-effort 方式建立 Google Drive 對應案件資料夾。
- NAS/OSC 仍使用 `一般案件/行政/2026-xxxx-...` 這種正式路徑；Drive 端則使用 `一般案件/Lumi/當事人-審級-案由`，不顯示 OSC 內部案號。
- 消債、陪偵等特殊案件種類在 Drive 端仍進入既有特殊 bucket，例如 `法扶案件/Lumi/01.消債/...`。
- Drive 端資料夾名稱不得顯示 OSC 內部案件系統編號，OSC 案號只存於 Drive `appProperties` 供 MAGI 對應。
- 一般案件 Drive 顯示名稱採協作可讀名稱，例如 `當事人-審級-案由`。
- 法扶案件 Drive 顯示名稱必須保留法扶案號，例如 `胡裕生-1150521-E-011-刑事偵查-竊盜`。
- 法扶消債案件因已位於 `01.消債` bucket，Drive 顯示名稱採法扶既有語境，例如 `金李連芯-1150519-E-014-消費者債務清理事件-消費者債務清理事件`，不附加 OSC 的「更生」「清算」等程序詞。
- Drive 建立失敗不會阻斷 OSC 建案，背景同步 worker 會在後續排程補建。

## 建議資料結構

### `case_cloud_links`

用途：記錄 OSC 案件與雲端資料夾的連結。

欄位：

- `case_number`
- `provider`：目前為 `google_drive`
- `remote_folder_id`
- `remote_path`
- `sync_enabled`
- `direction_policy`：`drive_to_nas`、`nas_to_drive`、`bidirectional`
- `last_inventory_at`
- `last_sync_at`
- `last_error`

### `case_file_sync_manifest`

用途：記錄檔案層同步狀態。

欄位：

- `case_number`
- `provider`
- `remote_file_id`
- `relative_path`
- `local_path`
- `size`
- `md5`
- `source_modified_at`
- `last_synced_at`
- `sync_direction`
- `conflict_status`

## 排程設計

- 每 6 小時：快速盤點新案與待歸戶資料夾。
- 每日離峰：完整 inventory 與差異計畫。
- 每日離峰：Drive → NAS 補檔，受檔案數與容量上限限制。
- 每週離峰：NAS → Drive 補檔 dry run；確認穩定後再開寫入模式。
- 每週：空殼資料夾清理，但不得刪除含實際案件檔案的資料夾。

目前私用版已啟用的本機排程：

- `job_drive_case_sync_bidirectional` 每 6 小時執行 `scripts/drive_case_sync_worker.py`。
- 每輪只掃描有限案件與有限檔案數，使用 state offset 輪轉唯一匹配案件，避免固定只處理前幾件。
- 每輪同步動作皆為 missing-only：缺檔才補，不覆蓋、不刪除。
- 每輪可為最近新建的 NAS-only 案件補建 Drive 端案件資料夾。

## Live 驗收標準

1. `海上賽鴿` 在 OSC 顯示為進行中，路徑為 `Z:\...`。
2. 2026-05-22 的 44 件待確認只剩 `黃日霖` 一件。
3. 其餘 43 件列入排除，不會出現在下載計畫。
4. 已匹配案件可做 Drive → NAS 補檔，且不覆蓋既有檔案。
5. 新建 OSC 案件時，案件編號由 OSC 產生，Drive 只建立或連結對應資料夾，不反向產生亂碼案件。
6. 結案後仍能將法扶結案酬金、通知書等後續檔案歸入已封存案件資料夾。
7. 無 NAS 時不產生進行中空殼案件資料夾；恢復 NAS 後再補搬。

## 待實作項目

1. 建立 `case_cloud_links` 與 `case_file_sync_manifest` migration。
2. 將 Drive → NAS 現有下載流程改為讀寫 manifest。
3. 在網頁版加入「雲端同步狀態」「待歸戶」「衝突清單」三個介面。
4. 建立壓力測試：大量小檔、大型 PDF、Google Docs 匯出、NAS 斷線重試、同名多案。

## 已落地項目

1. 在 OSC 新案建立與手動建立資料夾流程加入 Google Drive 對應資料夾建立。
2. 新增 `scripts/drive_case_sync_worker.py`，用 bounded batch 做 Drive ↔ NAS 缺檔同步。
3. 新增 NAS → Drive upload execution，缺檔才補上傳，不覆蓋既有雲端檔案。
4. 新增 NAS-only 新案資料夾補建 Google Drive folder 的安全流程。
