# CLAUDE.md 重構歸檔（2026-05-04）

CLAUDE.md 達 555 行，超出 §2.14 的 500 行紅線。本檔保留被移出的 fix log / 根因 / commit 流水帳，方便日後考古；CLAUDE.md 只保留 standing 紅線。重構者：Opus 4.7。

---

## §4.10 docx-editor skill 完整 Phase 紀錄（2026-04-27 ~ 2026-05-02）

新 skill `skills/docx-editor/`：對 .docx 套 anchored find-and-replace 產出 Word 真實 tracked changes（`<w:ins>`/`<w:del>`），律師在 Word 內可逐一 Accept/Reject。移植自 OSS Mike repo `docxTrackedChanges.ts`（TS 1178 行 → Python 純移植，無新依賴；用既有 lxml 6.0.3 + python-docx 1.2.0）。

- **Phase 1（2026-04-27）核心 + 真實律師書狀驗收**（46 unit tests + 鄭羢允聲請改期狀 round-trip 確認 `<w:del>/<w:ins>/<w:delText>` 結構正確、ID 唯一、author 標識正確、ZIP entries 不變）。commits `c7d3e21` ~ `d2f7699`（7 個）。
- **Phase 4（2026-05-02）generate_docx**：`lib/generator.py` + `cmd_generate` 從 sections list（heading/content/table/pageBreak）產 .docx。heading 層級不准跳級。commits `beda882`/`de60ff8`。
- **Phase 5（2026-05-02）citation 系統**：`skills/bridge/citation_format.py` 提供 `parse_citations()` 解析 `<CITATIONS>` JSON block + `render_citations_for_telegram()`；`ensemble_inference.ensemble_chat()` 加 `enable_citation: bool = False`（**預設 False 紅線**，既有 caller 完全行為不變）；`weekend_resummary.py` 用 feature flag `MAGI_RESUMMARY_ENABLE_CITATION`（預設 0）。commits `bc52397`/`5b404a7`/`9aadb56`。
- **Phase 5 順手修 pre-existing bug**：`ensemble_inference.py` 原 `role["system_prefix"]` dict access 在 ENSEMBLE_ROLES 沒此 key 時 KeyError，改為 `role.get("system_prefix", role.get("soul", ""))` fallback 到 soul（語意正確：soul 即人格 prefix）。
- **Phase 3（2026-05-02）chat-driven docx edit**：`lib/llm_edit_planner.py::plan_edits_with_llm()` 用 InferenceGateway 讀 docx 文字 + 律師指令 → 產 EditInput list（強制 anchor 預檢）。`cmd_chat_edit` 雙重閘門：source 必須含 user/telegram/discord/line（CLI 需 `MAGI_DOCX_EDITOR_ALLOW_CLI=1` bypass）。`api/pipelines/message_pipeline.py::_handle_docx_chat_edit_if_any` router 偵測 `.docx` attachment + 訊息含「@MAGI 編輯/修改」觸發詞。output 寫 `/tmp/magi_docx_edits/<ts>_<filename>` 不覆蓋原檔。commits `64f7904`/`e6c2903`/`466ae98`/`cb7a499`。
- **Phase 2（原計劃 §9）跳過**：MAGI 目前沒有「自動產出 .docx 草稿」的上游觸發點（grep LAF/閱卷/筆錄無 docx 產出），等 Phase 4 generate_docx 加上後若律師明確要求 LAF 結案報告書自動填等場景再做。
- **整體驗收層級：測試 + 部分驗收**（合計 97 個 unit tests 全綠：Phase 1 的 46 + Phase 4-5-3 的 51；全套 pytest 2329 passed 0 新增 failure；Phase 4 generate live verify ✅；Phase 3 chat_edit mock live verify ✅；Phase 5 citation parse + ensemble integration tests ✅）。**真實 LLM-driven chat edit live verify 待律師實際在 DC/TG 上傳 docx 觸發**。
- **commits 全列**：`c7d3e21`/`22e1385`/`77c4cd3`/`6a3e24d`/`78da5cc`/`a3615ee`/`d2f7699`（Phase 1）/ `beda882`/`de60ff8`（Phase 4）/ `bc52397`/`5b404a7`（Phase 5）/ `64f7904`/`e6c2903`/`466ae98`/`cb7a499`（Phase 3）/ `4b312ee`/`22457e2`/`9aadb56`/`9985b22`（整合 + 文檔 + sign-off）。
- **詳細實作計劃**：桌面 `MAGI_v2_docx-editor_實作計劃_20260427.md`（Phase 1）+ `MAGI_v2_docx-editor_Phase2-5_實作計劃_20260502.md`（Phase 2-5）。

---

## §4.11 OSC → Paperclip 完整改造紀錄（2026-05-02 ~ 2026-05-03）

### 4.11.0 品牌與介面（2026-05-02）

- **顯示品牌**：OSC 對外網頁顯示文字一律改成「📎 Paperclip」；**保留**所有後端 URL（`/osc/*`）、Python module 名（`api/blueprints/osc_*.py`、`static/osc/`）、blueprint 名、變數名、HTML element id（避免破壞 link / import / CSS）。
- **介面 audit**：55 處英文 placeholder / label 中文化（cases/accounting/clients/calendar/meetings/documents/todos/admin 共 9 個檔）。
- **Apple 系統風格**：`static/osc/osc-theme.css` 加 5 個 CSS variables（`--apple-font/radius/shadow/blue/border`）+ button hover/active 動畫 + input focus 藍邊光暈。**不改 layout grid/flex 結構**（CSS 變數化即可一鍵還原）。
- **按鈕 audit script**：`scripts/ops/audit_osc_buttons.py` 自動掃 HTML button + JS `fetch('/api/osc/...')` 對照 backend route — 每次大改前先跑確認 0 broken。本輪 audit：192 buttons / 164 fetch / 0 missing_route / 0 broken handler。
- **「開啟資料夾」邏輯重寫（核心修復）**：
    - 後端 `api/blueprints/osc_cases.py:osc_case_open_folder_api` 改 4 種 `error_kind`：`no_nas_no_synology`（彈窗請連 NAS 或開 Synology Drive）/ `folder_not_found_on_nas`（NAS 連著但路徑不存在）/ `folder_path_empty`（DB 沒設 folder_path）/ `open_failed`（路徑存在但 open 失敗）
    - 流程：先試 NAS SMB → 失敗試 Synology Drive 本機路徑 → 都失敗回 `error_kind` 給前端
    - **順手挖到舊 hardcoded bug**（line 539 寫死 `ok=True` 即使 `open_result.ok=False`）— 這是律師反映「按沒反應」的真實根因，本輪根修
    - 前端 `static/osc/tabs/cases.js::openCaseFolder` 改用 `<dialog>`-based `showAlert` helper（`static/osc/osc-ui.js` 新增），依 `error_kind` 顯示不同警告訊息，**絕不靜默**
- **驗收層級：驗收**（12 個 open_folder unit tests 全綠 + audit script 192 buttons 0 broken + 全套 pytest 2341 passed 0 新增 failure；待人工瀏覽器確認 Paperclip 字樣 + 對未連 NAS 案件按鈕看彈窗）。
- **commits**：`4d5ebad`(brand) / `ca9fd7d`(中文化) / `a4fbe09`(Apple CSS) / `8a4d399`(audit script) / `a2ad1b1`(open-folder 重寫含 hardcoded bug 修)。

### 4.11.1 UX v3 — IA 重組 + 跨電腦開資料夾 + Polish（2026-05-03）

- **Sidebar IA：16 平鋪 → 8 分組**。`templates/osc.html` 改 `<nav class="sidebar-nav">` + 4 single（業務概覽 / 結案歸檔 / 實務見解 / 系統設定）+ 4 group（案件 / 書狀 / 法扶 / 帳務）。**所有 16 個 `data-tab=X` 的 X 與既有 `<div id=X class="view">` 完全保留**，`bindTabs` 主邏輯不動。新檔 `static/osc/osc-grouping.js` 提供手風琴行為：點 group → 自動展開 + 點第一個 sub-tab + 收合其他 group；切 sub-tab（含外部觸發）也 auto-expand 所屬 group。`osc-events.js::bindTabs` 末尾呼叫 `autoExpandGroupForTab(tabId)`，`boot()` 用 `_safe` 包 `bindSidebarGroups`。
- **跨電腦開資料夾（核心修復）**：原本 `osc_case_open_folder_api` 在 server-side `open`，Tailscale / 遠端情境只會在伺服器 mac mini 開 Finder（律師看不到）。**改後端只回 `candidates` dict**（`smb_url` / `mac_synology` / `win_unc` / `win_synology`），由前端依 `navigator.platform` 決定：mac → `window.location.href = smb://...`；Win → 優先 `file:///` Win Synology Drive（含 `%USERPROFILE%` 落 fallback）/ 然後 file:UNC；iPad / 其他 → `showFolderPathDialog` 列 4 候選 + 複製按鈕。新 helper：`utils.py::_osc_windows_unc_candidates` + `_osc_windows_synology_candidates` + `osc-ui.js::showCustomDialog(title, bodyHtml)`（HTML body 的 `<dialog>` 版 alert）。
- **Polish**：(1) Card view 右下加 3 個 `.btn-icon` quick actions（⚙️ workbench / 📂 open-host / ✏️ edit）；(2) 全域 `#tabLoadingOverlay` + `osc-utils.js::showLoading/hideLoading`（ref-count 安全）+ `bindTabs` `_withLoading` wrapper；(3) 45 處 `placeholder="必填|選填"` → `"<欄位名>（必填|選填）"`；(4) `osc-theme.css` 加 `.btn-primary/.btn-secondary/.btn-danger`（與既有 `.btn.primary` 並存）+ table zebra + `.empty-state` component + `.card:hover` 微 shadow；(5) lafWizard / archiveWizard 各加漸層 callout 卡片說明流程。
- **驗收層級：驗收**。Playwright deep verify v6 12 項全綠（PASS=16/FAIL=0：sidebar 結構 / 手風琴 / 16 個 data-tab 保留 / 16 view 切換 / open-folder candidates / quick actions / loading overlay / showCustomDialog / placeholder polish）；`audit_osc_buttons.py` 176 buttons / 164 fetch / 0 missing_route；pytest **2332 passed, 4 skipped, 0 新增 failure**；`magi restart` 後 5002 `/health` 200 ok。律師人工瀏覽器 / iPad mini 確認待後續。
- **commits（12 依序）**：`a720d7c` Win helpers / `cb3e129` open-folder 後端改 candidates / `2e46bcf` 前端 platform-aware open / `b9ddcf3` sidebar IA HTML / `8810afd` osc-grouping.js + hook / `9f14890` card quick actions / `c726bd9` loading overlay / `08abb20` placeholder 中文化 / `f80bdd8` toolbar CSS / `615b8b1` wizard callout / `0f379e2` Apple polish / `cbefe69` deep verify v6。
- **詳細實作計劃**：桌面 `MAGI_v2_Paperclip網頁化_實作計劃_20260502.md`（Track 1-4）+ `MAGI_v2_Paperclip_UX_v3_IA重組_實作計劃_20260503.md`（本輪 UX v3）。

### 4.11.2 NAS 檔案總管 Phase 1 + Phase 2（2026-05-03）

- **目的**：律師遠端（iPad / Win Chrome / Mac Safari）在 Paperclip 內直接巡覽 NAS 案件資料夾；後端走 mac mini 已掛載的 SMB / Synology Drive，瀏覽器零安裝。
- **完整範圍（commits 1-13）**：P0 後端 5 + UI shell 1 + Phase 2 三檢視模式 / 預覽 modal / 拖放上傳 / case 連結 / 右鍵選單 / icon polish / Playwright deep verify。Phase 2 已於本輪 (2026-05-03 後段) 完成並通過 33/33 deep verify。
- **新檔**：
  - `api/blueprints/osc_files.py`（新 blueprint，註冊於 `api/server.py` line 446）
  - `api/osc/preview.py`（office/heic/csv/email/zip/hex 預覽 helper）
  - `static/osc/file-manager.css`
  - `static/osc/tabs/file_manager.js`
  - `templates/partials/osc/fileManager.html`
- **新 API**：
  - `GET /api/osc/folders/browse` 通用列出（暫存檔過濾 / 子檔數總大小）
  - `GET /api/osc/folders/tree` lazy-load tree（has_subdirs flag）
  - `POST /api/osc/folders/mkdir` / `rename` / `move`（move `to_trash:true` → `<base>/.trash/<name>_<ts>`，**取代永久刪除**符合 CLAUDE.md prohibited_actions）
  - `POST /api/osc/files/upload-multi` 多檔（副檔名 blacklist .exe/.bat/.sh/.ps1/...，每檔獨立結果回報）
  - `POST /api/osc/files/upload-chunked` 大檔分塊（session_id + chunk_index + total_chunks，~/.cache/paperclip-uploads/<sid>/，1hr TTL）
  - `GET /api/osc/files/preview` 統一入口（office→LibreOffice convert→PDF / heic→sips→jpg / csv/email/zip→JSON / 其他→hex dump）
  - `GET /api/osc/files/info` metadata
- **soffice 路徑**：macOS LibreOffice 預設不在 PATH，preview.py 自動 fallback 到 `/Applications/LibreOffice.app/Contents/MacOS/soffice`（已驗證 docx → 294860 byte PDF 含正確 `%PDF` magic）
- **預覽 LRU cache**：`~/.cache/paperclip-preview/<sha1(path+mtime)>.<ext>`，5GB cap，每次轉檔觸發 cleanup
- **Phase 2 commits 7-13 摘要**：
  - **commit 7** `812878c`: 三種檢視模式 (詳細/網格/清單) + 排序 + 暫存檔過濾 toggle
  - **commit 8** `e44659c`: 全檔類預覽 modal (PDF/img/Office/Email/Zip/Audio/Video/Hex)
  - **commit 9** `cfd6522`: 拖放上傳 + 進度條 + webkitdirectory + 衝突彈窗
  - **commit 10** `a027b04`: case-card / workbench → 檔案總管深層連結 (🌐 quick action)
  - **commit 11** `26ed116`: 右鍵選單 + 鍵盤捷徑 (F2/Del/Esc)
  - **commit 12** `2339ece`: file type icons 擴充 (📕📘📊📙🖼📧🗜🎵🎬📝⚙💿) + 檔名截斷 tooltip + breadcrumb 美化 (linear-gradient + ›)
  - **commit 13** `f606e46`: Playwright deep verify 33 項 + magic-byte mime sniff 強化（disguised .exe-as-.pdf 防護）
- **commit 13 backend 強化**：`api/blueprints/osc_files.py` 新增 `_sniff_executable()` (7 個 magic-byte signature: MZ/ELF/Mach-O 32+64 LE+BE/Java class/shebang)，串入 upload-multi (post-save) 和 upload-chunked (post-finalize)；偵測到 executable signature 立即刪除已存檔案並回 `blocked_content_signature`，硬擋 `.exe` rename 為 `.pdf` 上傳的繞過攻擊。
- **驗收層級：驗收**（Phase 1: 每個 P0 API curl live 通過 + Phase 2: Playwright headless 33/33 PASS：13 個檔類 preview status=200 + content-type 正確 / 結構顯示 (folders/hidden/view modes/sort/breadcrumb/tree) 全綠 / 上傳 7 子項 (single/multi/folder/chunked/.exe-rejected/conflict/missing-chunk-retry) 全綠 / 跨平台 3 個 UA (iPad Safari/Win Chrome/Mac Safari) 全綠 / 安全 3 子項 (path traversal blocked/disguised .exe blocked/.trash recycle) 全綠；screenshot `/tmp/paperclip_filemanager_p2_complete.png` + report `/tmp/paperclip_filemanager_p2_verify.json`）
- **deep verify 重跑指令**：`/usr/bin/python3 scripts/ops/paperclip_filemanager_deep_verify.py`（系統 python 3.9 內建 playwright；TEST_BASE 預設 `~/SynologyDrive/homes/01_案件/法扶案件/刑事`，sandbox `_p2_verify_sandbox` 自動 wipe + recreate）
- **fixtures**：`tests/fixtures/file_manager_samples/`（force-added，160KB total，17 個檔涵蓋 13 個預覽類型 + .exe + disguised .exe-as-.pdf）
- **詳細實作計劃**：桌面 `MAGI_v2_Paperclip_NAS檔案總管_實作計劃_20260503.md`。

---

## §5.1 閱卷系統 已完成 fix 詳細紀錄（2026-04-19 ~ 2026-04-25）

### Playwright popup 下載路徑（2026-04-19 已根修）
- **根因**：OLA portal 的「下載」按鈕觸發 `window.open()` 彈出新視窗，新視窗的下載不受 `Browser.setDownloadBehavior` (CDP session-level) 管控，實際下載到 `~/Downloads` 而非設定的 `download_folder/YYYYMMDD/`
- **修法**：`playwright_wrapper.py` 新增 `context.on("download", handler)` — context-level interceptor 會捕捉同一 browser context 下**所有**頁面/彈窗的下載事件；同時在 `_on_popup` 也對新彈窗補掛 `popup_page.on("download")`；新增 `set_download_dir()` 方法供 caller 動態更新目標目錄
- `file_review_automation.py` 在更新 CDP 路徑後，同步呼叫 `self.driver.set_download_dir(today_folder)` 讓 context-level interceptor 目標一致
- **誤報修正**：20 秒等待迴圈中對 `os.listdir()` 結果加 `os.path.isfile()` 過濾，排除 `_待歸檔` 等子目錄被誤判為已下載檔案
- **NAS 歸檔注意**：SMB over Tailscale relay (300ms RTT) 複製大檔案會觸發 `fcopyfile failed: Operation timed out`（原因是 macOS 嘗試複製 xattrs），修法：`xattr -c <file>` 清除 xattrs 後再 `cp`

### navigate_failed 兩輪根修（2026-04-19/04-24）
- **第一輪（commit e27588e）**：`navigate_to_file_review()` 呼叫 `click_link_and_wait_for_popup(timeout_ms=10000)`，OLA portal 在 Tailscale relay 高延遲環境下 `window.open()` 有時需 10-20 秒，超時即回 `navigate_failed`。修法：timeout 10s→25s + 首次逾時自動重找連結元素再試一次。`downloadable_probe` live 通過。
- **第二輪（2026-04-24）**：OLA portal 的 onClick handler 先執行 async AJAX，完成後才呼叫 `window.open()`；夜間高延遲下 AJAX 可能超過 50 秒，兩次 25s popup 等待都逾時但 `window.open()` 其實已被執行。修法：兩次逾時後加 context.pages fallback：等 3s → 比對 `self.driver.window_handles` 與 `original_windows` 的差集，找到則視為成功捕獲。
- **守則**：不要再動 `navigate_to_file_review()` 的 popup 等待與 fallback 邏輯；兩輪已根修。如再出現 navigate_failed，先查 server.log 確認是否為全新的 failure mode。

### Playwright `element.click()` / `execute_script()` 卡死（2026-04-25 根修，commit 76a6bff）
- **根因**：Playwright sync 的 `_el.click()` 與 `page.evaluate()`（即 `execute_script`）在 dialog 未 dismiss 時會**無限卡住**（greenlet cross-thread 限制）。若在呼叫前設 `_next_dialog_no_dismiss = True`，`_on_dialog` callback 不 dismiss dialog → dialog pending → click/evaluate 等待頁面 settle → 永遠不返回（實測 40 分鐘 hang）
- **正確做法（已套用守則）**：
    1. **絕不**在 `element.click()` 或 `execute_script()` 前設 `_next_dialog_no_dismiss = True`
    2. 點擊前 reset `self.driver._last_dialog = None`，讓 `_on_dialog` 自動 dismiss
    3. 點擊後從 `_last_dialog.message` 讀取 alert 文字（`_on_dialog` 在 dismiss 前已設好 `_last_dialog`，dismiss 後 `.message` 仍可讀）
    4. 不使用 `WebDriverWait(EC.alert_is_present())`（`_ECShim` 未實作此方法，會 AttributeError）
- **已修位置**：`file_review_automation.py` 登入流程（login_btn.click()）、`try_click_check_btn()`（step 5 + retry loop alert reading）；commit `76a6bff`
- **`_next_dialog_no_dismiss = True` 已全數清除（2026-04-25）**：`file_review_automation.py`（login/try_click_check_btn/submit 路徑）與 `laf_automation_v2.py`（login/toPrevious/doFinish/doTempSave/save_btn 共 15 處）已全部改為「重置 `_last_dialog=None` + 讓 `_on_dialog` 自動 dismiss + 讀 `_last_dialog.message`」模式
- **live 驗收**：蘇建和 TPH 114重上更二95 → 登入成功（密碼到期 alert 正確從 `_last_dialog.message` 讀到後繼續）、token B60A4E 建立、screenshot 存檔（2026-04-25 04:03）；陳文明 1150128-I-011 結案 portal-draft → doTempSave 成功（`存檔成功!` Modal 確認）、2 份 PDF 上傳、DB 更新 `已結案，待送出`（2026-04-25 05:10）；pytest 1964 passed / 1 skipped / 1 pre-existing flake（隔離執行通過）

### 已遞委任模式 / 法扶模式（2026-04-22）
- **已遞委任（commit 08391f9）**：指令含「已遞委任」等關鍵字時跳過 `is_first_application()` 與 `_find_review_upload_files()`。觸發關鍵字：`已遞委任`、`已送委任`、`委任已送`、`委任已遞`、`不用上傳`、`無需上傳`、`跳過上傳`、`略過上傳`。修改：`file_review_automation.py` 步驟 7.5 雙層 guard、`skills/file-review-orchestrator/action.py` cmd_apply / parse_line_command、`api/pipelines/command_dispatch.py`。Live E2E：吳玉琳 HLD 115家救字第3號 → Ready，確認碼 BA7EF7。
- **法扶模式（commit bab642f）**：指令含「法扶」關鍵字時呼叫新 helper `_find_review_upload_laf_only()`，只搜尋並上傳法扶通知書，不上傳委任狀。搜尋優先順序：(1) `02_開辦資料/` — 律師已簽好名的接案通知書/開辦通知書；(2) `01_法扶資料/` — 准予扶助證明書；(3) 整個案件資料夾 fallback。banned terms：審查表、申請書、資力詢問表、預付酬金、案件概述單、委任。Live E2E：陳文明 ILD 115原訴36 → uploaded xhr_ok:200，result=Ready，確認碼 6CA939。
- **DC 閱卷確認碼與表單 select 補回（2026-04-22）**：`message_pipeline.py` 先用 `.review_submit_pending.json` 驗證閱卷 token 再背景執行 `confirm_apply`；`apply_for_review()` 改用 JS/native select helper 選取並在 postback 後補回 `ocrtid`/`sys`；`婚/家/親/護...` 自動推論 `sys=U(家事)`，AUTO 候選補齊 `V/U/I/K`；confirm 送出保留 `skip_upload/laf_only`。

---

## §5.3 LAF 系統 已完成 fix 詳細紀錄（2026-04-22 ~ 2026-04-26）

### LAF portal retry NAS↔Synology 雙向 fallback（2026-04-22，commit e0141e0）
- **根因**：`laf_orchestrator.py::_nas_satisfies_trigger()` 與 portal retry 預檢 guard 只用 `os.path.isdir(folder)` 判斷資料夾存在；若案件資料夾存在於 Synology Drive 但 NAS 不可及（或反之），判定失敗 → 跳過預檢 → 無限 portal 重試（陳玉梅 169 次）
- **修法**：新增 `_resolve_case_folder_with_fallback(folder)` helper，呼叫 `local_synology_path_candidates()` 嘗試所有候選路徑（NAS /Volumes/ 各版本 + SynologyDrive-homes + ~/SynologyDrive + ~/.magi_mounts/），回傳第一個實際存在的目錄；兩處判斷均改用此 helper
- **守則**：NAS 與 Synology Drive 是同一份資料的雙重存取點；任何路徑存在性檢查都應透過 `local_synology_path_candidates()` 嘗試兩端，不能直接 `os.path.isdir(原路徑)` 就放棄

### progress 流程零次數自動填 + 雙通道通知（2026-04-26 根修）
CLI 模式 progress portal-draft 在零次數欄位時 100% fail（portal `checkData()` reject）。修法：
1. 抽 `fill_noarrivereason_textarea` 共用 helper，closing 行為不變；progress workflow 加偵測 10 個次數欄位 → 自動填預設文案 + DC/TG 通知附零次數警告。
2. 修 Plan B 漏的 `notify→notify_admin` method 名 bug + 順手挖到 2026-04-18 起 progress 確認碼通知 `attachment` kwarg 也錯（使用者已 8 天沒收到此通知）。
3. `LAFNotifier.notify_admin` 從 TG-only 改為 **TG + DC 雙通道**；`_push_discord` 加 bot+channel_id 路徑（topic_key → `MAGI_DC_CHANNEL_<UPPER_TOPIC>` env，例 `MAGI_DC_CHANNEL_LAF_PROGRESS=1494521752062267584` 路由到 DC 進度回報頻道），fallback 既有 webhook。

驗收：9/9 unit tests + closing regression + 黃彩庭 1130619-T-027 force-zero live verify 確認 zero_fields_detected=["開庭次數","調解次數"] + TG 收到 [TEST 1/2/3] + DC 進度回報頻道收到 [TEST 3]。commits `733592c`/`4118eba`/`57ce367`/`38f446e`/`b82a5bf`/`234a828`。

### legal_aid_status 流轉：已報結 → 已結案 + 副狀態（2026-04-26 完成）
portal「待轉入/已轉入」表示事務所端工作已完成，DB 主狀態應直接顯示「已結案」，而非中介狀態「已報結」/「已報結（待轉入）」。修法：DB 新增 `legal_aid_approval_status`（暫存/待轉入/已轉入）+ `legal_aid_approval_checked_at`，`verify_portal_closing_status()` mapping 改寫為主+副狀態雙寫；`laf_handler.py` 中文 alias 改為 `報結/結案/撤回/撤案 → 已結案`（deprecated alias「已報結」保留至 2026-07-26）。commits `6faa45d`(feat)、`b4b1e21`(migrate)。schema ALTER 已執行 + migration `--apply` 跑完 `updated=4, errors=0`：陳明宗/邱淑萍/蕭仁俊「已報結→已結案+已轉入」、陳文明「已結案，待送出」補填副狀態「暫存」；DB backup `/tmp/cases_backup_20260426.sql`。

### progress 兩階段確認碼送出（Plan C，2026-04-26）
因進度回報 portal 結構限制（無「存檔」按鈕只能直接送出），portal-draft 模式無法留暫存讓律師補欄位。仿照 go_live 既有 confirm_token 機制：填表+截圖完成後 → 產生 6-hex token + register pending file → DC/TG 通知律師（含截圖 URL + token）→ 律師回覆 token → MAGI `cmd_confirm_progress` 再進 portal 真送出。實作：`laf_flow.py` 新增 `register_laf_progress_submit_pending` + `resolve_laf_progress_pending_token`（kind="laf_progress_submit" 嚴格與 go_live 區隔，互不誤吃）；`action.py` 新增 `cmd_confirm_progress`（雙重閘門：source 必須含 user/telegram/discord/line + `MAGI_LAF_ALLOW_PROGRESS_SUBMIT` 只在 confirm runtime 動態設）；`laf_orchestrator.py` progress action draft 完成後 register pending；`message_pipeline.py` 偵測 6-hex token 路由到 progress confirm。commits `a103c2e`/`aad1524`/`6de3ab0`/`d65824a`/`0a2a063`(補 laf_flow.py 進版控)。**原始驗收層級：測試**（36 tests passed：14 progress confirm token + 12 progress submit pending + 10 go_live regression；kind 嚴格分離雙向驗證 progress↛go_live、go_live↛progress）。

**2026-05-05 狀態修正**：本段原本保留「Live verify 待人工觸發」的舊狀態；後續較新的 `docs/CLAUDE_FIX_LOG_ARCHIVE.md` 已記錄黃彩庭 1130619-T-027 的 draft + submit 全鏈 live E2E 完成：法院函與書狀兩份文件上傳成功、`說明` 欄位截圖確認、`MAGI_LAF_ALLOW_PROGRESS_SUBMIT=1` portal-submit 呼叫 `doUpdate()` 成功、偵測到成功訊息、`ok=true`。因此目前狀態改為 **驗收完成（live E2E）**。詳見桌面計劃歸檔 `MAGI_v2_LAF_progress二階段確認碼_執行計劃_20260426.md` 與 `docs/CLAUDE_FIX_LOG_ARCHIVE.md` 約 line 987。

### `laf_flow.py` 補進版控（2026-04-26）
`.gitignore line 221 casper_ecosystem/` 規則的漏網檔，2026-04-26 commit `0a2a063` 用 `git add -f` 補進。其他 5 個 LAF 核心檔（laf_orchestrator / laf_automation_v2 / laf_nightly_audit / laf_handler / line_notifier）也是同樣方式 force-add 的 tracked 檔。**未來在 `casper_ecosystem/law_firm_orchestrators/` 新增 .py 檔必須記得 `git add -f`，否則只在本機 disk**。

---

## §5.5 Codex 全面下線 + Gemma E4B 蒸餾管線完整紀錄（2026-04-25 ~ 2026-04-26）

**背景**：使用者已採用 NVIDIA NIM 免費 API。原本週末「Codex 蒸餾判決」是用 OpenClaw Codex OAuth 摘要判決全文 → 蒸餾資料供 TAIDE LoRA 訓練。但 TAIDE 訓練 (`job_distill_train`) 早已 disabled，整套 Codex 線路與訓練目標脫節。

**設計／執行／驗收分工**：Opus 4.7 設計（計劃已歸檔至 `docs/archive/desktop_plans_20260426/MAGI_v2_Codex下線_Gemma蒸餾_執行計劃_20260425.md`）→ Sonnet 執行（三次 commit）→ Opus live 驗收。

### 已完成 patch（commits `332a38c` / `9b69833` / `93521a7` / `6cea321`）

1. **Phase A — `weekend_resummary.py` 改走 NVIDIA NIM 405B**
   - `_codex_summarize` → `_nim_summarize`（走 `InferenceGateway.chat(heavy=True)` → `heavy_fast_path` 強制 NIM 405B）
   - 移除 `_clear_codex_cooldown`（NIM 無 cooldown 概念）
   - 加 `RESUMMARY_BUDGET_CAP=300`（保留 200 給其他系統，NIM 日預算 500）
   - 連續失敗呼叫 `issue_tracker.log_issue`
   - 防呆：`provider != "nvidia_nim"` 即視為失敗，不接受 oMLX fallback
   - `INTER_REQUEST_DELAY` 5s → 1.5s
   - `reprocess_insights.py`：`_summarize_with_codex` → `_summarize_with_nim`

2. **Phase B — Codex Bridge / API 層 stub 化**（保留 module 與函式簽名避免下游 import 爆炸）
   - `skills/bridge/openclaw_codex_bridge.py`：949→stub；`feature_enabled=False`、`apply_manual_command/run_prompt={success:False, message:"Codex 已停用"}`、`public_status_report=停用訊息字串`
   - `api/domains/codex_flow.py`：三函式回 stub 訊息
   - `api/product_runtime.py`：`_normalize_codex_mode("codex") → "local"` 並記 logger.info；`DEFAULT_PROFILES.codex_mode` `auto`→`local`

3. **Phase C — Gemma E4B 蒸餾管線（首訓前 disabled）**
   - 新建 `scripts/nightly_distill_gemma.py`（仿 `nightly_distill_train.py` 但 base 改 E4B、加 E4B 日間視窗檢查 `07:00-21:50`、**不自動部署**，寫 `pending_deploy.json` 並 TG 通知手動指令；新增 `--deploy <version>` 獨立入口）
   - 新建 `scripts/train_gemma_e4b_lora.py`（LoRA rank=8/alpha=16/q_proj+v_proj，max_steps=200；用 `mlx_lm.lora` + `mlx_lm.fuse`，已驗 mlx-lm 識別 `model_type=gemma4`）
   - `skills/bridge/distill_collector.py`：加 `MAGI_DISTILL_TARGET` env switch（`gemma`/`taide`/`both` 三模式 `_paths_for(target)`），預設 `gemma`
   - 資料遷移：`taide-distill/raw_pairs.jsonl` → `gemma-distill/raw_pairs.jsonl`（cp 保留兩份）
   - cron：新增 `job_distill_train_gemma`（週日 11:00，**enabled=false**，`long_job=true, timeout_sec=5400`）；`job_distill_train` desc 加淘汰注記；`job_judgment_retry_evening` desc 改 NIM/oMLX

4. **Phase D — `.env` 變更（gitignored）**：`MAGI_DISTILL_TARGET=gemma` / `GEMMA_E4B_BASE_MODEL=/Users/ai/.omlx/models/gemma-4-e4b-it-4bit` / `GEMMA_DISTILL_DIR=/Users/ai/.omlx/training/gemma-distill` / `WEEKEND_RESUMMARY_BUDGET_CAP=300`

**Codex 字眼殘留**：grep 從 209 → 169（剩餘均為 stub docstring 與函式名本身，刻意保留）

**Sonnet 計劃外調整（已 review）**：
- `gw.inference_chat()` → `gw.chat()`（實際方法名，OK）
- `cron_jobs.json` 一度被 `git add -f` 進版控（Opus 已 revert：commit `6cea321`，因含 `last_run` runtime state 不適合版控，`.gitignore` 規則維持）
- `distill_collector.collect_summary_pair()` 預設 `source` 由 `openclaw_codex` → `nim_resummary`（caller 可覆蓋，無相容性問題）
- `nightly_distill_gemma.build_training_set()` 用 monkey-patch 覆蓋 `distill_collector` module-level 路徑（避開 TAIDE 版的路徑寫死，但這是限定於該腳本內，無 side effect）

### 驗收層級：驗收
- pytest：1984 passed, 0 新增 failure
- live E2E：`weekend_resummary.py --limit 3` → 3/3 都實際走 `route=nvidia_nim, model=meta/llama-3.1-405b-instruct`，PII scrub 啟動，1/3 成功蒸餾收集（pair #414 寫入 `gemma-distill/raw_pairs.jsonl`）；2/3 失敗為腳本自身 STRUCTURE_HEADERS 門檻擋下（與 NIM 無關）
- `taide-distill/raw_pairs.jsonl` mtime 維持 Mar 30（雙寫切換正確，預設只寫 gemma）
- `magi restart` 後 5002 status=`degraded`、5003=`ok`；degraded 原因為 24h 內既有 cron 失敗，與 Codex 下線無關

### 首訓已完成（2026-04-26 07:40 ~ 08:14，commit `66d00df`）
- Train: 1781s / 200 iters / 372 train + 42 eval samples / E4B base
- Merge: 30s dequantize OK
- Validate: **3/3 PASS**（chat-template + remove temp= kwarg 修復後）
- pending_deploy.json：`/Users/ai/.omlx/training/gemma-distill/pending_deploy.json`
- Adapter 148MB（保留），Merged 14GB（dequantized fp16）

**首訓過程踩到的雷（已修進腳本）**：
1. mlx-lm 新版把 LoRA 超參數搬到 YAML config（用 `-c`）— 移除 `--lora-rank` 等 6 個 CLI 參數，改寫 `lora_params.yaml`
2. `mlx_lm.fuse` `--de-quantize` → `--dequantize`
3. 新版 `generate(model, tok, prompt, verbose, **kwargs)` 不再支援 `temp=` 直接參數
4. Gemma 4 instruction-tuned 必須套 `tokenizer.apply_chat_template(...)` 否則只會複讀
5. `nightly_distill_gemma.py` 的 nohup launcher exit 後 main flow 仍在 subprocess 跑，最終 `_clear_training_lock` 在 finally 觸發；training lock 殘留時要手動 `rm static/training.lock`

### 部署決定：⛔ 不部署 v001（Gemma 4 thinking-channel 失控）

實際 sample 完整輸出後發現嚴重問題：模型對複雜 prompt（涉及法條查詢、概念定義）會吐 `<|channel>thought` 前綴 + **英文 reasoning trace**，而不是中文回答。三個量化等級對照（max_tokens=300）：

| 版本 | 損害賠償 | 刑法 339 | 善意第三人 |
|------|---------|---------|-----------|
| fp16 dequantized (14GB) | ✅ 中文 45 字 | ❌ 英文 thinking 1113 字 | ❌ 英文 thinking 1233 字 |
| 8-bit (7.5GB) | ✅ 中文 45 字 | ❌ 英文 thinking 764 字 | ❌ 英文 thinking 779 字 |
| 4-bit (4.0GB) | ❌ 英文 thinking 811 字 | ❌ 英文 thinking 786 字 | ❌ 英文 thinking 834 字 |
| base 4bit + adapter（不 merge） | ✅ 中文 36 字 | ❌ 英文 thinking 1132 字 | ❌ 英文 thinking 1242 字 |

**根因**：Gemma 4 是 thinking-capable model（內建 `<|channel>thought / final` 兩個 channel）。我們訓練資料 schema 是 `prompt → 中文判決摘要` 的單一通道對，**沒教模型抑制 thinking channel 或在 thinking 後接中文 final**。對複雜 prompt 模型仍走預訓練 reasoning path（英文，因 base 模型英文資料較多）。

**validation gate 已加強（2026-04-26）**：`train_gemma_e4b_lora.py::validate()` 不再只看長度；必須通過 `_validate_output_gate()`（拒絕 `<|channel>`/`<|channel>thought`、英文 thinking trace、簡體字、繁中含量不足、過短輸出）。驗證 prompt 會加入 `/no_think` 與「只輸出 final」指示；`nightly_distill_gemma.py` 若 validation gate 未通過，不得寫 `pending_deploy.json`，也不得通知可部署。

### 下一輪訓練改善方向（優先級排序）
1. **訓練資料加 thinking channel suppression**：validation prompt 已加 `/no_think`；下一輪資料集仍應評估在每筆 prompt 或 response schema 中加入 final-channel suppression
2. **累積至少 1000 對**（目前 414 對偏少，模型沒被充分校準）
3. **deployment gate 不可放寬**：未通過繁中 / channel / thinking-trace gate 的模型不可部署，即使簡單 prompt 看似正常
4. 評估改用 non-reasoning model（如 Mistral Small 24B 4bit）作 base，迴避 channel 問題

### 現階段保留物
- LoRA adapter (148MB)、fp16 merged (14GB)、8-bit (7.5GB)、4-bit (4.0GB) 全部保留作 evidence；不刪除（盤上 SSD 充裕）
- `pending_deploy.json` 已標記 `status=rejected` / `deploy_allowed=false`；`nightly_distill_gemma.py --deploy gemma-distill-v001` 會拒絕部署
- oMLX 仍跑原生 `gemma-4-e4b-it-4bit`（不變）
- `job_distill_train_gemma` 維持 `enabled: false`，等下輪訓練資料 schema 改善後再重訓再考慮

---

## CLAUDE.md 附錄 A 完整內容（2026-04-26 文件整理歸檔索引）

> 本段彙整桌面 / `docs/` 下「已驗收完成」之獨立 MD 計劃文件，原檔於本輪整理後已刪除；保留摘要於此作為唯一索引。

### A. 嚴格修復計劃（P0–P2 共 11 項）— 驗收
- 來源原檔：`Desktop/MAGI_v2_嚴格修復計劃_20260425.md`、`Desktop/MAGI_v2_驗收報告_OPUS_20260425.md`（已刪除）
- 驗收人：Opus 4.7（嚴格模式）；驗收結果：**11/11 PASS**
- 對應 commits：`f46497b`、`7cd3ebe`、`094c49b`
- 內容索引：
  - **P0-1** `job_insight_sync` `force_local`：`scripts/sync_insights_to_vectors.py` 加 `MAGI_INSIGHT_SYNC_FORCE_LOCAL=1` 預設，避免遠端 host 失敗即紅燈
  - **P0-2** `job_obsidian_ingest`：daily 模式 `MAGI_OBSIDIAN_OCR_FALLBACK=0`，掃描件改 `ocr_skipped`
  - **P0-3** cron 碰撞清零：52 jobs / 52 unique cron strings
  - **P1-4** Codex 5 個未提交檔已入 git；`audit_operational_hardening.py` 跑通
  - **P1-5** worktree 清乾淨；`.claude/worktrees/` 12 個空 stale 目錄已於 `032ddae` 清除
  - **P1-6** 筆錄 `_N` 重複處理：280 個檔散落 22 個 `.duplicates/` 目錄；16 筆 different_content 全保留原處（0 誤移）；PDF 內文 SHA256 抽樣 8/8 全 match
  - **P2-7** `/health` operational_health 子物件（cron_failures_24h / benchmark stale / degraded reasons）
  - **P2-8** PDFNAMER 99% 根因確認：cloud-only 樣本 fitz 讀不到，邏輯本身正確（archived golden 100%）
  - **P2-9** `_LONG_JOBS` 動態化：`timeout_sec` > `long_job=true` > legacy `_LONG_JOBS`
  - **P2-10** 三通道 E2E smoke：`PASS=16 / WARN=0 / FAIL=0`（Discord webhook + Telegram 已補環境變數相容）
- 紅線：`judicial_automation_v2.py` 因配合 `repair_transcript_filenames.py` 加了 8 行 `00000000` 邏輯處理（輕觸守則但屬必要 surgery）

### B. MAGI 商用化評估 — 驗收
- 來源原檔：`Desktop/MAGI_商用化評估總結報告_20260425.md`（已刪除）
- 結論：**內部生產可用 ✅ / 受控試營運 ✅ / 完全外部商用 SLA：尚未通過**
- 已達標：核心 runtime 全綠、`5002=operational` / `5003=ok`、全套 pytest `1981 passed`、三通道 smoke `PASS=16`、autopilot self-test ok、APE benchmark success、Obsidian daily-mode 10 件 7 秒 errors=0
- 對外商用前需補：(1) Obsidian daily 預設不跑 OCR 是有意取捨；(2) 長任務需觀察 3–7 天 cycle；(3) 不可逆送出仍要人工確認；(4) 多租戶資料隔離未設計
- 不建議自動化承諾：法扶二階段送出、大量掃描 PDF 自動 OCR、任意 portal 全自動送件、三通道必達 SLA

### C. 法扶 portal 流程歸檔（2026-03-26 起）— 驗收
- 來源原檔：`docs/CHANGELOG_20260402.md`、`docs/LAF_CLOSING_PORTAL_AUDIT_20260326.md`、`docs/LAF_CODE_CHANGES_20260326.md`（已刪除）
- 11 項程式碼修改全部已 committed；9 項表單欄位對照完成；MEMORY.md 已標註「不要再動」
- 仍保留之 runtime 參考資料：`docs/LAF_PORTAL_FIELD_MAP_20260326.md`、`docs/LAF_PORTAL_PROGRESS_FIELD_MAP_20260417.md`（欄位對照表，腳本仍會讀）

### D. 文件整理範圍外（保留不動）
- `docs/ENV_REFERENCE.md`、`ARCHITECTURE.md`、`API_CONTRACT.md`、`USER_GUIDE.md`、`OPERATOR_RUNBOOK.md`、`PRIVACY_POLICY.md`、`SECURITY_INTEGRATION_GUIDE.md`、`THIRD_PARTY_BOM.md`、`DATA_RETENTION_POLICY.md`：通用文件，繼續維護
- `docs/SETUP_SHORTCUTS_REBUILD_2026.md`：`/shortcut/*` API 參考（已驗收 commit `9c30eb3`），保留作為遠端觸發備用入口的 API 契約
- `docs/LAF_PORTAL_FIELD_MAP_*.md`：runtime 仍會讀的欄位對照
- `Desktop/AcroPDF_開發計劃.md`：與 MAGI 無關，留在桌面
- `Desktop/臺灣法庭外語通譯現況調查與檢討_中英對照.md`：翻譯交付成果，保留
- worktree `intelligent-brown-452a46`：含未合併 commits（`3accac0` text-layer fast-path / `9661e9a` 操作手冊重寫 / `69db59a` cron _LONG_JOBS 補入 / `68e9dbd` privacy / `8b66a6b` transcript-indexer timeout），未列入本輪刪除範圍

### E. 未完成 / 未驗證項目
- 已彙整至 `Desktop/MAGI_v2_未完成項目彙整_20260426.md`（同日另檔）。已完成的桌面計劃已移至 `docs/archive/desktop_plans_20260426/`；桌面只保留仍需決策、人工驗收或非 MAGI 的活文件。
