# MAGI V3 維修百科全書

**基準版本：** `v3-20260831-rc643-r75-hotfix7-r1`<br>
**來源 commit：** `29222c40cd5f898f27670c13feb4c134c751bdb3`<br>
**文件狀態：** active production；release manifest `96ab97392dfbdbf46cb895e92c0db725d80ef53202ad5568dad65bdf76cd7829`，formal gate `b6f29aeda6062654317231c2bbb73ee6d382bbaecd2bc88f84e51d3cfb89dbb6`<br>
**文件日期：** 2026-08-31<br>
**GitHub：** `WhaleChao/MAGI-public` / immutable commit `29222c40cd5f898f27670c13feb4c134c751bdb3`

> 本書的目標不是讓你背程式，而是讓你能從「現象」追到「入口 → owner → state → 外部邊界 → receipt → health」，並知道什麼可以安全修、什麼必須停手。全文不含密碼、Cookie、token、案件內容或可逆個資。

## 文件使用方式

1. 先查第 20 章的總決策樹，確認是功能故障、等待、降級、資料不一致或驗證器問題。
2. 到對應功能章找連動表與權威狀態。
3. 只讀蒐證後，再依第 21 章修復；不要先 kill、刪 lock、改 cron JSON 或清 checkpoint。
4. 任何原始碼修改都建立新 commit、新 release、新證據鏈；installed release 不就地修改。
5. 附錄的來源索引列出 SHA、行數與符號；完整內容以不可變 source commit 為準。

## 目錄

| 章節 | 頁 |
| --- | --- |
| [1. 閱讀方法、權威順序與安全界線](#ch01) | 3 |
| [2. 整體架構與功能連動總圖](#ch02) | 4 |
| [3. 程序角色、服務、埠與啟動順序](#ch03) | 5 |
| [4. 原始碼目錄、責任邊界與讀碼方法](#ch04) | 6 |
| [5. 請求路由、身分、授權與工具執行](#ch05) | 7 |
| [6. 排程、重試、checkpoint 與自然終局](#ch06) | 8 |
| [7. 法扶派案、附件、開辦與報結](#ch07) | 9 |
| [8. 閱卷、繳費憑證、下載與簽章對帳](#ch08) | 10 |
| [9. 案件、NAS、Google Drive 與雙邊映射](#ch09) | 11 |
| [10. OSC、日曆、待辦、帳務與債務文件](#ch10) | 12 |
| [11. PDF、OCR、筆錄、翻譯與知識庫](#ch11) | 13 |
| [12. 公開創作工具：Cookie Cutter 與影片工作室](#ch12) | 14 |
| [13. 本機模型、資源閘門與降級策略](#ch13) | 15 |
| [14. 通知、外網入口、TG/Discord 與安全邊界](#ch14) | 16 |
| [15. Menubar、NERV、Doctor、Guardian 與紅燈語意](#ch15) | 17 |
| [16. 狀態、鎖、owner、收據與證據鏈](#ch16) | 18 |
| [17. 不可變發行、LIVE 切換與自動回滾](#ch17) | 19 |
| [18. 備份、還原、災難復原與 GitHub 保存](#ch18) | 20 |
| [19. 測試、品質閘門與驗證器加速](#ch19) | 21 |
| [20. 故障排查總則與決策樹](#ch20) | 22 |
| [21. 分功能排查與排除手冊](#ch21) | 23 |
| [22. 已知故障、根因、修復與防回歸](#ch22) | 26 |
| [23. 日常維修、升級與自主演進守則](#ch23) | 33 |
| [附錄 A. 核心原始碼節錄與解讀](#appA) | 34 |
| [附錄 B. 全部 production 原始碼索引](#appB) | 55 |
| [附錄 C. 全部測試原始碼索引](#appC) | 110 |
| [附錄 D. 設定、Schema、前端與腳本索引](#appD) | 138 |
| [附錄 E. API 路由索引](#appE) | 156 |
| [附錄 F. 維修命令速查與名詞表](#appF) | 168 |


---

<a id="ch01"></a>
# 1. 閱讀方法、權威順序與安全界線

MAGI 的維修核心是『權威與證據』，不是畫面看起來像成功。讀取順序如下：

| 順位 | 權威來源 | 用途 | 不可替代原因 |
| --- | --- | --- | --- |
| 1 | active release marker | 目前真正生效的 release、root、transaction | 工作區 branch 或 cron 舊 SHA 不代表實際程序 |
| 2 | installed immutable manifest | 逐檔 hash/size、來源 commit | 防止就地修改與半套部署 |
| 3 | formal chain / cutover receipts | 品質、備份、static、install、prepare、rollback | 每一步都有 hash-bound 前提 |
| 4 | owner/lock metadata＋PID 實況 | 誰正在執行、是否為 canonical worker | lock 檔存在不等於 owner 活著；反之亦然 |
| 5 | checkpoint / terminal status | 進度、cache、cursor、風險計數 | 舊 terminal 或 copied JSON 不能證明本輪成功 |
| 6 | business/function/Doctor/guardian/Funnel | 面向使用者的整體健康 | 必須與同一 active transaction 綁定 |

**安全停手線：** owner 活著且 executable、argv、release root 都屬 canonical release 時，禁止 kill、清鎖或改 state；先讓其自然 terminal。只有精確證明 foreign/stale owner，且已有 rollback/重啟契約時，才可終止程序。

<a id="ch02"></a>
# 2. 整體架構與功能連動總圖

本版共索引 **2,004** 個檔案、**1,394** 個 Python 檔、**23,437** 個類別／函式／方法與 **361** 條靜態 Flask route。

```text
使用者 / cron / webhook
        │
        ▼
Gateway（5002/5003）── 身分、CSRF、節流、意圖與相容層
        │
        ▼
Control（8088）────── 工具契約、管理面、協調與健康入口
        │
        ▼
Supervisor ────────── 服務單例、cron、worker、重試與 quiesce
        │
        ├── 業務：法扶／閱卷／OSC／Drive／日曆／帳務
        ├── 文件：PDF／OCR／筆錄／翻譯／知識庫
        ├── 模型：oMLX／NIM／embedding／quality gate
        └── 維運：Doctor／Guardian／NERV／Menubar
        │
        ▼
read-back receipt → business/function health → 使用者通知
```

| 觸發 | 入口 | 協調 | 執行 | 完成證據 |
| --- | --- | --- | --- | --- |
| Web／Mobile／TG／Discord | api/server.py、api/webhooks/*、api/discord_bot.py | api/pipelines/* → api/orchestrator.py | 專用 skill / api/osc/* | durable notification / receipt / health |
| 法扶 Gmail | scripts/ops/laf_gmail_dispatch_scan.py | laf_automation_v2.py 郵件分類 | laf_portal_new_files_scan.py／laf-orchestrator | 附件歸檔＋業務健康 |
| 閱卷通知／繳費 | skills/file-review-orchestrator/action.py | file_review_receipts.py | 法院入口下載／上傳佇列 | signature receipt＋Menubar |
| Drive all-files | cron_service.py | scripts/drive_case_sync_worker.py | api/osc/drive_case_sync.py | checkpoint＋terminal outcome |
| 案件與 OSC | api/blueprints/osc_*.py | api/osc/* | NAS／MariaDB／calendar | read-back result＋business snapshot |
| PDF／OCR／筆錄 | skills/pdf-*、skills/documents/* | OCR queue／namer／bookmarker | NAS 文件與知識索引 | 品質收據＋自測 |
| Cookie Cutter | api/blueprints/cookie_cutter.py | skills/cookie_stl/* | 隔離子程序 | ZIP/STL attestation，零持久化 |
| 影片工作室 | api/blueprints/video_studio.py | magi_v3/video_autopilot_adapter.py | 固定上游 portrait normalizer＋本機 ffmpeg＋使用者素材 | 指令理解回顯、素材／plan hash、MP4 畫質 attestation，零外送與零持久化 |
| 判決趨勢 | api/blueprints/sentencing_trends.py | api/sentencing_trends.py | 本機裁判庫＋臺灣法律 MCP | 逐筆納入／排除理由、官方全文核對、量刑統計與穩定裁判頁連結 |
| 實務見解 | api/blueprints/legal_research.py | api/domains/judgment_flow.py | 本機裁判庫＋官方全文 MCP 補量 | 來源綁定摘要、官方 JID／網址／全文驗證與排除理由 |
| 健康與自修 | business_module_live_check.py | function_health_index.py／magi_doctor.py | magi_self_repair_guardian.py | 固定語意紅燈＋安全修復 |

**資料面與控制面分離：** API 回應不是完成證據；真正的外部寫入必須由專用 worker 執行，完成後回讀遠端或目標檔案，再產生去識別 receipt。

<a id="ch03"></a>
# 3. 程序角色、服務、埠與啟動順序

| 服務 | 角色 | 型態 | 埠／命令 | 責任 |
| --- | --- | --- | --- | --- |
| main_http | gateway | wsgi | 5002 | required |
| tools_http | gateway | wsgi | 5003 | required |
| website_admin | control | http_server | 8088 | required |
| discord | supervisor | process | {python} api/discord_bot.py | required |
| file_review_auto | supervisor | process | {python} skills/ops/file_review_auto_worker.py | required |
| heartbeat | supervisor | process | {python} skills/ops/heartbeat.py | required |
| legacy_background | supervisor | process | {python} magi_v3/legacy_background_service.py --legacy-root . | required |
| osc_shell_nas_helper | supervisor | process | {python} scripts/ops/osc_shell_nas_helper.py | required |
| menubar | supervisor | process | {python} gui/magi_menubar.py | required |

啟動與切換順序不是任意的：gateway 直接回應使用者，使用 Interactive QoS；control/supervisor 為 Background。切換時先由 audited engine 停 supervisor，等待 child tree/owner 釋放，再停 gateway/control、安裝新版本、啟動並驗證。回滾使用切換前封存 plist、static receipt 與 active marker。

Host singleton（MariaDB、NAS mount、RPC、oMLX text/embedding、input/memory/oMLX watchdog）不隨每個 release 重複啟動；錯把 singleton 當 release child 會造成雙實例與資料損壞。

<a id="ch04"></a>
# 4. 原始碼目錄、責任邊界與讀碼方法

| 區域 | 檔案數 | 責任 |
| --- | --- | --- |
| .github | 2 | 其他版本化內容 |
| api | 229 | HTTP、業務 domain、OSC、auth、routing、session、工具契約 |
| bin | 2 | 其他版本化內容 |
| configuration | 43 | 能力、模型、service、schedule、resource 與 schema |
| data | 1 | 其他版本化內容 |
| documentation | 26 | 其他版本化內容 |
| gui | 1 | Menubar 與人類可讀健康 |
| integrations | 10 | 其他版本化內容 |
| json | 5 | 其他版本化內容 |
| legal_aid_legacy_adapter | 13 | 法扶 portal 相容與既有流程 |
| migrations | 2 | 其他版本化內容 |
| mobile_app | 59 | 其他版本化內容 |
| operations | 386 | 部署、稽核、備份、維運與驗證 |
| resources | 3 | 其他版本化內容 |
| root | 19 | 其他版本化內容 |
| skills | 636 | 可執行能力與業務 worker |
| src | 10 | 其他版本化內容 |
| tests | 307 | 單元、合約、故障注入、LIVE adapter synthetic |
| third_party | 4 | 其他版本化內容 |
| v3_kernel | 64 | 不可變核心、ledger、cron、supervisor、health、release ownership |
| web_ui | 182 | templates/static 前端 |

讀碼順序：先找觸發入口，再找 domain/orchestrator，再找真正外部 I/O，最後找 receipt 與 health evaluator。不要只修畫面文案；若底層 receipt/schema 不完整，Menubar 綠燈反而是危險的假綠。

完整 machine-readable 索引：[`docs/MAGI_V3_原始碼索引_rc643.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/docs/MAGI_V3_原始碼索引_rc643.json)。

<a id="ch05"></a>
# 5. 請求路由、身分、授權與工具執行

HTTP 請求依序經過 server/app factory、auth/CSRF/request guards、route/domain、tool registry、專用 worker。會改變外部狀態的動作必須具備明確授權、idempotency key、bounded timeout、read-back proof。

| 層 | 主要原始碼 | 故障時先看 |
| --- | --- | --- |
| App / WSGI | api/app_factory.py、api/wsgi_server.py、magi_v3/gateway.py | port/listener、factory import、release ownership |
| Auth / CSRF | api/server_auth.py、api/authz.py、api/csrf_guard.py | 401/403、session、origin、CSRF token |
| Routing | api/routing/*、api/pipelines/* | intent、clarification、tool choice、no-guess gate |
| Tools | api/tools/*、api/tools_api.py | contract、timeout、async job、output envelope |
| Durability | api/durable_notifications.py、api/durable_rate_limit.py | outbox、retry、dedup、delivery receipt |

**排查 500/502：** 先 localhost 健康 → listener PID → installed root → gateway log 安全尾端 → factory import。不要先重啟所有服務；若只是工具 worker 失敗，整體 gateway 重啟會中斷無關請求。

<a id="ch06"></a>
# 6. 排程、重試、checkpoint 與自然終局

CronService 不是單純 crontab：它有 lane、共享容量、same-job coalescing、pending occurrence、retry、business recovery、owner lock 與結果語意。`command_sha` 是排程定義身分；真正執行版本仍須看 owner PID 的 executable/argv/release root。

| 物件 | 語意 | 維修重點 |
| --- | --- | --- |
| v3_pending_occurrence | 尚未完成或被 controlled shutdown 保留的 occurrence | 不可手動刪；新 supervisor 會用新 definition 重建 |
| v3_retry | 結構化失敗後 bounded retry | reason/label/attempt/timestamp/occurrence 必須 exact |
| owner metadata | 目前執行者與 lock 的公開身分 | PID 活性＋argv＋release root 一起驗 |
| checkpoint | seq、last_progress、hash cache、partial staging | 只讀安全欄；禁止輸出 case/path/token |
| terminal | chunk_completed 或 cycle_completed | fresh、cursor 正確、risk counters 全零 |

自然終局不是『整輪 221 案全部完成』才算：all-files 採公平單案 chunk；`before→after=before+1` 即可成為 fresh terminal，最後一案才 `total-1→0`。但必須同時 `case_complete=true`、checkpoint seq/hash cache>0、pending/partial/storage/collision/errors 全零。

**rc641 大型上傳修復：** NAS→Drive 的單檔與單輪上傳安全上限提高為 3 GB，仍使用 8 MiB resumable chunks、30 GB 磁碟門檻與 no-overwrite/no-delete 契約。執行器會先判斷單檔上限，再套用本輪 byte budget；因此超限第一檔會留下 `deferred_large_file` 的精確原因，不會再形成 `0 attempted + stopped_by_bytes` 的無效重試。排查時比較 `large_upload_deferred`、`stopped_by_bytes`、attempted/bytes 與 checkpoint cursor；不要手動清除 pending/retry。

<a id="ch07"></a>
# 7. 法扶派案、附件、開辦與報結

法扶流程分為 Gmail 事件分類、案件生命週期、portal 可下載清單、附件下載／歸檔、開辦、進度、報結。『審查結果／已轉入』只證明業務狀態，不證明附件已上架。

| 訊號 | 允許動作 | 禁止誤判 |
| --- | --- | --- |
| 正式派案通知 | 解析案號/當事人/類型，進入建案與附件流程 | 接案意願或補充資料不可當派案 |
| 審查結果／已轉入 | 更新狀態；等待內文明示或 portal listing | 不得直接標 needs_download |
| portal table 有該案 | 下載、驗證檔案、歸檔並回讀 | row parse 失敗不可當空清單 |
| portal empty | 健康空清單，沒有待下載 | 與 timeout/relogin_failed 分開 |
| 達 retry 上限 | 轉人工確認並停止盲重試 | 不得只清 queue 讓紅燈消失 |

排查附件：先看 Gmail classification receipt → portal login/session → listing diagnostic → row count/parsed count → download receipt → NAS archive → business health。不要代使用者申請案件，也不要把『已轉入』信件當附件通知。

<a id="ch08"></a>
# 8. 閱卷、繳費憑證、下載與簽章對帳

閱卷採雙側簽章對帳：入口 expected signatures 與本輪 processed＋verified-existing handled signatures 必須同一 canonical schema。只看數量 7/7 不夠，因為可能是不同 7 件。

| 欄位 | 規則 |
| --- | --- |
| portal_downloadable_count | type 必須是非 bool int 且 >=0 |
| expected/processed/verified/handled lists | lowercase 64-hex、排序、唯一；raw 必須等於 normalize 後結果 |
| declared handled | 精確等於 processed ∪ verified-existing |
| accounted | 只在雙側 contract 有效時算 expected ∩ handled |
| success | expected ⊆ handled、非 deferred、底層 success=true |

繳費憑證去重需綁『案件＋閱卷事件＋檔案 SHA』的私密 registry；同案不同閱卷時間不可互相跳過。若上傳佇列卡住，先查 portal lock owner 是否仍活著；合法 owner 就等待，foreign/stale 才依程序處理。

<a id="ch09"></a>
# 9. 案件、NAS、Google Drive 與雙邊映射

Drive 同步的安全原則：case identity 先解析、規劃與執行分離、任何本機內容比較都走 fingerprint-bound checkpointed hash、寫入前再驗來源、絕不以路徑相似取代內容證據。

| 階段 | 主要程式 | 證據／停止條件 |
| --- | --- | --- |
| 列舉 | scripts/drive_case_sync_worker.py | worker_kind=all_files、exact command |
| 案件解析 | api/osc/drive_case_sync.py | alias/exclusion/identity guard |
| 規劃 | build_file_sync_plan | zero-write on pending/collision/storage |
| 內容比較 | _checkpointed_local_md5＋DriveFileCheckpoint | fingerprint/cache/deadline |
| 執行 | download/upload no-overwrite | before/after stat、partial sidecar |
| 終局 | worker status＋outcome gate | fresh cursor、cache>0、risk=0 |

**映射錯誤處理：** 先區分同一來源 ID、相同 checksum/size、可 hash 的 NAS alias、不可 hash 的 Drive native file。只有證據足夠才能自動合併；distinct native IDs 無內容 proof 時維持 collision，不能猜。

<a id="ch10"></a>
# 10. OSC、日曆、待辦、帳務與債務文件

OSC 是案件管理與業務資料的整合面，並非單一檔案。案件、檔案、日曆、待辦、帳務、債務文件各有 domain 與 blueprint；跨域動作要先確認 canonical case identity。

| 功能 | 入口／domain | 外部邊界 | 完成證據 |
| --- | --- | --- | --- |
| 案件 | api/blueprints/osc_cases.py、api/osc/case_intelligence.py | MariaDB/NAS | read-back case/card |
| 檔案 | osc_files.py、document_reuse.py | NAS/Drive | hash＋case identity |
| 日曆待辦 | osc_gcal.py、calendar_event_time.py、calendar_sources.py | Google Calendar | event id＋semantic audit |
| 帳務 | osc_accounting.py、accounting_sheet_import.py | sheet/DB | import summary＋monthly bonus |
| 債務文件 | osc_debt.py、debt_document_generator.py | DOCX/PDF templates | required checklist＋download proof |

日曆故障優先檢查 token health、source mapping、timezone/期限解析、duplicate semantic key。不要直接刪 Google event；先由 audit 證明重複並使用專用 reconciliation。

<a id="ch11"></a>
# 11. PDF、OCR、筆錄、翻譯與知識庫

文件鏈通常是：取得來源 → identity/lock → OCR/文字層 → 命名/分類 → 書籤/版面 → 寫入新檔 → reopen/read-back → receipt → index。任何一步失敗都不得覆蓋原檔。

| 能力 | 原始碼 | 常見故障 |
| --- | --- | --- |
| PDF 命名 | skills/pdf-namer/* | OCR 空、case mapping、state path、watcher 重複 |
| PDF 書籤 | skills/pdf-bookmarker/* | 邊界、label、large volume |
| OCR | skills/engine/ocr/*、nas_pdf_ocr_worker.py | backend unavailable、queue lock、低品質 |
| 筆錄 | transcript-downloader/indexer、forensic verifier | partial retry、portal empty、filename identity |
| 翻譯 | skills/translator/*、heavy_translation_quality_live.py | 模型 provenance、術語、長文降級 |
| 知識庫 | skills/memory/*、obsidian/*、judicial cache | 重複、stale index、來源缺證 |

排查順序固定為 source bytes → parser/OCR → canonical identity → target lock → write temp → verify output → atomic replace。若只有畫面預覽成功而無 reopen/receipt，不能視為完成。

<a id="ch12"></a>
# 12. 公開創作工具：Cookie Cutter 與影片工作室

Cookie Cutter 僅接受 bounded image upload，於隔離子程序內建立 STL/ZIP。端點有尺寸、速率、timeout、RSS、child reap、ZIP parent 與幾何 attestation。LIVE 驗證只能用 synthetic 或使用者明確授權且 SHA 精確的本機圖片；不得持久化、不得外送。

| 步驟 | 檢查 |
| --- | --- |
| 輸入 | 格式、像素、multipart byte 上限、SHA |
| 輪廓 | 去噪、平滑、封閉、最小特徵厚度 |
| 幾何 | wall/base/height 尺寸、mesh manifold、non-empty |
| 封裝 | ZIP 唯一預期 member、parent/resource attestation |
| 資源 | 20 秒、384 MiB、2 slots；setrlimit 失敗即拒 |
| 隱私 | no persistence、no external、固定安全錯誤 |

粗糙成品先判斷是輸入鋸齒、輪廓 simplify 過強、平滑不足、列印切片參數或 STL 非 manifold。應用多個自製圖做一致性測試，而非只對單一圖片調參。

影片工作室位於同一個免登入的公開創作分類，但公開只表示不要求 MAGI 帳號，不代表解除安全邊界。它可接受 1～5 份本機圖片或短影片，也可只用文字分鏡。使用者的中文剪輯命令必須先被解析、回顯並確認；生成端會重新計算 edit-plan SHA，避免畫面說已理解、實際卻套用別的設定。不接受外部 URL，也不啟用上游更新器、CapCut 或發布平台。

| 影片階段 | 契約 |
| --- | --- |
| 來源 | video-autopilot-kit v0.21.1、固定 commit、MIT；只封存 portrait normalizer |
| 素材 | JPG／PNG／WebP／MP4／MOV；1～5 份、單檔 24 MiB、multipart 合計 64 MiB；magic bytes 後再以 Pillow／ffprobe 解碼驗證 |
| 命令 | 先回顯順序、轉場、運鏡、音訊四欄；未知、衝突、plan SHA 漂移一律拒絕，素材與文字模式使用同一計畫 |
| 字幕 | Pillow 產生繁中透明 PNG，避開本機 ffmpeg 缺少 libass 的能力差異 |
| 資源 | JSON 16 KiB、multipart 64 MiB、2/min、單一並行、70 秒、每程序 1.5 GiB、CPU/NOFILE 限制 |
| 程序 | spawn child＋獨立 process group；逾時 SIGTERM→SIGKILL→wait→absence proof |
| 輸出 | 唯一 H.264 1080×1920 video＋AAC audio、6/9/12 秒、30 fps、逐幕畫質取樣與 bytes/SHA/ffprobe read-back |
| 隱私 | 0600、O_EXCL/O_NOFOLLOW 暫存；工作目錄只活在單次 request，回傳或失敗後刪除，不外送、不自動發布 |

若影片端點失敗，先讀固定 error code：輸入錯誤回 400/413；節流或忙碌回 429；engine、timeout、資源或 cleanup 失敗回 503。不可把公開端點的 engine 失敗誤當成外網／Gateway 全線故障。

<a id="ch13"></a>
# 13. 本機模型、資源閘門與降級策略

| 模型 | 角色 | 啟用條件 |
| --- | --- | --- |
| gemma-4-e4b-it-4bit | stable_local | registry allowlist |
| gemma-4-26b-a4b-it-4bit | heavy_local_moe | min_disk_free_gb=70; min_free_plus_inactive_gb=8; max_swap_used_gb=20; allowed_resource_levels=['normal']; require_model_live=True |
| gemma-4-12B-it-4bit | day_primary_local_unified | runtime_overlay=gemma4-unified; wrapper=~/.omlx/bin/omlx-gemma4-unified-serve; min_disk_free_gb=80; min_free_plus_inactive_gb=8; max_swap_used_gb=20; allowed_resource_levels=['normal']; require_model_live=True; require_tool_call_gate=True |
| gemma-4-31b-experimental | experimental_dense_local | registry allowlist |
| modernbert-embed-4bit | embedding_local | registry allowlist |
| Phi-4-mini-instruct-4bit | verify_sidecar | registry allowlist |
| SmolLM3-3B-4bit | crosscheck_sidecar | registry allowlist |
| nvidia-nim-heavy-non-china | cloud_heavy | registry allowlist |

E4B 是穩定降級，不等於必然故障。26B/12B 需要 model live、磁碟、free+inactive、swap、resource level、overlay/tool gate 同時安全。強迫大型模型在 24GB unified memory 啟動可能讓整機 swap/OOM，反而使所有功能紅燈。

排查：`model registry → active model probe → resource view → choose_model_for_request → decision_summary`。只有先清理經核准的 cache/evidence 垃圾並恢復資源後，才重新評估升級；不得刪 immutable release、最新 rollback 或唯一 evidence。

<a id="ch14"></a>
# 14. 通知、外網入口、TG/Discord 與安全邊界

通知與外網是最後一公里，功能本體成功不等於訊息已送達。durable outbox、provider response、delivery receipt、Funnel/route health 必須分開。

| 通道 | 程式 | 排查 |
| --- | --- | --- |
| Telegram | api/webhooks/telegram.py、durable_notifications.py | token health、topic/channel、outbox、provider error |
| Discord | api/discord_bot.py | supervisor child、gateway intent、rate limit |
| LINE compatibility | api/line_compat.py | legacy route、auth、delivery response |
| Funnel/Tailscale | tailscale_funnel_healthcheck.py | public URL、local upstream、no-store、TLS |
| Gmail/Drive/Calendar | 各專用 OAuth client | token refresh、scope、canonical credential path |

外網斷線時不要把所有業務功能判成失敗；應呈現 upstream_unavailable/waiting，保留 durable work，恢復後 bounded retry。驗證時不可將 token、URL query secret 或 provider raw body寫入公開 receipt。

<a id="ch15"></a>
# 15. Menubar、NERV、Doctor、Guardian 與紅燈語意

| 層 | 回答問題 | 原始碼 |
| --- | --- | --- |
| Business health | 業務結果是否真的完成？下一步是什麼？ | business_module_live_check.py、business_readiness_snapshot.py |
| Function health | route/skill/contract 是否可用？ | function_health_index.py |
| Doctor | 程序、埠、依賴、磁碟、模型、launchd 是否正常？ | scripts/magi_doctor.py |
| Guardian | 能否做安全的自動修復？ | magi_self_repair_guardian.py |
| NERV/Menubar | 如何向人類呈現 ok/waiting/degraded/attention/failed？ | health_presentation.py、magi_menubar.py |
| Process monitor | 核心、真實 worker、孤兒 ancestry、持續殭屍與重複群組是否一致？ | magi_v3/process_monitor.py → web_runtime.py／magi_menubar.py |
| Funnel | 外部入口是否到達正確 release？ | tailscale_funnel_healthcheck.py |

紅燈不是『請重啟』；先讀 reason_code/next_action/evidence age。waiting 表示系統有安全續作路徑；attention 表示需人類資料或入口處理；failed 才是本輪終局失敗。不得為消紅燈而刪 state。

### Golem／MENUBAR 共用的程序語意

| 欄位 | 唯一正式定義 | 常見誤解 |
| --- | --- | --- |
| core_count | 非 shell -c 包裝、命中 daemon REAPER_NEVER_KILL 的實際程序 | 命令文字提到 server.py 不代表真核心 |
| worker_count | argv0 是 Python/PyPy 且命中專用 worker script 的非 Z 程序 | zsh -lc 內文含 action.py 不算 worker |
| orphan_count | 真 worker 的 ancestry 在遇到 canonical supervisor/cron/core 前先抵達 PID 1 或斷鏈 | 只看 worker 直接 PPID 會漏掉 orphan shell 下的 child |
| zombie_count | MAGI 程序樹內同一 PID/PPID 的 Z state 持續至少五秒 | 瞬間 exit→wait 不亮紅燈 |
| duplicate_groups | 真 worker 的完整 command 完全相同且同時超過一個 | launcher 與 child 命令不同，不是假重複 |
| anomaly_count | 孤兒 PID 與殭屍 PID 的聯集，再加 duplicate group 數 | 這是異常項目，不是所有程序總數 |

兩個 UI 都必須呼叫 `magi_v3.process_monitor.collect_process_monitor()`；任何一端自行重新解讀 `ps` 都視為回歸。讀取失敗必須顯示 attention，不能以零項假綠。

<a id="ch16"></a>
# 16. 狀態、鎖、owner、收據與證據鏈

MAGI 的狀態檔分三類：mutable runtime state、owner/lock metadata、immutable evidence。前兩者會變，最後一類只能新增。任何 validator 都要防 symlink、非 regular file、未知欄位、bool 冒充 int、舊 receipt、錯 transaction、copied JSON。

| 資料 | 允許操作 | 禁止 |
| --- | --- | --- |
| owner/lock | 只讀核 PID/exe/argv/root；owner 退場後由官方 cleanup | 手動 rm、看到 PID 就 kill |
| checkpoint | 讀 safe counters；由 worker atomic write | 改 seq/cursor/cache 造成功 |
| cron state | 官方 scheduler/marker API | 直接編 JSON、清 retry/pending |
| receipt | exclusive/atomic、hash-bound、append-only | 覆寫舊成功、混用舊 release |
| active marker | ActivationTransaction | 手動改 symlink/JSON |

<a id="ch17"></a>
# 17. 不可變發行、LIVE 切換與自動回滾

正式發布鏈：clean source → changed-module source gate → sealed bundle/privacy → host-outer full quality（同一 immutable inputs 僅一次）→ backup/actual restore/independent restore → static stage/restore → install inactive → render/audit → private prepare/formal-chain → wrapper review → cutover → core post。business/function/Doctor/guardian/Funnel/Drive/portal/MCP/benchmark 改為獨立背景 receipt，不得串成單一全域同步 gate。

切換必須在 rollback envelope 內：驗 old release 與 durable work eligibility；停 supervisor 並 quiesce；保存 old bytes；安裝/啟動新；active marker atomic commit；post/health 失敗就 cleanup candidate、restore old、start old。不得再次執行已成功的 live_upgrade。

| 工件 | 保證 |
| --- | --- |
| release-manifest / COMPLETE | sealed source 的逐檔身份 |
| formal-chain | 32 個品質/備份/static/install/prepare/rollback artifacts |
| deploy manifest | 角色、plist、installed root、external inputs |
| active marker | 唯一 active release＋transaction |
| post receipt | Web/Funnel/STL/legacy absence |
| health receipt | business/function/Doctor/guardian/Funnel |
| Drive outcome | fresh terminal/cursor/hashcache/zero risk |

<a id="ch18"></a>
# 18. 備份、還原、災難復原與 GitHub 保存

備份不是只有 tar 成功：要做 actual restore drill 與 independent restore，並確認 DB、static receipt、active marker、plist、mutable state 的 byte/schema。回滾材料必須在切換前封存，不能在失敗後臨時從 candidate 猜。

GitHub 是版本保存與協作，不是 LIVE runtime 備份。公私庫都不存 token、Cookie、案件內容或 runtime DB；私庫原 MAGI-v2 已原地更名 MAGI-v3，歷史保留。發布分支保存 privacy-filtered source、手冊與不可逆雜湊。

災難復原順序：停止寫入 → 封存現況 evidence → 驗備份 SHA → isolated restore → schema/integrity check → 啟動單一 release → health → 恢復排程；不得直接把舊 DB 複製到正在寫入的 runtime。

<a id="ch19"></a>
# 19. 測試、品質閘門與驗證器加速

測試分層：unit → contract → adversarial/fail-closed → offline integration → isolated LIVE → maintenance cutover → post-cutover。執行邊界再分成「同步核心 gate」、「變更模組 gate」與「獨立背景健康」。驗證慢的主因是同一 nodes 在 focused/formal 重跑，或把外部網路、portal lock、Drive 長任務與 daily benchmark 錯串成整版 gate。

| 變更風險 | 最低驗證 |
| --- | --- |
| 純文件 | render/links/privacy/audit |
| 純 presentation | formatter tests＋payload adversarial＋business snapshot |
| 業務 parser/receipt | focused full file＋schema negatives＋PII scan |
| 外部寫入 | no-write synthetic＋idempotency＋read-back＋rollback |
| scheduler/Drive | owner/occurrence/retry/checkpoint/cursor/terminal adversarial |
| deploy/cutover | fresh full chain＋independent review＋LIVE post/health |

加速原則：測試選擇 manifest 化、nodeid 精確；promotion precheck 不得重跑 formal manifest 已覆蓋的 node。formal PASS 只有在 source commit、release/suite manifests、runtime、nodeids、test-source hashes、resource policy 與 runner SHA 全部 byte-identical 時才能沿用；任一變動即重驗。Seatbelt child rc71 改由 host-outer runner，不能跳過 sandbox。

<a id="ch20"></a>
# 20. 故障排查總則與決策樹

```text
看到紅燈／失敗
  ├─ active release/transaction 不符？ → 停止，先修 deployment binding
  ├─ owner 正在跑？
  │    ├─ canonical owner → 讀 checkpoint，等待自然 terminal
  │    └─ foreign/stale → 精確核 PID/PGID/argv，再走受控終止/rollback
  ├─ receipt stale/缺失？ → 查 owner/job terminal，不可複製舊 receipt
  ├─ reason=waiting/degraded？ → 查 next_action 與資源/入口，非立即故障
  ├─ external unavailable？ → 保留 durable state，修 token/network/portal
  ├─ risk/pending/collision>0？ → 查 identity/hash/storage，不可強行清零
  └─ source exception？ → 建新 commit＋focused/adversarial＋fresh release
```

每次事件建立一張維修紀錄：時間、active release、觸發、使用者症狀、owner、safe counters、第一個可信錯誤、採取動作、read-back、receipt SHA。禁止記錄案件內容、路徑、token。

<a id="ch21"></a>
# 21. 分功能排查與排除手冊

### 服務無法開啟

1. 讀 active marker/transaction

2. lsof 查 5002/5003/8088 owner

3. launchctl print 三角色

4. 核 executable/working directory

5. localhost health

6. 只重啟故障角色；若 binding 錯走 rollback

### Drive 長時間 running

1. 核四 owner metadata

2. 確認 canonical worker argv/root

3. 讀 seq/last_progress/hash cache/staging bytes

4. 有進度就等待

5. 無進度查 storage/token/deadline

6. owner 退場後才跑 outcome gate

### 法扶附件紅燈

1. 確認郵件類型不是單純已轉入

2. 查 portal session/listing status

3. table/empty/timeout 分流

4. 核 retry 上限與 deadline

5. 下載後驗 archive receipt

6. 人工確認案修資料或登入，不清 queue

### 閱卷 7/0

1. 讀 expected receipt

2. 讀 handled receipt

3. 比較 signature set hash/fingerprint

4. 找 result/result_text/row revision

5. 重跑 canonical probe

6. 雙側 exact 才標綠

### Cookie STL 粗糙或斷壁

1. 核原圖解析度/鋸齒

2. 跑 seeded synthetic 外框與內圖案

3. 驗 float32 serialized mesh

4. 驗單一連通殼與頂層2條封閉環

5. 確認輪廓誤差<=0.15 mm及最小特徵

6. 實際 slicer 預覽

7. 不以單張特例降低安全 gate

### 判決趨勢候選未納入／官方連結不可用

1. 確認第一階段 query 以案由發現候選

2. 逐筆讀 exclusion_codes／exclusion_reasons，不可用靜默 continue

3. 核官方 JID、FJUD URL 與 URL 內 JID 是否一致

4. 核全文長度、主文、簽署區、附表與判決日期

5. 以簽署區比對 requested/actual 法官並明列差異

6. 用民國年月日選單重現日期篩選；通過者才納入統計

### 實務見解夜間數量不足

1. 先區分 source_remaining 與 local backlog

2. 若 source remaining>0 且本機 pending=0，應為 SOURCE_PULL_CATCHING_UP 而非全綠

3. 核既有 daily crawl 的 bounded MCP gap fill 是否啟用

4. 逐筆只接受官方 JID／URL／全文交叉驗證

5. 丟棄 provider summary，從官方全文重建 source-bound summary

6. 核 provider daily remaining 與預估完成天數，不把 scheduler selection 當 provider 額度

### E4B 降級

1. 查 active models

2. 查 disk/free+inactive/swap/resource level

3. 確認 12B/26B gates

4. 只清可刪 cache/evidence

5. 重新 model probe

6. 仍不足就保留降級，勿硬啟動

### TG/外網故障

1. localhost 功能先驗

2. token health

3. outbox pending

4. provider DNS/TLS/response

5. Funnel upstream/no-store

6. 恢復後 official retry，不直接重送未知副作用

### 磁碟不足

1. 先列分類與最後使用時間

2. 保留 active/rollback/latest evidence

3. 刪已核准 A/B/C cache、舊 worktree、render temp

4. 重算 free space

5. 跑 model/resource/health

6. 留下清理 receipt

### Golem／MENUBAR 程序數不一致

1. 確認兩端 active release/transaction 相同

2. 讀 shared summary 的 orphan/zombie/duplicate/anomaly

3. 以 ps 核 PID、PPID、PGID、stat、actual worker argv

4. 沿 parent chain 找 canonical supervisor/cron/core

5. shell -lc launcher不可當 worker，Z 必須持續五秒

6. 修 shared classifier 並跑兩端 summary equality，禁止只改 UI 文案

### HTML 手冊表格文字被遮住

1. 用實際瀏覽器在 1440px 與 390px 重現

2. 量測 document/table/cell 的 clientWidth 與 scrollWidth

3. 確認 table-layout、min-width、overflow-wrap、word-break

4. 修 generator 而非直改 generated HTML

5. 重建 HTML/PDF/manual_assets

6. 要求所有表格 overflow=0 且 clipped cell=0

<a id="ch22"></a>
# 22. 已知故障、根因、修復與防回歸

以下登錄表是維修時最重要的防回歸知識。『修復』不代表可以刪掉 guard；相反地，對應 negative tests 必須永久保留。

### F-001｜Codex 內 formal test 立刻 rc71

- **根因：** Codex 已在 Seatbelt 內，再建立第二層 macOS Seatbelt 失敗；測試入口甚至未執行。
- **修復：** 改由 host-outer hash-bound runner 執行；不得把 rc71 當產品測試失敗，也不得弱化 Seatbelt gate。
- **驗證：** runner receipt 必須 exact/full、marker 存在、source/manifest SHA 相符。
- **原始碼／證據：** `scripts/v3_release_gate.py；formal runner evidence`

### F-002｜Gateway 回應約 6 秒，shell 正常

- **根因：** 三角色一律綁 launchd Background QoS，使 HTTP gateway 被降優先。
- **修復：** gateway=Interactive，control/supervisor=Background；cookie 子程序資源上限不變。
- **驗證：** rendered plist 三角色 ProcessType 精確，gateway LIVE 延遲與資源界線均通。
- **原始碼／證據：** `scripts/v3_deploy_prepare.py`

### F-003｜Drive checkpoint phase=scan_plan 且 hash cache=0

- **根因：** duplicate/same-content 兩條路徑直接 local_file_md5，繞過 DriveFileCheckpoint，deadline 又被 generic except 吞掉。
- **修復：** 全部共用 checkpointed MD5；DriveCaseSyncDeadline 先行 re-raise；fingerprint 變更才重算。
- **驗證：** 二次規劃零新增 MD5、cache>0、deadline 不前進 cursor。
- **原始碼／證據：** `api/osc/drive_case_sync.py；magi_v3/drive_file_checkpoint.py`

### F-004｜大量 semantic collision 但沒有內容證據

- **根因：** NAS alias 沒有 MD5，僅因 NFKC/casefold 同桶便被當成真衝突。
- **修復：** 對 local bucket 使用 bounded checkpointed MD5；同內容選 deterministic representative，異內容才保留人工確認。
- **驗證：** 同內容解衝且 cache>0；異內容、native distinct-ID 仍 fail closed。
- **原始碼／證據：** `api/osc/drive_case_sync.py`

### F-005｜local hash/storage 失敗後仍可能規劃寫入

- **根因：** duplicate lookup 回傳 local_hash_failed，但後續 download/upload planning 未被一併抑制。
- **修復：** 集中 suppress_case_write_actions，清空下載/上傳 action 並固定 cursor；retryable 與 hard failure 分流。
- **驗證：** timeout/storage/hard error 均零 transfer；partial failure cursor 不前進。
- **原始碼／證據：** `api/osc/drive_case_sync.py`

### F-006｜Drive outcome gate 對假資料放行或對真 terminal 誤拒

- **根因：** validator 使用 subset、int(bool)、虛構 aggregate failed，且未綁 canonical status/worker/cron。
- **修復：** exact schema、strict non-bool int、真 chunk/cycle cursor、canonical path、worker/snapshot/command 三重綁定。
- **驗證：** unknown/raw/bool/jump/early wrap/old release 全拒；真 terminal 通過。
- **原始碼／證據：** `evidence drive_outcome_gate；scripts/drive_case_sync_worker.py`

### F-007｜每兩分鐘 Drive running，LIVE preflight 永遠等不到 idle

- **根因：** preflight 在 supervisor 停止前要求 Drive idle，與高頻排程形成活鎖。
- **修復：** 切成 pre-quiesce 身分驗證與 supervisor unload 後 post-quiesce gate；由既有 rollback envelope 保護。
- **驗證：** original cleanup→post gate→install；失敗 restore/start old；不直接 kill child。
- **原始碼／證據：** `scripts/v3_cutover/*；release live wrapper`

### F-008｜安全的 Drive owner 被誤判 foreign

- **根因：** ps command 字串會破壞含空白 argv；lsof 有多個 ftxt；wrapper exec 後三層 wrapper 不再出現在 argv。
- **修復：** KERN_PROCARGS2 結構化 argv＋嚴格 lsof first ftxt；由 sealed cron 最後 -- 推導 exact worker argv。
- **驗證：** 實況 owner argc/worker/release 通過；missing/reorder/extra/foreign 全拒。
- **原始碼／證據：** `release live wrapper；skills/ops/cron_scheduler.py`

### F-009｜排程 handoff 因固定 occurrence/reason tuple 被拒

- **根因：** evidence wrapper 寫死某次 occurrence，並把 storage rc0 誤綁 process_interrupted/143。
- **修復：** 動態封存當下 exact occurrence digest；依 sealed business recovery labels 驗證 reason，支援合法 storage_recovered 派生票。
- **驗證：** ID 輪替、raw extra、錯 label、bool rc 拒；current storage tuple 通。
- **原始碼／證據：** `magi_v3/business_recovery.py；skills/ops/cron_scheduler.py`

### F-010｜閱卷入口 7 件、驗證 0 件

- **根因：** raw result 與 public result_text 選取順序不同；renderer 文案差異造成簽章不一致。
- **修復：** canonical content marker 改為 result_text→result→row_text；雙側 canonical signature 對帳。
- **驗證：** result 同義欄相等、真正 revision 變更必換簽章。
- **原始碼／證據：** `magi_v3/file_review_receipts.py`

### F-011｜閱卷 invalid/duplicate/uppercase hash 仍顯示成功

- **根因：** normalize 後才驗，靜默丟棄非法元素；handled declared list 未被精確驗證。
- **修復：** 四個 raw list 必須等於 normalized；handled=processed∪existing；count strict int。
- **驗證：** invalid extra、duplicate、uppercase、non-list、bool/negative count 全拒。
- **原始碼／證據：** `skills/file-review-orchestrator/action.py`

### F-012｜沒有待下載檔案卻顯示上輪失敗

- **根因：** 健康層把 0/0 reconciliation 或待確認狀態解讀成失敗，Menubar 優先級不正確。
- **修復：** attention 優先，合法 zero receipt 顯示正常；未知 reason 固定安全文案。
- **驗證：** ok+0/0 正常；不完整 receipt 維持紅燈。
- **原始碼／證據：** `scripts/ops/business_readiness_snapshot.py；gui/magi_menubar.py`

### F-013｜法扶『已轉入／審查結果』被誤認已有附件

- **根因：** 只看主旨，未區分審查結果通知與入口實際可下載清單。
- **修復：** 審查結果主旨本身 needs_download=False；須由內文明確指示或官網 listing 證明。
- **驗證：** 空 listing 與 listing timeout 分離；無附件不得盲重試/建案。
- **原始碼／證據：** `laf_automation_v2.py；laf_portal_new_files_scan.py`

### F-014｜法扶附件重試超上限仍盲跑／紅燈無解

- **根因：** 業務狀態把 portal 尚未上架、登入失效、資料錯誤混為同一 retry。
- **修復：** 期限內 bounded retry；達上限轉人工確認；登入/入口/案件資料分開呈現。
- **驗證：** 每案 next action 明確；人工確認案不繼續盲試。
- **原始碼／證據：** `scripts/ops/laf_portal_new_files_scan.py；business_module_live_check.py`

### F-015｜繳費憑證顯示已上傳過但其實是不同閱卷

- **根因：** 去重鍵曾過度依賴案件或舊 receipt，未綁精確 payment event。
- **修復：** 只接受 exact v2 payment event＋檔案 SHA 的 canonical 私密 registry；輸出只留 opaque digest。
- **驗證：** 不同事件不得互相跳過；同一事件重送才 idempotent。
- **原始碼／證據：** `file-review payment registry / evidence payment gate`

### F-016｜Cookie Cutter 表面粗糙、外壁斷點，或只有封閉外框時被拒絕

- **根因：** 舊契約只驗每條邊恰有兩個面；數個各自封閉的碎片仍可能假扮 watertight。float64 建模通過後轉成 STL float32 也可能讓近鄰頂點合併而產生切片裂縫；0.35 mm 輪廓門檻亦可能肉眼可見稜角。
- **修復：** 封閉外框建立單一 annular cutter；外壁必須是單一連通實體，最高層恰一個連續面與兩條封閉邊界。STL float32 座標再驗一次 manifold／方向／連通性；輪廓誤差收緊至 0.15 mm。只有與外框分離的內部圖案才建立鏡像 stamp，且同樣驗封閉頂面。
- **驗證：** 附圖凹形框實測 0.112995 mm、1 個頂面／2 條邊界；12 組隨機外框與內圖案、斷裂雙殼反例、薄線與空框全部通過。正式 runtime 的切模／端點／資源／路由聚焦回歸 73/73。
- **原始碼／證據：** `api/blueprints/cookie_cutter.py；skills/cookie_stl/*；tests/test_cookie_stl.py`

### F-017｜Cookie 子程序資源限制失敗仍繼續生成

- **根因：** setrlimit ImportError/OSError/ValueError 曾 silent pass。
- **修復：** child 回固定 resource_error，engine 不執行；parent 精確 IPC schema；finally reap。
- **驗證：** engine_calls=0、無產品 bytes、child_reaped、leaks=0。
- **原始碼／證據：** `api/blueprints/cookie_cutter.py`

### F-018｜文字推理顯示 E4B（預期 26B）

- **根因：** 不是單一故障；可能是磁碟、free+inactive、swap、resource level 或 26B 未 live 的安全降級。
- **修復：** 依 model registry gate 逐項檢查；清理經核准工程垃圾後重新 probe，不強迫載入導致 OOM。
- **驗證：** decision_summary 明列 gate reason；26B 僅在全條件安全時啟用。
- **原始碼／證據：** `api/model_router.py；config/model_registry.json`

### F-019｜健康紅燈只因 legacy lock dir 不存在而漏查 owner

- **根因：** owner validator early return，未繼續掃 legacy PID。
- **修復：** lock glob 可為空，但 legacy paths 必查；persistent owner/calendar owner 使用 exact schema/argv allowlist。
- **驗證：** missing dir＋live foreign PID 必紅；合法 persistent owner 綠。
- **原始碼／證據：** `refresh_live_health evidence；scripts/ops/audit_operational_hardening.py`

### F-020｜健康 audit 被 inherited PYTHONPATH 指到 source/evidence

- **根因：** subprocess 繼承 hostile environment，可能用錯版本。
- **修復：** 執行前綁 installed root、manifest、cron snapshot SHA，覆寫 cwd/PYTHONPATH/MAGI_ROOT。
- **驗證：** hostile env 測試仍只載 installed rc release。
- **原始碼／證據：** `release refresh_live_health wrapper`

### F-021｜formal bundle privacy gate 因測試 fixture 絕對路徑失敗

- **根因：** scanner 將測試中的 workstation-style literal 視為私有路徑。
- **修復：** fixture 以 runtime concatenation 建構，仍測 absolute path 但不把私有 literal 放入 bundle。
- **驗證：** fresh privacy gate violations=0。
- **原始碼／證據：** `tests/v3/test_change_scope.py；test_validation_router.py`

### F-022｜測試工作樹產生 casper.log，focused gate 失敗

- **根因：** api.orchestrator import-time handler 在 MAGI_AGENT_DIR 未隔離時落 root。
- **修復：** 測試明確綁 tmp agent dir；fixture 覆寫 hostile inherited env；source root 不產 log。
- **驗證：** tracked/index clean、forbidden untracked=0。
- **原始碼／證據：** `api/orchestrator.py；tests/test_admin_runtime_blueprint.py`

### F-023｜post-cutover JPG 驗證無法執行

- **根因：** wrapper 只綁 SHA，原始輸入 bytes 已不存在；視覺相似或重編碼不能替代。
- **修復：** 恢復 byte-exact 原檔後先驗 SHA，再僅送 localhost；缺 bytes 時維持 fail closed。
- **驗證：** input SHA 精確、regular non-symlink、no persistence/no external。
- **原始碼／證據：** `post_cutover evidence；cookie endpoint`

### F-024｜GitHub 發布混入案件／手機格式資料

- **根因：** 即使私有 repo，也不應保存可逆個資或 runtime dataset。
- **修復：** 公私版都跑 strict audit；個資資料集排除；只保存不可逆 release hash 與文件。
- **驗證：** public/private 0 errors、0 warnings；branch SHA 遠端核對。
- **原始碼／證據：** `scripts/public_release_audit.py；PUBLIC_RELEASE.json；PRIVATE_RELEASE.json`

### F-025｜Menubar 顯示紅燈但底層功能其實正常

- **根因：** presentation 把 stale receipt、waiting、degraded、failed 混為同一文字。
- **修復：** 健康 state 與 next_action 分開；unknown reason 固定文案；attention 先於 ready。
- **驗證：** 同一 payload 在 CLI/JSON/Menubar 語意一致。
- **原始碼／證據：** `magi_v3/health_presentation.py；gui/magi_menubar.py`

### F-026｜Golem 顯示孤兒 1／背景工作 2，但 Menubar 顯示殭屍 0

- **根因：** 兩端各自掃 ps 且定義不同；Golem 用 command substring＋PPID=1，把含 worker 路徑的 zsh -lc launcher 誤算成 worker，Menubar 只看持續 Z state。
- **修復：** 新增單一 magi_v3.process_monitor：只認 actual Python worker，沿 ancestry 判斷有無 canonical MAGI owner，孤兒／殭屍／重複採同一 schema；兩端只作呈現。
- **驗證：** rc600 zsh＋Python 父子形狀只算 worker1/orphan1；managed ancestor 為 orphan0；五秒殭屍、duplicate、read failure 與兩端 summary equality 均有回歸。
- **原始碼／證據：** `magi_v3/process_monitor.py；api/blueprints/web_runtime.py；gui/magi_menubar.py；tests/test_process_monitor_unification.py`

### F-027｜判決趨勢 MCP 候選未納入，但畫面沒有說明理由

- **根因：** 舊流程在結構或篩選條件不符時直接 continue；本機缺本文又會把外部候選一律擋掉，使用者無法分辨是法官、法院、案由、日期、簽署區、主文、附表或來源驗證哪一關失敗。
- **修復：** 每個 MCP 候選都保留公開安全的 exclusion_codes／exclusion_reasons；法官不符會列 requested 與簽署區實際法官。MCP 只有在官方 JID、judgment.judicial.gov.tw 網址、網址內 JID 與完整全文全部交叉驗證後，才可補足本機本文並參與統計。有效 JID 一律連穩定裁判頁；日期可用民國年／月／日選單並由後端正規化重驗。
- **驗證：** 指定 PCDM,114,侵訴,59,20260812,1 與隨機法官名冊＋案由 LIVE 抽查：納入者須有官方全文 proof；未納入者須逐筆列出原因；錯 JID／網址、短本文、未簽署法官、缺主文或日期不符均 fail closed。
- **原始碼／證據：** `api/domains/judgment_official_source.py；api/sentencing_trends.py；api/osc/taiwan_legal_mcp.py；templates/sentencing_trends.html；tests/test_sentencing_trends.py`

### F-028｜HTML 維修手冊的表格在手機上像是把右側文字吃掉

- **根因：** 表格以 display:block 加每欄最小 120px 呈現，實際內容寬度最高 601px，但 390px 視窗只有 317px 可視寬度。
- **修復：** 改為 fixed-layout 整表；儲存格、連結與 code 允許 anywhere/break-word 安全換行，取消欄位最小寬度，窄螢幕再縮小字體與 padding。
- **驗證：** 以實際瀏覽器分別量測 1440px 與 390px：24 張表格的 body overflow、table overflow、clipped cell 均為 0；PDF 268 頁目錄與代表性表格頁可讀。
- **原始碼／證據：** `scripts/docs/build_magi_encyclopedia.py；tests/test_web_information_architecture.py`

### F-029｜Drive owner 實際從 evidence/release-staging 執行，LIVE preflight 拒絕

- **根因：** production deploy 的 plist 已綁 canonical installed root，但 cron snapshot renderer 仍使用呼叫端 staging root，導致排程啟動驗證暫存版本。
- **修復：** production 模式的 cron snapshot 一律以 canonical binding_root 渲染；候選 release-staging 只供封裝與驗證，不得成為正式 process command。owner validator 維持 fail closed，禁止為了切版放寬。
- **驗證：** 部署測試逐一解析 cron command，要求所有程式路徑位於 canonical installed release、不得包含 candidate/evidence 路徑；LIVE 前以 KERN_PROCARGS2／process argv 回讀 worker root。
- **原始碼／證據：** `scripts/v3_deploy_prepare.py；tests/v3/test_deploy_prepare.py`

### F-030｜PDF 命名 benchmark 把摘要內的「第4條」誤當當事人

- **根因：** 舊驗證器使用無嵌套括號 regex。檔名外層是「當事人；摘要」，摘要內又有「（第4條）」時，regex 只取到內層法條。
- **修復：** 改用 balanced outer-bracket parser，保留最外層內容與 group span；嵌套摘要不再取代當事人。
- **驗證：** 真實 47 份 benchmark 的唯一失敗樣本重驗為有效；PDF-namer 相關 35 測試通過；99% 門檻未降低。
- **原始碼／證據：** `skills/pdf-namer/naming_validator.py；tests/test_content_quality_hardening_rc390.py`

### F-031｜實務見解每天蒐集量不足，MCP 候選又因本機無本文被排除

- **根因：** 舊流程把本機 mirror 當成唯一全文信任根，外部候選即使帶官方 JID、司法法院網址與全文，也只能當提示；每天排程只處理本機既有欠量，不能補官方全文來源。
- **修復：** MCP 候選必須通過官方 JID／網址／全文交叉驗證，丟棄 provider 摘要，從全文重新產生 extractive source-bound summary，再沿既有實務可用性 gate 決定是否納入。既有每日裁判蒐集排程加入 bounded MCP gap fill，固定時間與每案由上限，輸出只留 aggregate。
- **驗證：** 錯 JID／URL、短本文、provider summary 污染、非官方來源、PII query 全拒；合法官方全文即使本機沒有 mirror 仍可形成 verified_external_official_fulltext。
- **原始碼／證據：** `api/domains/judgment_flow.py；api/legal_research_quality.py；skills/judgment-collector/action.py；tests/v3/test_legal_research_quality.py`

### F-032｜夜間顯示 backlog=0，實際官方來源仍有大量未下載

- **根因：** 舊健康只算已落本機的 pending wrapper，且把 NVIDIA 96 輪 scheduler selection 誤當成每日 provider 額度，造成『本機沒有待做』與『來源已完整』兩種不同狀態被混為綠燈。失敗 wrapper 也曾被視為永久完成。
- **修復：** 夜間 pull 分別記 source_listed/completed/remaining；失敗 wrapper 可重試；若本機 backlog 為零但 source_remaining>0，健康顯示 SOURCE_PULL_CATCHING_UP。摘要回填分開呈現 scheduler selection capacity、provider daily limit/used/remaining，估算以真實每日額度。夜間 TLR-smart 只在固定窗口提高至 1200 筆、5 天、4800 秒，仍受權威時段與資源 gate 約束。
- **驗證：** 來源尚欠量不得假綠；重跑失敗 wrapper 必須成功寫入後才增加完成數；provider 24/day 不得顯示 3072/day；外部 API 非服務時間保留 durable debt，不偽造成功。
- **原始碼／證據：** `skills/judgment-collector/action.py；scripts/ops/check_judicial_api_pipeline.py；scripts/ops/judgment_summary_staged_backfill.py；api/domains/judicial_api_policy.py`

### F-033｜指定判決取得官方全文後仍因『主文無法辨識』未納入

- **根因：** 司法院將『事 實』展平後可直接連全形匿名代碼，例如『事 實Ａ０７…』；舊主文邊界只允許少數漢字編號，把合法官方全文誤判為 main_section_unrecognized。同時本機 MCP 可選 runtime 實際未安裝，遠端搜尋又遭 WAF 阻擋。
- **修復：** 主文 parser 改以結構性段落標題分界，接受全形匿名代碼但不放寬句內的一般『事實／理由』文字。本機 MCP 部署上游 immutable commit，保留法院篩選，並以 Playwright Chromium 只取司法院 WAF cookie；cookie 0600，瀏覽器不常駐。
- **驗證：** PCDM,114,侵訴,59,20260812,1 實際全文驗證後，梁世樺為末位列名法官，主文刑度可統計且排除碼為空。官方 MCP 79/79；閱卷／筆錄／法扶安裝後 LIVE 20/20；Playwright cleanup 無殘留程序。
- **原始碼／證據：** `api/sentencing_trends.py；api/osc/taiwan_legal_mcp.py；tests/test_sentencing_trends.py；lawchat-oss/mcp-taiwan-legal-db`

### F-034｜NVIDIA 背景額度耗盡被誤報為排程失敗與健康紅燈

- **根因：** 新的 provider 授權層會回傳 background_heavy_authorization_budget_exhausted；舊週末彙整與 cron 結果政策只辨識 nim_daily_budget_exceeded／nim_background_budget_reserved，因此把可預期的每日額度耗盡重試到上限。
- **修復：** 將三種額度標記統一分類為 terminal schedule deferral；scheduler 啟動時以官方 reconcile 將既有同類失敗改列 deferred，不刪除證據、不假造成功，也不繞過每日額度。只有公開裁判全文可依使用者授權原文送 NVIDIA；案件、當事人檔案及閱卷／筆錄／法扶資料仍禁止外送。
- **驗證：** 三種 marker 都產生 deferred、零 false failure；真正 provider error 仍失敗。額度恢復後由正式排程續跑，健康層顯示等待額度而非功能故障。
- **原始碼／證據：** `scripts/weekend_resummary.py；skills/ops/cron_result_policy.py；tests/test_weekend_resummary_budget_semantics.py`

### F-035｜影片範例單調，且顯示理解命令卻沒有真正套用

- **根因：** 第一版只用 synthetic 測試畫面；後續雖能產文字分鏡，但純文字路徑曾忽略已確認的倒序、轉場、運鏡與音訊設定。上游字幕範例又依賴本機 ffmpeg 未提供的 libass filter。
- **修復：** 改用 Pillow 產生繁中 overlay，允許 1～5 份 JPG／PNG／WebP／MP4／MOV 本機素材；中文指令先解析成 exact edit plan 並回顯，成片端點重算並比對 plan SHA。素材與純文字路徑都套用相同順序、淡化或直接切換、平滑或固定畫面、配樂或靜音。停用 updater、remote fetch、CapCut 與 publish；保留 CSRF、大小／速率／並行／deadline／RSS／process-group cleanup。
- **驗證：** 真實圖片、圖片＋MP4、純文字四控制與公開 endpoint 全數重編碼為 1080×1920 H.264/AAC；重新 ffprobe、逐幕畫質抽樣、素材集合／storyboard／plan／output SHA、暫存清除與 child absence 均通。
- **原始碼／證據：** `api/blueprints/video_studio.py；magi_v3/video_autopilot_adapter.py；templates/video_studio.html；tests/test_video_studio_blueprint.py`

### F-036｜筆錄同步最後回傳 success=true、return code 0，排程仍顯示執行失敗

- **根因：** 同步器會把個別案件已捕捉且可重試的 traceback 寫入 stderr，舊 cron 結果政策只要看到 traceback 就覆蓋最後的結構化成功 receipt，將 partial_retry_pending 誤判為整體失敗。
- **修復：** 結果政策先驗證程序 return code 與最後一筆完整 JSON receipt；return code 0 且 receipt success=true 時，保留 partial／retry_pending 業務狀態但排程終態為 success。非零 return code、未捕捉 traceback、缺 receipt 或 success=false 仍 fail closed。
- **驗證：** production job_transcript_sync 於 active RC643/R75 完成：last_success=true、last_status=success、return code 0、error 空；真正失敗反例與 65 個 focused policy tests 仍通過。
- **原始碼／證據：** `skills/ops/cron_result_policy.py；tests/test_cron_result_policy.py；cron_state.json receipt`

### F-037｜active release 已是新版本，但 commercial readiness／release gate 仍讀 candidate 路徑或舊 release 證據

- **根因：** production deploy renderer 已將 LaunchAgent 綁定 canonical installed release；舊 evidence compiler 卻仍要求 candidate root，造成同一個合法封裝同時被部署器接受、證據閘門拒絕。latest 相容 JSON 若未綁 active release，也會讓歷史失敗污染現行紅燈。
- **修復：** Evidence compiler 在 production mode 驗 canonical installed root、release marker、manifest 與 candidate-equivalent immutable 0555 identity；release gate 重新計算 installed marker／manifest 並要求 deployment_mode=production。active-release pointer 才是 latest 真相，舊 JSON 僅為歷史投影。
- **驗證：** canonical tamper、錯 release、錯 manifest、candidate/production 混綁全部拒絕；RC643/R75 最終 gate 14/14 GO，missing／failed／invalid 全空，原子切換後 active marker 與三角色 executable 均指向同一 installed release。
- **原始碼／證據：** `scripts/v3_evidence_compiler.py；scripts/v3_release_gate.py；tests/v3/test_evidence_compiler.py；release-gate-final-r3.json`

### F-038｜網頁維修百科仍顯示 rc641，且過期驗證資料持續占用磁碟

- **根因：** RC643 維修百科是在 R75 hotfix2 完成不可變封裝與切換後才提交；線上 `/manual` 只能讀 active package，因此仍沿用封裝內 rc641 allowlist 與資產。另有已被 r75 取代的 r60～r74 campaign／candidate 報告及逾期臨時驗證資料未依生命週期退役。
- **修復：** 建立 R75 docs-only 不可變維修封裝，將 `/manual` 四個固定 allowlist 資產更新為 rc643；不直接修改 installed release。清理器先解析 active marker 與 rollback 保護清單，只刪除明確版本／前綴、由本機帳號擁有且未被程序使用的 superseded 驗證樹，遇到 symlink、owner、scope 或 active identity 異常即 fail closed。
- **驗證：** 清理 receipt 證明永久移除 317 項、15,582,344,938 bytes；r59、原始 R75、hotfix2 與新文件封裝均保留。文件路由 focused regression、PDF 結構／頁面渲染、封裝 SHA、登入保護與切換後 LIVE `/manual` 檢查全部通過後才更新 active pointer。
- **原始碼／證據：** `scripts/docs/build_magi_encyclopedia.py；api/blueprints/dashboard_pages.py；magi_v3/manual_assets/*rc643*；cleanup receipt；post-cutover manual probe`

<a id="ch23"></a>
# 23. 日常維修、升級與自主演進守則

安全維修流程：fork/branch → 重現 → 最小根修 → focused → adversarial → review → fresh release → backup/restore → prepare → authorized cutover → post/health/outcome → Git 保存。installed source 絕不熱修。

| 可以自主做 | 必須停手／需要額外授權 |
| --- | --- |
| 只讀診斷、local tests、source-only 修補、文件、建立未執行 wrapper | LIVE cutover、外部上傳/刪除、通知他人、清鎖、kill、改 cron/state |
| 安全 cache/worktree 清理（已核准範圍） | 刪 active release、rollback、唯一 backup、案件資料 |
| 健康重算與 PII-free report | 用舊 receipt 假綠或手改 health JSON |

自主演進只能提出可審查 proposal 或在既定 allowlist 內修復，例如清除自己建立且過期的 tmp；不能自行改法律業務判斷、合併案件、上傳文件、刪除遠端檔案或放寬驗證器。

<a id="appA"></a>
# 附錄 A. 核心原始碼節錄與解讀

以下節錄是維修最常需要閱讀的核心邏輯；行號以 source commit `29222c40cd5f898f27670c13feb4c134c751bdb3` 為準。完整檔案請沿不可變連結開啟。

### magi_v3/gateway.py

**位置：** [magi_v3/gateway.py:205](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/gateway.py#L205)<br>

release ownership 驗證：把 gateway 綁到 exact installed release/manifest。

```python
  205  def validate_release_ownership(environ: Mapping[str, str]) -> ReleaseOwnership:
  206      """Fail closed unless gateway and control share one declared release identity."""
  207
  208      if environ.get("MAGI_V3_ROLE", "").strip() != "gateway":
  209          raise GatewayConfigurationError("MAGI_V3_ROLE must equal gateway")
  210      release_id = environ.get("MAGI_V3_RELEASE_ID", "").strip()
  211      if not _RELEASE_ID.fullmatch(release_id):
  212          raise GatewayConfigurationError("MAGI_V3_RELEASE_ID is missing or invalid")
  213      raw_path = environ.get("MAGI_V3_OWNERSHIP_MANIFEST", "").strip()
  214      if not raw_path:
  215          raise GatewayConfigurationError("MAGI_V3_OWNERSHIP_MANIFEST is required")
  216      manifest_path = Path(raw_path).expanduser()
  217      if not manifest_path.is_absolute():
  218          raise GatewayConfigurationError("MAGI_V3_OWNERSHIP_MANIFEST must be absolute")
  219      ownership_sha = environ.get("MAGI_V3_OWNERSHIP_MANIFEST_SHA256", "").strip()
  220      payload = _json_object(manifest_path, ownership_sha)
  221      if payload.get("schema_version") != 1 or payload.get("release_id") != release_id:
  222          raise GatewayConfigurationError("ownership manifest release identity mismatch")
  223
  224      release_sha = payload.get("release_manifest_sha256")
  225      env_release_sha = environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256", "").strip()
  226      if not isinstance(release_sha, str) or not _SHA256.fullmatch(release_sha):
  227          raise GatewayConfigurationError("ownership manifest release hash is invalid")
  228      if env_release_sha != release_sha:
  229          raise GatewayConfigurationError("environment and ownership release hashes differ")
  230
  231      rows = payload.get("roles")
  232      if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
  233          raise GatewayConfigurationError("ownership manifest roles must be a list of objects")
  234      by_role: dict[str, dict[str, Any]] = {}
  235      for row in rows:
  236          role = row.get("role")
  237          if not isinstance(role, str) or role in by_role:
  238              raise GatewayConfigurationError("ownership manifest contains invalid or duplicate roles")
  239          by_role[role] = row
  240      if "gateway" not in by_role or "control" not in by_role:
  241          raise GatewayConfigurationError("ownership manifest must declare gateway and control roles")
  242      gateway = by_role["gateway"]
  243      control = by_role["control"]
  244
  245      expected = (
  246          (gateway, "gateway", "com.magi.v3.gateway", [5002, 5003], _GATEWAY_DOMAINS),
  247          (control, "control", "com.magi.v3.control", [8088], _CONTROL_DOMAINS),
  248      )
  249      for binding, role, label, ports, domains in expected:
  250          if binding.get("release_id") != release_id or binding.get("label") != label:
  251              raise GatewayConfigurationError(f"{role} ownership does not match the declared release")
  252          if binding.get("release_manifest_sha256") != release_sha:
  253              raise GatewayConfigurationError(f"{role} ownership release hash mismatch")
  254          if binding.get("ports") != ports:
  255              raise GatewayConfigurationError(f"{role} ownership ports mismatch")
  256          if not domains <= _string_set(binding.get("ownership_domains"), name=f"{role} ownership_domains"):
  257              raise GatewayConfigurationError(f"{role} ownership domains are incomplete")
  258          declared_manifest = binding.get("ownership_manifest")
  259          if not isinstance(declared_manifest, str) or Path(declared_manifest).resolve() != manifest_path:
  260              raise GatewayConfigurationError(f"{role} ownership manifest path mismatch")
  261
  262      if environ.get("MAGI_V3_PORTS", "").strip() != "5002,5003":
  263          raise GatewayConfigurationError("gateway environment must declare ports 5002,5003")
  264      env_domains = frozenset(
  265          item.strip()
  266          for item in environ.get("MAGI_V3_OWNERSHIP_DOMAINS", "").split(",")
  267          if item.strip()
  268      )
  269      if not _GATEWAY_DOMAINS <= env_domains:
  270          raise GatewayConfigurationError("gateway environment ownership domains are incomplete")
  271      return ReleaseOwnership(release_id, manifest_path, release_sha, gateway, control)
  272
  273
```

### magi_v3/cron_service.py

**位置：** [magi_v3/cron_service.py:80](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/cron_service.py#L80)<br>

occurrence 與 timeout 的 deterministic 身分。

```python
   80  def _cron_occurrence_id(job: dict[str, Any], command_sha256: str) -> str:
   81      supplied = str(job.get("_magi_occurrence_id") or "").strip().lower()
   82      if len(supplied) == 64 and all(char in "0123456789abcdef" for char in supplied):
   83          return supplied
   84      due_at = str(job.get("_magi_due_at") or "").strip()
   85      if not due_at:
   86          # A due occurrence normally carries _magi_due_at.  This fallback is
   87          # used only by direct execution adapters and is propagated into every
   88          # retry before the first process exits.
   89          due_at = datetime.now().isoformat(timespec="minutes")
   90      raw = "\0".join((str(job.get("id") or ""), command_sha256, due_at))
   91      return hashlib.sha256(raw.encode("utf-8")).hexdigest()
   92
   93
   94  def _cron_timeout_seconds(runtime_policy: Any, job: dict[str, Any]) -> int:
   95      """Return the sealed policy timeout with narrowly-approved safety floors."""
   96
   97      configured = int(runtime_policy.cron_job_timeout(job))
   98      floor = int(CRON_TIMEOUT_FLOORS_SECONDS.get(str(job.get("id") or ""), 0))
   99      return max(configured, floor)
  100
  101
  102  def _load_bound_cron_environment() -> None:
  103      """Load the hash-bound V3 environment before any scheduled child starts."""
  104
  105      raw = os.environ.get("MAGI_ENV_FILE", "").strip()
  106      if not raw:
  107          return
  108      path = Path(raw).expanduser()
  109      if (
  110          not path.is_absolute()
  111          or path.is_symlink()
  112          or not path.is_file()
  113      ):
  114          raise CronServiceError("MAGI_ENV_FILE is not a safe regular file")
  115      expected = os.environ.get("MAGI_ENV_FILE_SHA256", "").strip().lower()
  116      if expected:
  117          digest = hashlib.sha256(path.read_bytes()).hexdigest()
  118          if digest != expected:
  119              raise CronServiceError("MAGI_ENV_FILE SHA-256 mismatch")
  120      try:
  121          from dotenv import load_dotenv
  122      except ImportError as exc:
  123          raise CronServiceError("python-dotenv is required for MAGI_ENV_FILE") from exc
  124      if load_dotenv(path, override=False) is False:
  125          raise CronServiceError("MAGI_ENV_FILE could not be loaded")
  126
```

### magi_v3/drive_file_checkpoint.py

**位置：** [magi_v3/drive_file_checkpoint.py:72](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/drive_file_checkpoint.py#L72)<br>

case/item token、fingerprint、proof 與原子 JSON。

```python
   72  def case_token(worker_kind: str, canonical_case_id: str) -> str:
   73      return _digest("drive-case-v1", worker_kind, canonical_case_id)
   74
   75
   76  def source_fingerprint(
   77      *,
   78      direction: str,
   79      case_key: str,
   80      locator: str,
   81      size: int | None = None,
   82      modified: object = "",
   83      checksum: str = "",
   84      opaque_source_id: str = "",
   85  ) -> str:
   86      return _digest(
   87          "drive-source-v1",
   88          direction,
   89          case_key,
   90          locator,
   91          int(size or 0),
   92          modified,
   93          checksum,
   94          opaque_source_id,
   95      )
   96
   97
   98  def item_token(*, direction: str, case_key: str, source_key: str) -> str:
   99      return _digest("drive-item-v1", direction, case_key, source_key)
  100
  101
  102  def proof_hash(*parts: object) -> str:
  103      return _digest("drive-proof-v1", *parts)
  104
  105
  106  def snapshot_hash(tokens: Iterable[str]) -> str:
  107      clean = sorted({str(token) for token in tokens if _valid_digest(token)})
  108      return _digest("drive-snapshot-v1", *clean)
  109
  110
  111  def _strict_atomic_json(path: Path, payload: dict[str, Any]) -> None:
  112      """Replace a private JSON file durably, including the containing directory."""
  113
  114      path = Path(path)
  115      path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  116      try:
  117          os.chmod(path.parent, 0o700)
  118      except OSError:
  119          pass
  120      tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
  121      data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
  122          "utf-8"
  123      )
  124      fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  125      try:
  126          with os.fdopen(fd, "wb", closefd=True) as handle:
  127              handle.write(data)
  128              handle.flush()
  129              os.fsync(handle.fileno())
  130          os.replace(tmp, path)
  131          os.chmod(path, 0o600)
  132          try:
  133              dir_fd = os.open(path.parent, os.O_RDONLY)
  134          except OSError:
  135              dir_fd = -1
  136          if dir_fd >= 0:
  137              try:
  138                  os.fsync(dir_fd)
  139              finally:
  140                  os.close(dir_fd)
  141      finally:
  142          try:
  143              tmp.unlink()
  144          except FileNotFoundError:
  145              pass
  146
  147
```

### magi_v3/file_review_receipts.py

**位置：** [magi_v3/file_review_receipts.py:16](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/file_review_receipts.py#L16)<br>

閱卷 canonical signature 與 snapshot receipt。

```python
   16  def _first(row: dict[str, Any], *names: str) -> str:
   17      for name in names:
   18          value = str(row.get(name) or "").strip()
   19          if value:
   20              return value
   21      return ""
   22
   23
   24  def _first_upper(row: dict[str, Any], *names: str) -> str:
   25      """Read one alias set using the portal's case-insensitive status semantics."""
   26      return _first(row, *names).upper()
   27
   28
   29  def canonical_portal_download_signature(row: dict[str, Any]) -> str:
   30      """Hash one actionable OLA row without publishing its case/person data.
   31
   32      Portal ``rowid`` is the preferred opaque identity.  Case/court fields are
   33      used only as an in-memory fallback when an older row has no opaque ID; the
   34      receipt exposes only the final SHA-256 digest.  Revision/status fields make
   35      a later upload batch on the same row a different receipt.
   36      """
   37      if not isinstance(row, dict):
   38          return ""
   39      row_id = _first(row, "rowid", "no")
   40      identity = {
   41          "row_id": row_id,
   42          "fallback_court": "" if row_id else _first(row, "court", "crtid"),
   43          "fallback_case": "" if row_id else _first(
   44              row, "case_number", "yyidno", "court_case_no", "showyyidno", "c60yyidno"
   45          ),
   46          "apply_at": _first(row, "applydt"),
   47      }
   48      revision = {
   49          "semantic_status": "downloadable",
   50          "status_code": _first(row, "status_code", "status"),
   51          "portal_status": _first_upper(row, "p_status"),
   52          "payment_status": _first(row, "paystatus"),
   53          "payment_flag": _first_upper(row, "payment_flag", "payment"),
   54          "is_downloaded": _first_upper(row, "isdown"),
   55          "download_date": _first(row, "downdt"),
   56          "download_time": _first(row, "downtm"),
   57          "download_deadline": _first(
   58              row, "deadline", "downlimit", "dlmdate", "payedate"
   59          ),
   60          "payment_deadline": _first(row, "pay_deadline", "paylimitdt", "limitdt"),
   61          "updated_at": _first(row, "upddt", "updated_at", "updtime"),
   62          "content_marker": hashlib.sha256(
   63              re.sub(r"\s+", " ", _first(row, "result_text", "result", "row_text"))[:1000]
   64              .strip()
   65              .encode("utf-8")
   66          ).hexdigest(),
   67      }
   68      if not any(identity.values()):
   69          return ""
   70      material = json.dumps(
   71          {
   72              "schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
   73              "identity": identity,
   74              "revision": revision,
   75          },
   76          ensure_ascii=False,
   77          sort_keys=True,
   78          separators=(",", ":"),
   79      )
   80      return hashlib.sha256(material.encode("utf-8")).hexdigest()
   81
   82
   83  def normalize_signature_hashes(values: Iterable[Any] | None) -> list[str]:
   84      return sorted(
   85          {
   86              str(value or "").strip().lower()
   87              for value in (values or [])
   88              if _SHA256_RE.fullmatch(str(value or "").strip().lower())
   89          }
   90      )
   91
   92
   93  def signature_set_hash(values: Iterable[Any] | None) -> str:
   94      normalized = normalize_signature_hashes(values)
   95      material = json.dumps(
   96          {"schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA, "signatures": normalized},
   97          sort_keys=True,
   98          separators=(",", ":"),
   99      )
  100      return hashlib.sha256(material.encode("utf-8")).hexdigest()
  101
  102
  103  def portal_snapshot_fingerprint(values: Iterable[Any] | None) -> str:
  104      signatures = normalize_signature_hashes(values)
  105      fingerprint_material = json.dumps(
  106          {
  107              "schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
  108              "signature_set_hash": signature_set_hash(signatures),
  109              "signature_count": len(signatures),
  110          },
  111          sort_keys=True,
  112          separators=(",", ":"),
  113      )
  114      return hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()
  115
  116
  117  def portal_observed_epoch(value: Any) -> float | None:
  118      text = str(value or "").strip()
  119      if not text:
  120          return None
  121      try:
  122          parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
  123      except (TypeError, ValueError):
  124          return None
  125      if parsed.tzinfo is None:
  126          return None
  127      return parsed.timestamp()
  128
  129
  130  def portal_download_snapshot(
  131      items: Iterable[dict[str, Any]] | None,
  132      *,
  133      observed_at: str = "",
  134  ) -> dict[str, Any]:
  135      signatures = normalize_signature_hashes(
  136          canonical_portal_download_signature(item)
  137          for item in (items or [])
  138          if isinstance(item, dict)
  139          and str(item.get("status") or "").strip().lower() == "downloadable"
  140      )
  141      observed = str(observed_at or "").strip() or datetime.now().astimezone().isoformat(
  142          timespec="seconds"
  143      )
  144      set_hash = signature_set_hash(signatures)
  145      return {
```

### api/model_router.py

**位置：** [api/model_router.py:273](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/model_router.py#L273)<br>

模型 gate 與 request routing 的安全判斷。

```python
  273  def _evaluate_gates(spec: ModelSpec, resource: ResourceView, active_models: tuple[str, ...], prompt_len: int) -> tuple[bool, tuple[str, ...], bool]:
  274      gates = dict(spec.gates or {})
  275      blocked: list[str] = []
  276      should_queue = False
  277
  278      if gates.get("require_model_live", False) and not _active_has(active_models, "26b"):
  279          blocked.append("26b_not_live")
  280
  281      min_disk = _env_float("MAGI_ROUTER_26B_MIN_DISK_GB", float(gates.get("min_disk_free_gb", 70) or 70))
  282      if resource.disk_free_gb >= 0 and resource.disk_free_gb < min_disk:
  283          blocked.append(f"disk_free<{min_disk:g}GB")
  284          should_queue = True
  285
  286      min_free = _env_float("MAGI_ROUTER_26B_MIN_FREE_GB", float(gates.get("min_free_plus_inactive_gb", 8) or 8))
  287      if resource.free_plus_inactive_gb >= 0 and resource.free_plus_inactive_gb < min_free:
  288          blocked.append(f"free_plus_inactive<{min_free:g}GB")
  289          should_queue = True
  290
  291      max_swap = _env_float("MAGI_ROUTER_26B_MAX_SWAP_GB", float(gates.get("max_swap_used_gb", 20) or 20))
  292      if resource.swap_used_gb >= 0 and resource.swap_used_gb > max_swap:
  293          blocked.append(f"swap_used>{max_swap:g}GB")
  294          should_queue = True
  295
  296      allowed_levels = tuple(str(x) for x in gates.get("allowed_resource_levels") or ("normal",))
  297      if resource.level not in allowed_levels and resource.level != "unknown":
  298          blocked.append(f"resource_level={resource.level}")
  299          should_queue = True
  300
  301      max_prompt = int(os.environ.get("MAGI_ROUTER_26B_MAX_PROMPT_CHARS", "60000") or "60000")
  302      if prompt_len > max_prompt:
  303          blocked.append(f"prompt_len>{max_prompt}")
  304          should_queue = True
  305
  306      return not blocked, tuple(blocked), should_queue
  307
  308
  309  def choose_model_for_request(
  310      *,
  311      task_type: str = "general",
  312      prompt: str = "",
  313      requested_model: str = "",
  314      heavy_opt_in: bool = False,
  315      force_quality: bool = False,
  316      active_models: Iterable[str] | None = None,
  317      resource: ResourceView | dict[str, Any] | None = None,
  318      registry: dict[str, ModelSpec] | None = None,
  319  ) -> ModelRouteDecision:
  320      task = str(task_type or "general").strip() or "general"
  321      prompt_len = len(str(prompt or ""))
  322      reg = registry or load_registry()
  323      active, rv = get_runtime_state(active_models=active_models, resource=resource) if active_models is not None or resource is not None else get_runtime_state()
  324
  325      if task == "embedding":
  326          return ModelRouteDecision(
  327              selected_model=DEFAULT_EMBED_MODEL,
  328              tier="embedding_local",
  329              reason="embedding tasks must keep the embedding model",
  330              task_type=task,
  331              provider="omlx",
  332              active_models=active,
  333              resource_level=rv.level,
  334              safe_context_tokens=8192,
  335          )
  336
  337      if requested_model and not is_disallowed_model(requested_model):
  338          resolved = resolve_text_model(requested_model, available=active or None)
  339          spec = reg.get(resolved) or reg.get(requested_model)
  340          if resolved and (not active or resolved in active):
  341              return ModelRouteDecision(
  342                  selected_model=resolved,
  343                  tier=(spec.tier if spec else "explicit"),
  344                  reason="explicit model request accepted",
  345                  task_type=task,
  346                  provider=(spec.provider if spec else "omlx"),
  347                  active_models=active,
  348                  preferred_model=resolved,
  349                  resource_level=rv.level,
  350                  safe_context_tokens=(spec.safe_context_tokens if spec else 4096),
  351              )
  352
  353      if heavy_opt_in:
  354          return ModelRouteDecision(
  355              selected_model="nvidia-nim-heavy-non-china" if _env_bool("NVIDIA_NIM_ENABLE", False) else TEXT_PRIMARY_MODEL,
  356              tier="cloud_heavy" if _env_bool("NVIDIA_NIM_ENABLE", False) else "stable_local",
  357              reason="@heavy explicitly requested; NVIDIA NIM is required and failures close without local fallback",
  358              task_type=task,
  359              provider="nvidia_nim" if _env_bool("NVIDIA_NIM_ENABLE", False) else "omlx",
  360              active_models=active,
```

### api/blueprints/cookie_cutter.py

**位置：** [api/blueprints/cookie_cutter.py:45](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/cookie_cutter.py#L45)<br>

cookie 子程序資源錯誤、bounded upload 與 parent cleanup。

```python
   45      "stamp_mirrored.3mf",
   46  }
   47  _RESOURCE_ATTESTATION_KEYS = {
   48      "generation_seconds",
   49      "peak_rss_bytes",
   50      "child_reaped",
   51      "child_leaks",
   52  }
   53  _RESOURCE_LIMIT_SETUP_FAILURE = "generation_resource_limit_setup_failed"
   54  _COOKIE_ERROR_MESSAGES = {
   55      "no_usable_line_art": "圖片中找不到可用的黑白線稿。",
   56      "open_or_missing_outer_contour": "找不到完整封閉的最外框，請先補齊斷線。",
   57      "outer_contour_touches_image_edge": "最外框碰到圖片邊緣或底色無法判讀，請在四周保留白邊。",
   58      "line_art_geometry_incomplete": "線稿無法建立完整封閉模型，請簡化細節後重試。",
   59      "vector_geometry_unavailable": "切模向量引擎尚未就緒，請稍後再試。",
   60      "contour_quality_failed": "線稿曲線誤差超過 0.15 mm，請提高原圖解析度後重試。",
   61      "cutter_wall_not_continuous": "切模外壁無法形成單一連續封閉環，請加粗過窄處或移除相交線段後重試。",
   62      "feature_too_small": "線稿細節小於可安全列印的最小寬度，請加粗後重試。",
   63      "resource_limit_exceeded": "線稿過於複雜或處理逾時，請簡化細節後重試。",
   64      "finished_envelope_exceeded": "成品尺寸超出設定值，請調整握邊或寬度。",
   65      "invalid_dimensions": "建模尺寸不合理，請依畫面範圍調整。",
   66      "generation_resource_limit": "模型產生超過資源限制，請簡化線稿後重試。",
   67      "generation_resource_limit_setup_failed": "模型資源限制無法安全啟用，請稍後再試。",
   68      "generation_resource_cleanup_failed": "模型工作程序未能安全回收，請稍後再試。",
   69      "generation_resource_attestation_failed": "模型資源證據無法安全驗證，請稍後再試。",
   70      "generation_archive_schema_failed": "模型壓縮檔未通過完整性檢查，請稍後再試。",
   71      "resource_attestation_unavailable": "模型資源監測證據不足，請稍後再試。",
   72  }
   73
   74
   75  def _cookie_generation_child(
   76      connection,
   77      monitor_ready,
   78      content: bytes,
   79      values: dict[str, float],
   80  ) -> None:
   81      """Spawn-safe, importable child target. Never writes uploads to disk."""
   82      try:
   83          # Import in the child so macOS spawn does not inherit server state.
   84          from skills.cookie_stl import CookieParameters, CookieSTLError, generate_zip_bytes
   85          try:
   86              import resource
   87              resource.setrlimit(
   88                  resource.RLIMIT_CPU,
   89                  (MAX_GENERATION_SECONDS, MAX_GENERATION_SECONDS + 1),
   90              )
   91          except (ImportError, OSError, ValueError):
   92              # A child without its CPU ceiling is unsafe to run.  Send only a
   93              # fixed protocol value: no platform exception details cross IPC.
   94              connection.send(("resource_error", _RESOURCE_LIMIT_SETUP_FAILURE))
   95              return
   96          # Do not start the expensive engine until the parent has attached its
   97          # OS-level RSS monitor and recorded the first positive sample.  Without
   98          # this handshake, a simple frame can legitimately finish before psutil
   99          # observes it and is then rejected as an unattested fast child.
  100          if not monitor_ready.wait(timeout=MAX_GENERATION_SECONDS):
  101              connection.send(("cookie_error", "resource_attestation_unavailable"))
  102              return
  103          bundle, summary = generate_zip_bytes(content, CookieParameters(**values))
  104          connection.send(("ok", bundle, summary))
  105      except CookieSTLError as exc:
  106          connection.send(("cookie_error", str(exc)))
  107      except Exception:
  108          connection.send(("error", "generation_failed"))
  109      finally:
  110          connection.close()
  111
  112
  113  def _plain_error(message: str, status: int):
  114      response = jsonify({"ok": False, "message": message})
  115      response.status_code = status
  116      response.headers["Cache-Control"] = "no-store"
  117      return response
  118
  119
  120  def _cookie_error_response(exc: CookieSTLError):
  121      return _plain_error(
  122          _COOKIE_ERROR_MESSAGES.get(
  123              str(exc),
  124              "線稿未通過封閉網格檢查，請修正後重試。",
  125          ),
  126          400,
  127      )
  128
  129
  130  @cookie_cutter_bp.before_request
  131  def _reject_oversized_multipart_before_parse():
  132      """Reject a declared oversized body before Flask accesses request.files."""
  133      if request.method != "POST":
  134          return None
  135      declared = request.content_length
  136      if declared is not None and declared > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES:
  137          return _plain_error("圖片過大，請控制在 8MB 以內", 413)
  138      return None
  139
  140
```

### api/sentencing_trends.py

**位置：** [api/sentencing_trends.py:220](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/sentencing_trends.py#L220)<br>

穩定官方裁判頁、簽署區法官核對與量刑解析。

```python
  220      if len(found) == 1 and found[0]["role"] == "法官":
  221          found[0]["role"] = "獨任法官"
  222      return found
  223
  224
  225  def _sentence_items(text: str) -> list[dict[str, Any]]:
  226      items: list[dict[str, Any]] = []
  227      for match in _SENTENCE_RE.finditer(text):
  228          phrase = _clean_text(match.group(0))
  229          item = {"kind": match.group("kind"), "text": phrase, "months": _duration_months(match.group("kind"), match.group("term"))}
  230          if item not in items:
  231              items.append(item)
  232      return items
  233
  234
  235  def _execution_item(main_text: str) -> dict[str, Any] | None:
  236      match = _EXECUTION_RE.search(main_text)
  237      if not match:
  238          return None
  239      return {
  240          "kind": match.group("kind"),
  241          "text": _clean_text(match.group(0)),
  242          "months": _duration_months(match.group("kind"), match.group("term")),
  243      }
  244
  245
  246  def _official_issue(full_text: str, fallback: str = "") -> str:
  247      head = _clean_text(full_text[:1800])
  248      patterns = (
  249          r"上列.{0,18}?因(.{1,40}?)案件",
  250          r"因犯(.{1,32}?)罪",
  251      )
  252      for pattern in patterns:
  253          match = re.search(pattern, head, re.S)
  254          if match:
  255              return re.sub(r"\s+", "", match.group(1)).strip("，。；;")[:80]
  256      return str(fallback or "").strip()
  257
  258
  259  def parse_sentencing_judgment(row: dict[str, Any]) -> dict[str, Any]:
  260      full_text = str(row.get("full_text") or "")
  261      main = _main_text(full_text)
  262      judges = _signature_judges(full_text)
  263      participating_judges = [entry["name"] for entry in judges if entry.get("name")]
  264      last_listed_judge = participating_judges[-1] if participating_judges else ""
  265      appendix_referenced = "如附表" in main or "如附表" in full_text[:2200]
  266      appendix_pos = max(full_text.rfind("【附表"), full_text.rfind("\n附表"))
  267      appendix_text = full_text[appendix_pos:] if appendix_pos >= 0 else ""
  268      appendix_sentences = _sentence_items(appendix_text)
  269      appendix_complete = (not appendix_referenced) or bool(appendix_sentences)
  270      main_sentences = _sentence_items(main)
  271      execution = _execution_item(main)
  272      if execution:
  273          main_sentences = [item for item in main_sentences if item["text"] != execution["text"]]
  274      raw_source_url = str(row.get("source_url") or "").strip()
  275      jid = str(row.get("jid") or "").strip()
  276      source_url = official_judgment_page_url(jid, raw_source_url)
  277      court_match = _COURT_RE.search(full_text[:500])
  278      court = court_match.group(1) if court_match else str(row.get("court_name") or "").strip()
  279      judgment_date = normalize_judgment_date(row.get("judgment_date"))
  280      exclusion_codes: list[str] = []
  281      if not jid or not _OFFICIAL_JID_RE.fullmatch(jid):
  282          exclusion_codes.append("missing_official_jid")
  283      if not full_text:
  284          exclusion_codes.append("missing_official_fulltext")
  285      if not judges:
  286          exclusion_codes.append("signature_block_unrecognized")
  287      if not main:
  288          exclusion_codes.append("main_section_unrecognized")
  289      if main and not (main_sentences or execution):
  290          exclusion_codes.append("sentence_not_found")
  291      if appendix_referenced and not appendix_complete:
  292          exclusion_codes.append("appendix_incomplete")
  293      if row.get("judgment_date") and not judgment_date:
  294          exclusion_codes.append("judgment_date_invalid")
  295      complete = not exclusion_codes
  296      return {
  297          "id": row.get("id"),
  298          "jid": jid,
  299          "court": court,
  300          "case_number": str(row.get("case_number") or "").strip(),
  301          "case_type": str(row.get("case_type") or "").strip(),
  302          "issue": _official_issue(full_text, str(row.get("case_type") or "")),
  303          # Keep the ISO canonical value for database/filter/sort semantics and
  304          # provide an explicit display-only value for every presentation path.
  305          "judgment_date": judgment_date,
  306          "judgment_date_display": format_roc_date(judgment_date),
  307          "judges": judges,
  308          "participating_judges": participating_judges,
  309          "last_listed_judge": last_listed_judge,
  310          "main_text": main[:1800],
  311          "sentences": main_sentences,
  312          "execution_sentence": execution,
  313          "appendix_referenced": appendix_referenced,
  314          "appendix_complete": appendix_complete,
  315          "appendix_sentences": appendix_sentences[:60],
  316          "statistics_eligible": complete,
  317          "exclusion_codes": exclusion_codes,
  318          "exclusion_reason": "；".join(_public_exclusion_reason(code) for code in exclusion_codes),
  319          "source_url": source_url,
  320          "source_bound": bool(jid and (_official_judgment_url(raw_source_url) or full_text)),
  321          "source": str(row.get("source") or "local_official_archive"),
  322          "external_verified": bool(row.get("external_verified")),
  323      }
  324
  325
  326  def _official_judgment_url(value: Any) -> bool:
  327      return is_official_judgment_url(value)
  328
  329
  330  def search_public_judgment_candidates(query: str, **kwargs: Any) -> dict[str, Any]:
  331      """Search the deployed MCP chain with a privacy-safe query.
  332
  333      Results remain discovery candidates.  ``search_sentencing_trends`` applies
  334      the independent official-JID/full-text/signature/sentence gates before any
  335      result can enter statistics.
  336      """
  337
  338      from api.legal_research_quality import prepare_external_legal_query
  339
  340      privacy = prepare_external_legal_query(query)
  341      court = str(kwargs.pop("court", "") or "").strip()
  342      if privacy.external_allowed and privacy.safe_query:
  343          try:
  344              from api.osc.legaltech_taiwan_law_mcp import (
  345                  search_practical_judgments_via_legaltech,
  346              )
  347
  348              result = search_practical_judgments_via_legaltech(
  349                  privacy.safe_query,
  350                  **({"court": court} if court else {}),
  351                  **kwargs,
  352              )
  353              if result.get("success"):
  354                  return result
  355          except (ImportError, ModuleNotFoundError):
  356              pass
  357      try:
  358          from api.osc.taiwan_legal_mcp import search_practical_judgments_via_mcp
  359
  360          return search_practical_judgments_via_mcp(
```

### api/domains/judgment_official_source.py

**位置：** [api/domains/judgment_official_source.py:1](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_official_source.py#L1)<br>

MCP 判決的官方 JID、司法法院網址、完整全文與日期正規化信任邊界。

```python
    1  """Strict provenance helpers for official Taiwanese judgment candidates.
    2
    3  The helpers in this module are deliberately independent from storage and
    4  presentation code.  An MCP response is discovery evidence only until its JID,
    5  official URL, full text and date agree with this contract.
    6  """
    7  from __future__ import annotations
    8
    9  import hashlib
   10  import re
   11  from datetime import date
   12  from typing import Any
   13  from urllib.parse import parse_qs, unquote, urlparse
   14
   15
   16  OFFICIAL_JUDGMENT_HOSTS = {"judgment.judicial.gov.tw", "data.judicial.gov.tw"}
   17  OFFICIAL_JID_RE = re.compile(
   18      r"^[A-Z0-9]{2,12},\d{2,3},[^,\s]{1,24},\d{1,10},\d{8},\d{1,3}$"
   19  )
   20
   21
   22  def normalize_judgment_date(value: Any) -> str:
   23      """Return a validated Gregorian ISO date from ROC/Gregorian input."""
   24
   25      text = str(value or "").strip()
   26      if not text:
   27          return ""
   28      match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
   29      if match:
   30          year, month, day = (int(part) for part in match.groups())
   31      else:
   32          match = re.fullmatch(
   33              r"(?:民國\s*)?(\d{1,3})(?:\s*年|[-/.])(\d{1,2})(?:\s*月|[-/.])(\d{1,2})(?:\s*日)?",
   34              text,
   35          )
   36          if not match:
   37              return ""
   38          year, month, day = (int(part) for part in match.groups())
   39          year += 1911
   40      try:
   41          return date(year, month, day).isoformat()
   42      except ValueError:
   43          return ""
   44
   45
   46  def is_official_judgment_url(value: Any) -> bool:
   47      try:
   48          parsed = urlparse(str(value or "").strip())
   49      except (TypeError, ValueError):
   50          return False
   51      return parsed.scheme == "https" and str(parsed.hostname or "").lower() in OFFICIAL_JUDGMENT_HOSTS
   52
   53
   54  def _url_jid_matches(url: str, jid: str) -> bool:
   55      parsed = urlparse(url)
   56      if str(parsed.hostname or "").lower() == "judgment.judicial.gov.tw":
   57          values = parse_qs(parsed.query).get("id") or []
   58          return len(values) == 1 and unquote(str(values[0])) == jid
   59      # data.judicial.gov.tw is an authenticated official API.  Its response is
   60      # bound by the exact JID field rather than an HTML query parameter.
   61      return str(parsed.hostname or "").lower() == "data.judicial.gov.tw"
   62
   63
   64  def official_judgment_page_url(jid: Any, source_url: Any = "") -> str:
   65      normalized = str(jid or "").strip()
   66      if OFFICIAL_JID_RE.fullmatch(normalized):
   67          from urllib.parse import quote
   68
   69          return (
   70              "https://judgment.judicial.gov.tw/FJUD/data.aspx"
   71              f"?ty=JD&id={quote(normalized, safe='')}&ot=in"
   72          )
   73      fallback = str(source_url or "").strip()
   74      if not is_official_judgment_url(fallback):
   75          return ""
   76      return fallback if str(urlparse(fallback).hostname or "").lower() == "judgment.judicial.gov.tw" else ""
   77
   78
   79  def _date_from_full_text(full_text: str) -> str:
   80      head = str(full_text or "")[:2200]
   81      for pattern in (
   82          r"中\s*華\s*民\s*國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
   83          r"民\s*國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
   84      ):
   85          match = re.search(pattern, head)
   86          if match:
   87              return normalize_judgment_date("-".join(match.groups()))
   88      return ""
   89
   90
   91  def validate_official_judgment_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
   92      """Validate a full-text MCP candidate without trusting its summary."""
   93
   94      reasons: list[str] = []
   95      jid = str(candidate.get("jid") or candidate.get("doc_id") or "").strip()
   96      source_url = str(candidate.get("source_url") or candidate.get("url") or "").strip()
   97      full_text = str(candidate.get("full_text") or candidate.get("content") or "").strip()
   98      if not OFFICIAL_JID_RE.fullmatch(jid):
   99          reasons.append("missing_or_invalid_official_jid")
  100      if candidate.get("official_origin") is not True:
  101          reasons.append("official_origin_not_verified")
  102      if not is_official_judgment_url(source_url):
  103          reasons.append("unofficial_source_url")
  104      elif jid and not _url_jid_matches(source_url, jid):
  105          reasons.append("official_url_jid_mismatch")
  106      # A complete simplified judgment can legitimately be short.  Structural
  107      # consumers perform their own signature/main/holding checks, so this gate
  108      # only distinguishes real judgment text from a title/snippet.
  109      if len(full_text) < 40:
  110          reasons.append("missing_official_fulltext")
  111      raw_date = candidate.get("judgment_date") or candidate.get("date")
  112      normalized_date = normalize_judgment_date(raw_date) or _date_from_full_text(full_text)
  113      if raw_date and not normalized_date:
  114          reasons.append("judgment_date_invalid")
  115      return {
  116          "ok": not reasons,
  117          "exclusion_codes": reasons,
  118          "jid": jid,
  119          "source_url": official_judgment_page_url(jid, source_url),
  120          "judgment_date": normalized_date,
  121          "full_text": full_text if not reasons else "",
  122          "full_text_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest() if full_text else "",
  123          "pii_included": False,
  124      }
```

### api/domains/judgment_flow.py

**位置：** [api/domains/judgment_flow.py:305](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_flow.py#L305)<br>

實務見解的本機 mirror 與 verified external official fulltext 雙路來源綁定。

```python
  305          out["success"] = False
  306          out["error"] = "no_high_quality_judgment_matches"
  307      return out
  308
  309
  310  def _run_skill_json(skill_script: str, task: str, timeout_sec: int) -> Dict[str, Any]:
  311      py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
  312      if not py or not os.path.exists(py):
  313          py = sys.executable or "python3"
  314      proc = subprocess.run(
  315          [py, skill_script, "--task", task],
  316          capture_output=True,
  317          text=True,
  318          timeout=timeout_sec,
  319          cwd=_MAGI_ROOT,
  320          env=os.environ.copy(),
  321      )
  322      out = (proc.stdout or "").strip()
  323      err_text = (proc.stderr or "").strip()
  324      if proc.returncode != 0:
  325          return {"ok": False, "error": (err_text or out or "unknown")[:280], "returncode": proc.returncode}
  326      if not out:
  327          return {"ok": False, "error": "empty_output", "returncode": proc.returncode}
  328      try:
  329          data = json.loads(out)
  330          if isinstance(data, dict):
  331              return data
  332      except Exception:
  333          pass
  334      return {"ok": False, "error": out[:500], "returncode": proc.returncode}
  335
  336
  337  def _is_practical_insight_request(message: str) -> bool:
  338      text = str(message or "")
  339      return any(keyword in text for keyword in ["實務見解", "法律見解", "法院見解"])
  340
  341
  342  _GENERAL_LEGAL_QUESTION_RE = re.compile(
  343      r"(?:"
  344      r"(?:法律上|依(?:民法|刑法|公司法|行政程序法)|民法第?\d+條|刑法第?\d+條|"
  345      r"侵權行為|違約|損害賠償|不當得利|無因管理|舉證責任|消滅時效|"
  346      r"請求權時效|構成要件|法律效果|管轄法院|上訴期間|抗告期間)"
  347      r".{0,36}(?:如何|怎麼|為何|是否|可否|哪些|要件|責任|時效|效力|認定|成立|分配)|"
  348      r"(?:如何|怎麼|為何|是否|可否).{0,24}"
  349      r"(?:舉證責任|消滅時效|請求權時效|構成要件|法律效果|管轄法院|侵權行為|損害賠償)"
  350      r")"
  351  )
  352
  353
  354  def _is_general_legal_question(message: str) -> bool:
  355      """Recognize common legal questions that need evidence, not model recall."""
  356      return bool(_GENERAL_LEGAL_QUESTION_RE.search(str(message or "").replace(" ", "")))
  357
  358
  359  def _is_legal_research_request(message: str) -> bool:
  360      text = str(message or "")
  361      if _is_practical_insight_request(text):
  362          return True
  363      return any(
  364          keyword in text
  365          for keyword in [
  366              "查判決",
  367              "找判決",
  368              "判決搜尋",
  369              "搜尋判決",
  370              "收集判決",
  371              "判決搜集",
  372              "搜尋最高法院判決",
  373              "查裁判",
  374              "找裁判",
  375              "裁判搜尋",
  376              "搜尋裁判",
  377              "查法院",
  378              "法院判決",
  379              "最高法院",
  380              "最高行政法院",
  381              "大法庭",
  382              "查法規",
  383              "查法條",
  384              "法規查詢",
  385              "法條查詢",
  386              "釋字",
  387              "憲判",
  388          ]
  389      )
  390
  391
  392  def _with_legal_workflow_footer(reply: str, query: str, *, tool_used: bool = True) -> str:
  393      workflow = detect_legal_workflow(text=query, mode="legal")
  394      return append_workflow_footer(reply, workflow, tool_used=tool_used)
  395
  396
  397  def _mcp_lookup_allowed() -> bool:
  398      return taiwan_legal_mcp_enabled() and taiwan_legal_mcp_available()
  399
  400
  401  def _legaltech_mcp_lookup_allowed() -> bool:
  402      return legaltech_mcp_enabled()
  403
  404
  405  def _augment_judgments_with_legaltech_mcp(
  406      query: str,
  407      judgments: Dict[str, Any],
  408      *,
  409      case_type: str = "",
  410      limit: int = 3,
  411  ) -> Dict[str, Any]:
  412      if not _legaltech_mcp_lookup_allowed() or not str(query or "").strip():
  413          return judgments
  414      primary = _payload_with_high_quality_judgments(judgments)
  415      remote = search_practical_judgments_via_legaltech(
  416          query,
  417          case_type=case_type,
  418          limit=int(os.environ.get("MAGI_LEGALTECH_TAIWAN_LAW_MCP_MAX_RESULTS", str(limit)) or str(limit)),
  419          fulltext_limit=int(os.environ.get("MAGI_LEGALTECH_TAIWAN_LAW_MCP_FULLTEXT_LIMIT", "2") or "2"),
  420      )
  421      if remote.get("success"):
  422          return merge_judgment_sources(
  423              primary,
  424              # Keep official-JID discovery candidates until the next step can
  425              # bind them to a local official full text.  Search-result snippets
  426              # are intentionally not treated as summaries or draft evidence.
  427              remote,
  428              limit=limit,
  429          )
  430      if not primary.get("success"):
  431          return remote
  432      return primary
  433
  434
  435  def _augment_judgments_with_mcp(
  436      query: str,
  437      judgments: Dict[str, Any],
  438      *,
  439      case_type: str = "",
  440      limit: int = 3,
  441  ) -> Dict[str, Any]:
  442      if not _mcp_lookup_allowed():
  443          return judgments
  444      primary = _payload_with_high_quality_judgments(judgments)
  445      mcp_judgments = search_practical_judgments_via_mcp(
  446          query,
  447          case_type=case_type,
  448          limit=int(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_MAX_RESULTS", str(limit)) or str(limit)),
  449          fulltext_limit=int(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_FULLTEXT_LIMIT", "1") or "1"),
  450      )
  451      if mcp_judgments.get("success"):
  452          return merge_judgment_sources(primary, _payload_with_high_quality_judgments(mcp_judgments), limit=limit)
  453      if not primary.get("success"):
  454          return mcp_judgments
  455      return primary
  456
  457
  458  def _tlr_lookup_allowed() -> bool:
  459      value = str(os.environ.get("MAGI_TWLEGALRAG_AUGMENT", "1")).strip().lower()
  460      return tw_legal_rag_enabled() and value not in {"0", "false", "no", "off"}
  461
  462
  463  def _twlegalrag_cache_enabled() -> bool:
  464      # External discovery results must not be written into the canonical
  465      # ``court_judgments`` table before an official/local exact-copy check.
  466      value = str(os.environ.get("MAGI_TWLEGALRAG_CACHE_HITS", "0")).strip().lower()
  467      return value not in {"0", "false", "no", "off"}
  468
  469
  470  def _normalize_tlr_judgment_date(value: Any) -> Optional[str]:
```

### skills/judgment-collector/action.py

**位置：** [skills/judgment-collector/action.py:650](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judgment-collector/action.py#L650)<br>

夜間官方來源欠量、失敗 wrapper 重試與 bounded MCP gap fill。

```python
  650      try:
  651          os.makedirs(os.path.dirname(path), exist_ok=True)
  652          tmp = path + ".tmp"
  653          with open(tmp, "w", encoding="utf-8") as f:
  654              json.dump(obj, f, ensure_ascii=False, indent=2)
  655          os.replace(tmp, path)
  656          return True
  657      except Exception:
  658          return False
  659
  660
  661  def _load_code_config() -> dict:
  662      cfg: dict = {}
  663      for p in (
  664          os.path.join(CODE_DIR, "json", "config.json"),
  665          os.path.join(CODE_DIR, "config.json"),
  666      ):
  667          try:
  668              if os.path.exists(p):
  669                  with open(p, "r", encoding="utf-8") as f:
  670                      obj = json.load(f) or {}
  671                  if isinstance(obj, dict):
  672                      cfg = obj
  673                      break
  674          except Exception:
  675              continue
  676
  677      return cfg
  678
  679
  680  def _get_jdg_credentials() -> tuple[str, str, str]:
  681      """
  682      司法院官方 API 帳密來源（優先順序）：
  683      1) env: JUDICIAL_API_USER/JUDICIAL_API_PASSWORD
  684      2) env: JDG_API_USER/JDG_API_PASSWORD
  685      3) code/json/config.json -> judicial_api_user/judicial_api_pass
  686      4) 明示允許時才回退 judicial.record_username/record_password
  687      """
  688      user = _env("MAGI_JUDICIAL_API_USER") or _env("JUDICIAL_API_USER") or _env("JDG_API_USER")
  689      pwd = _env("MAGI_JUDICIAL_API_PASS") or _env("MAGI_JUDICIAL_API_PASSWORD") or _env("JUDICIAL_API_PASSWORD") or _env("JDG_API_PASSWORD")
  690      if user and pwd:
  691          return user, pwd, "env"
  692
  693      cfg = _load_code_config()
  694      if isinstance(cfg, dict):
  695          user = str(cfg.get("judicial_api_user") or "").strip()
  696          pwd = str(cfg.get("judicial_api_pass") or "").strip()
  697          if user and pwd:
  698              return user, pwd, "config.judicial_api_*"
  699
  700          judicial = cfg.get("judicial")
  701          if isinstance(judicial, dict):
  702              user = str(judicial.get("api_user") or "").strip()
  703              pwd = str(judicial.get("api_password") or "").strip()
  704              if user and pwd:
  705                  return user, pwd, "config.judicial.api_*"
  706
  707              allow_record_fallback = (_env("JUDICIAL_API_ALLOW_RECORD_FALLBACK", "0") or "0").lower() in {
  708                  "1",
  709                  "true",
  710                  "yes",
  711                  "on",
  712              }
  713              if allow_record_fallback:
  714                  user = str(
  715                      _env("MAGI_JUDICIAL_RECORD_USERNAME") or judicial.get("record_username") or ""
  716                  ).strip()
  717                  pwd = str(
  718                      _env("MAGI_JUDICIAL_RECORD_PASSWORD") or judicial.get("record_password") or ""
  719                  ).strip()
  720                  if user and pwd:
  721                      return user, pwd, "env/config.judicial.record_*"
  722      return "", "", ""
  723
  724
  725  def _is_jdg_service_window(dt: Optional[datetime] = None) -> bool:
  726      """
  727      依官方說明預設 00:00-06:00。end hour 採「不含」。
  728      """
  729      dt = dt or datetime.now()
  730      h = int(dt.hour)
  731      s = int(JDG_API_WINDOW_START_HOUR % 24)
  732      e = int(JDG_API_WINDOW_END_HOUR % 24)
  733      if s == e:
  734          return True
  735      if s < e:
  736          return s <= h < e
  737      return (h >= s) or (h < e)
  738
  739
  740  # ★ SSL context for judicial API: Python 3.14 + OpenSSL 3.x enforces strict
  741  #   X.509 checks (e.g. Subject Key Identifier).  The judicial.gov.tw cert chain
  742  #   lacks SKI, so we build a verified-but-relaxed context using certifi CA bundle
  743  #   and ~VERIFY_X509_STRICT.  Falls back to unverified only as last resort.
  744  _jdg_ssl_ctx_cache: dict[str, Any] = {}  # {"ctx": ssl.SSLContext}
  745
  746
  747  def _build_jdg_ssl_context() -> ssl.SSLContext:
  748      """Build a verified SSL context that tolerates missing SKI extension."""
  749      cached = _jdg_ssl_ctx_cache.get("ctx")
  750      if cached is not None:
  751          return cached
  752      try:
  753          import certifi
  754          ca_bundle = certifi.where()
  755      except ImportError:
  756          ca_bundle = None
  757      ctx = ssl.create_default_context(cafile=ca_bundle)
  758      # Relax X.509 strict mode — keeps chain/hostname validation but skips SKI check.
  759      ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
  760      _jdg_ssl_ctx_cache["ctx"] = ctx
  761      return ctx
  762
  763
  764  def _jdg_post_json(path: str, payload: dict, timeout_sec: int = 25) -> Any:
  765      url = JDG_API_BASE + "/" + path.lstrip("/")
  766      data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
  767      req = _urlrequest.Request(
  768          url,
  769          data=data,
  770          headers={"Content-Type": "application/json"},
  771          method="POST",
  772      )
  773      allow_insecure_fallback = (_env("JUDICIAL_API_ALLOW_INSECURE_SSL", "0") or "0").lower() in {
  774          "1",
  775          "true",
  776          "yes",
  777          "on",
  778      }
  779
  780      ctx = _build_jdg_ssl_context()
  781      try:
  782          with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=ctx) as resp:
  783              raw = resp.read().decode("utf-8", errors="replace")
  784          return json.loads(raw or "{}")
  785      except _urlerror.HTTPError as e:
  786          body = ""
  787          try:
  788              body = e.read().decode("utf-8", errors="replace")
  789          except Exception:
  790              body = ""
  791          return {"error": f"HTTP {getattr(e, 'code', 'ERR')}", "body": body[:800]}
  792      except Exception as e:
  793          msg = str(e)
  794          cert_err = ("CERTIFICATE_VERIFY_FAILED" in msg) or ("certificate verify failed" in msg.lower())
  795          if cert_err and allow_insecure_fallback:
  796              logger.warning("[AUDIT] SSL relaxed context 仍失敗，降級為不驗證模式（url=%s）", url[:120])
  797              try:
  798                  unverified = ssl._create_unverified_context()
  799                  _jdg_ssl_ctx_cache["ctx"] = unverified
  800                  with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=unverified) as resp:
  801                      raw = resp.read().decode("utf-8", errors="replace")
  802                  obj = json.loads(raw or "{}")
  803                  if isinstance(obj, dict):
  804                      obj.setdefault("_ssl_insecure_fallback", True)
  805                  return obj
  806              except Exception as e2:
  807                  return {"error": str(e2)[:240], "ssl_insecure_fallback": True}
  808          return {"error": msg[:240]}
  809
  810
  811  def _jdg_download_file(url: str, dest_path: str, timeout_sec: int = 30) -> dict:
  812      src = str(url or "").strip()
  813      if not src:
  814          return {"ok": False, "error": "empty_url"}
  815      req = _urlrequest.Request(
  816          src,
  817          headers={"User-Agent": "MAGI/1.0"},
  818          method="GET",
  819      )
  820      ctx = _build_jdg_ssl_context()
  821      try:
  822          os.makedirs(os.path.dirname(dest_path), exist_ok=True)
  823          with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=ctx) as resp:
  824              data = resp.read()
  825          tmp = dest_path + ".tmp"
  826          with open(tmp, "wb") as f:
  827              f.write(data)
  828          os.replace(tmp, dest_path)
  829          return {"ok": True, "path": dest_path, "bytes": len(data)}
  830      except _urlerror.HTTPError as e:
  831          return {"ok": False, "error": f"HTTP {getattr(e, 'code', 'ERR')}"}
  832      except Exception as e:
  833          return {"ok": False, "error": str(e)[:240]}
  834
  835
  836  def _jid_slug(jid: str) -> str:
  837      s = str(jid or "").strip()
  838      if not s:
  839          return "jid_empty"
  840      head = hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]
  841      tail = re.sub(r"[^0-9A-Za-z_\-]+", "_", s)[:64].strip("_")
  842      if not tail:
  843          tail = "jid"
  844      return f"{head}_{tail}"
  845
  846
  847  def _sanitize_filename(name: str, default: str = "file") -> str:
  848      s = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]+', "_", str(name or "").strip())
  849      s = re.sub(r"\s+", " ", s).strip().strip(".")
  850      return (s[:180] if s else default)
```

### scripts/v3_release_bundle.py

**位置：** [scripts/v3_release_bundle.py:1158](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_release_bundle.py#L1158)<br>

不可變 release bundle 建立與封存。

```python
 1158          raise ReleaseBundleError(f"allowlist snapshot does not exactly match tracked HEAD: {detail}")
 1159      paths = sorted(snapshot_by_path)
 1160      if not _git_diff_is_clean(source_root, cached=True, paths=paths):
 1161          raise ReleaseBundleError("allowlist contains staged changes relative to HEAD")
 1162      if not _git_diff_is_clean(source_root, cached=False, paths=paths):
 1163          raise ReleaseBundleError("allowlist contains modified or deleted files relative to HEAD")
 1164      for path, entry in snapshot_by_path.items():
 1165          mode, object_id = tree[path]
 1166          expected_mode = 0o555 if mode == "100755" else 0o444
 1167          if entry.mode != expected_mode:
 1168              raise ReleaseBundleError(f"allowlist file mode differs from HEAD: {path}")
 1169          blob = _git_output(source_root, "cat-file", "blob", object_id)
 1170          if len(blob) != entry.size or hashlib.sha256(blob).hexdigest() != entry.sha256:
 1171              raise ReleaseBundleError(f"allowlist file content differs from HEAD: {path}")
 1172
 1173
 1174  def build_release_bundle(
 1175      source_root: Path,
 1176      staging_dir: Path,
 1177      *,
 1178      release_id: str,
 1179      commit: str | None = None,
 1180      expected_snapshot_sha256: str | None = None,
 1181      now: datetime | None = None,
 1182      require_supply_chain: bool = False,
 1183  ) -> dict[str, Any]:
 1184      """Copy the V3 allowlist and atomically mark a verified staging bundle complete."""
 1185
 1186      if not RELEASE_ID_RE.fullmatch(release_id):
 1187          raise ReleaseBundleError("release_id contains unsupported characters")
 1188      source = source_root.resolve(strict=True)
 1189      try:
 1190          supply_chain = validate_release_supply_chain_binding(source)
 1191      except (OSError, SupplyChainError) as exc:
 1192          if require_supply_chain or (source / "config/v3_supply_chain_binding.json").exists():
 1193              raise ReleaseBundleError(f"release supply-chain binding failed: {exc}") from exc
 1194          supply_chain = {"schema": "magi.supply-chain-binding/v1", "ok": False, "reason": "not_bound"}
 1195      _top_level, head = _git_identity(source)
 1196      resolved_commit = commit or head
 1197      if not COMMIT_RE.fullmatch(resolved_commit):
 1198          raise ReleaseBundleError("commit must be a lowercase 40- or 64-character hex digest")
 1199      if resolved_commit != head:
 1200          raise ReleaseBundleError("release commit does not exactly match git HEAD")
 1201      staging = _assert_safe_staging(source, staging_dir)
 1202      before = snapshot_sources(source)
 1203      _verify_git_snapshot(source, before, resolved_commit)
 1204      privacy_audit = _release_privacy_audit(source, before)
 1205      source_snapshot_sha256 = _snapshot_digest(before)
 1206      if expected_snapshot_sha256 is not None:
 1207          if not SHA256_RE.fullmatch(expected_snapshot_sha256):
 1208              raise ReleaseBundleError("expected_snapshot_sha256 must be a lowercase SHA-256 digest")
 1209          if source_snapshot_sha256 != expected_snapshot_sha256:
 1210              raise ReleaseBundleError("source snapshot does not match expected_snapshot_sha256")
 1211      git_provenance = _git_provenance(source)
 1212      staging.mkdir(mode=0o755)
 1213      for entry in before:
 1214          _copy_entry(source, staging, entry)
 1215      after_copy = snapshot_sources(source)
 1216      if after_copy != before:
 1217          raise ReleaseBundleError("source snapshot changed while building release bundle")
 1218      _verify_git_snapshot(source, after_copy, resolved_commit)
 1219      copied_files = [entry.manifest_entry() for entry in before]
 1220      for entry in before:
 1221          destination = staging / entry.path
 1222          if destination.is_symlink() or not destination.is_file():
 1223              raise ReleaseBundleError(f"staged file is missing or unsafe: {entry.path}")
 1224          if destination.stat().st_size != entry.size or _sha256_file(destination) != entry.sha256:
 1225              raise ReleaseBundleError(f"staged file verification failed: {entry.path}")
 1226      generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
 1227      manifest = {
 1228          "schema_version": 1,
 1229          "release_id": release_id,
 1230          "commit": resolved_commit,
 1231          "generated_at": generated_at,
 1232          "immutable": True,
 1233          "source_snapshot_sha256": source_snapshot_sha256,
 1234          "release_sha256": source_snapshot_sha256,
 1235          "git_provenance": git_provenance,
 1236          "source_file_count": len(copied_files),
 1237          "source_allowlist": [*SOURCE_DIRECTORIES, *REQUIRED_FILES],
 1238          "required_package_files": list(REQUIRED_PACKAGE_FILES),
 1239          "required_test_targets": list(REQUIRED_TEST_TARGETS),
 1240          "test_execution_evidence": "not_evaluated_by_bundle_builder",
 1241          "privacy_audit": privacy_audit,
 1242          "supply_chain_evidence": supply_chain,
 1243          "excluded_components": sorted(EXCLUDED_COMPONENTS),
 1244          "excluded_mutable_files": sorted(EXCLUDED_MUTABLE_FILES),
 1245          "external_template_contract": {
 1246              "hearing_leave_template_env": "MAGI_HEARING_LEAVE_TEMPLATE_PATH",
 1247              "bundled_local_template": False,
 1248          },
 1249          "files": copied_files,
 1250      }
 1251      manifest_bytes = _write_json_exclusive(staging / MANIFEST_NAME, manifest)
```

### scripts/v3_cutover/activation.py

**位置：** [scripts/v3_cutover/activation.py:187](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/activation.py#L187)<br>

active marker 與 activation transaction。

```python
  187          os.close(directory)
  188      return _sha256_bytes(data)
  189
  190
  191  def _load(path: Path, *, description: str) -> dict[str, Any]:
  192      try:
  193          metadata = path.lstat()
  194      except OSError as exc:
  195          raise CutoverError(f"{description} is unavailable: {exc}") from exc
  196      if (
  197          stat.S_ISLNK(metadata.st_mode)
  198          or not stat.S_ISREG(metadata.st_mode)
  199          or stat.S_IMODE(metadata.st_mode) != 0o600
  200          or metadata.st_nlink != 1
  201      ):
  202          raise CutoverError(f"{description} is unsafe")
  203      try:
  204          value = json.loads(path.read_text(encoding="utf-8"))
  205      except (OSError, UnicodeError, json.JSONDecodeError) as exc:
  206          raise CutoverError(f"{description} is invalid: {exc}") from exc
  207      if not isinstance(value, dict):
  208          raise CutoverError(f"{description} must be a JSON object")
  209      return value
  210
  211
  212  def active_release_marker(
  213      path: Path,
  214      *,
  215      expected_release: str,
  216      expected_release_id: str | None = None,
  217      expected_release_root: Path | None = None,
  218      expected_manifest_sha256: str | None = None,
  219  ) -> dict[str, Any]:
  220      payload = _load(path, description="active release marker")
  221      if (
  222          payload.get("schema") != MARKER_SCHEMA
  223          or payload.get("schema_version") != 1
  224          or payload.get("release") != expected_release
  225          or not isinstance(payload.get("transaction_id"), str)
  226          or not payload["transaction_id"]
  227      ):
  228          raise CutoverError("active release marker identity mismatch")
  229      expected = {
  230          "release_id": expected_release_id,
  231          "release_root": str(expected_release_root) if expected_release_root else None,
  232          "release_manifest_sha256": expected_manifest_sha256,
  233      }
  234      for key, value in expected.items():
  235          if value is not None and payload.get(key) != value:
  236              raise CutoverError(f"active release marker {key} mismatch")
  237      return payload
  238
  239
  240  def verify_active_release_snapshot(
  241      marker: Mapping[str, Any],
  242      journal: Mapping[str, Any],
  243      *,
  244      expected_release: str,
  245      allowed_phases: frozenset[str],
  246  ) -> dict[str, Any]:
  247      """Verify a marker/journal pair and return its PII-free stable identity.
  248
  249      A durable restart lease can bind the stable identity, while every restart
  250      must still prove that the hash-chained journal remains in an explicitly
  251      active phase. Entering rollback therefore revokes restart admission before
  252      any service stop is attempted.
  253      """
  254      if expected_release not in {"v2", "v3"}:
  255          raise CutoverError("active release snapshot expected release is invalid")
  256      if (
  257          type(allowed_phases) is not frozenset
  258          or not allowed_phases
  259          or any(
  260              type(phase) is not str
  261              or phase not in {*TRANSITIONS, *ROTATION_TRANSITIONS}
  262              for phase in allowed_phases
  263          )
  264      ):
  265          raise CutoverError("active release snapshot phases are invalid")
  266      try:
  267          raw = json.dumps(
  268              {"marker": marker, "journal": journal},
  269              ensure_ascii=False,
  270              sort_keys=True,
  271              separators=(",", ":"),
  272              allow_nan=False,
  273          ).encode("utf-8")
  274          if not 1 <= len(raw) <= 8 * 1024 * 1024:
  275              raise CutoverError("active release snapshot exceeds its byte bound")
```

### gui/magi_menubar.py

**位置：** [gui/magi_menubar.py:925](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/gui/magi_menubar.py#L925)<br>

business/health payload 到人類狀態的轉換。

```python
  925          return CHECK_WAITING_TEXT
  926      if state == "idle":
  927          return "未啟用"
  928      return ATTENTION_TEXT
  929
  930
  931  def _business_module_status_from_payload(payload: dict) -> dict:
  932      result_ok = {}
  933      if isinstance(payload, dict):
  934          results = payload.get("results") if isinstance(payload.get("results"), list) else []
  935          for item in results:
  936              if not isinstance(item, dict):
  937                  continue
  938              name = str(item.get("name") or "").strip()
  939              if not name:
  940                  continue
  941              if "ok" in item:
  942                  result_ok[name] = bool(item.get("ok"))
  943              elif "success" in item:
  944                  result_ok[name] = bool(item.get("success"))
  945
  946      modules = {}
  947      for label, checks in BUSINESS_MODULE_CHECKS.items():
  948          state_info = _checks_state(result_ok, checks)
  949          state = state_info["state"]
  950          modules[label] = {
  951              **state_info,
  952              "label": _label_for_state(state, OPERATIONAL_TEXT),
  953          }
  954
  955      factory = {}
  956      for label, checks in FACTORY_CHECKS.items():
  957          state_info = _checks_state(result_ok, checks)
  958          state = state_info["state"]
  959          factory[label] = {
  960              **state_info,
  961              "label": _label_for_state(state, CHECK_PASSED_TEXT),
  962          }
  963
  964      credential = _checks_state(result_ok, ("token_health_refresh",))
  965      credential_state = credential["state"]
  966      return {
  967          "ok": bool(payload.get("ok") if isinstance(payload, dict) else False),
  968          "result_ok": result_ok,
  969          "modules": modules,
  970          "factory": factory,
  971          "credential": {
  972              **credential,
  973              "label": _label_for_state(credential_state, OPERATIONAL_TEXT),
  974          },
  975      }
  976
  977
  978  def _business_module_status_failure(reason: str, *, returncode: int | None = None) -> dict:
  979      """Represent the current live-check round without reusing an older report."""
  980      status = _business_module_status_from_payload({"ok": False, "results": []})
  981      for group_name in ("modules", "factory"):
  982          for info in status.get(group_name, {}).values():
  983              if isinstance(info, dict):
  984                  info["state"] = "attention"
  985                  info["label"] = "本輪檢查失敗"
  986      credential = status.get("credential")
  987      if isinstance(credential, dict):
  988          credential["state"] = "attention"
  989          credential["label"] = "本輪檢查失敗"
  990      status.update({"ok": False, "failed_reason": str(reason or "live_check_failed")[:160]})
  991      if returncode is not None:
  992          status["returncode"] = int(returncode)
  993      return status
  994
  995
  996  def _health_state_from_payload(kind: str, payload: dict, *, age_sec: float | None = None) -> dict:
  997      spec = HEALTH_ARTIFACTS[kind]
  998      label = str(spec["label"])
  999      if not isinstance(payload, dict) or not payload:
 1000          return {"state": "attention", "label": f"{label}無法解析", "detail": "健康報告不存在或不是有效的 JSON。"}
 1001      if age_sec is not None and age_sec > int(spec["max_age_sec"]):
 1002          return {"state": "waiting", "label": f"{label}檢查逾時", "detail": f"健康報告已超過 {round(age_sec / 3600, 1)} 小時未更新。"}
 1003      summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
 1004      requires_human = payload.get("requires_human") if isinstance(payload.get("requires_human"), list) else []
 1005      errors = int(summary.get("error_count") or 0)
 1006      warnings = int(summary.get("warning_count") or 0)
 1007      nested_failed = []
 1008      nested_missing = []
 1009      nested_stale = []
 1010      for container_name in ("health", "runtime_health"):
 1011          container = payload.get(container_name)
 1012          if not isinstance(container, dict):
 1013              continue
 1014          if isinstance(container.get("failed"), list):
 1015              nested_failed.extend(container["failed"])
 1016          if isinstance(container.get("missing"), list):
 1017              nested_missing.extend(container["missing"])
 1018          if isinstance(container.get("stale"), list):
 1019              nested_stale.extend(container["stale"])
 1020      errors = max(
 1021          errors,
 1022          int(summary.get("failed_health_count") or 0)
 1023          + int(summary.get("missing_health_count") or 0),
 1024          len(nested_failed) + len(nested_missing),
 1025      )
 1026      warnings = max(
 1027          warnings,
 1028          int(summary.get("stale_health_count") or 0),
 1029          len(nested_stale),
 1030      )
 1031      if payload.get("ok") is not True or requires_human or errors or warnings:
 1032          reasons = []
 1033          for item in payload.get("unresolved_issue_ids") or []:
 1034              reasons.append(str(item))
 1035          for item in payload.get("failed") or []:
 1036              if isinstance(item, dict):
 1037                  reasons.append(f"{item.get('path') or item.get('name') or '檢查項目'}：{item.get('reason') or item.get('detail') or '失敗'}")
 1038          for item in nested_failed + nested_missing + nested_stale:
 1039              if isinstance(item, dict):
 1040                  reasons.append(
 1041                      f"{item.get('name') or item.get('path') or '檢查項目'}："
 1042                      f"{item.get('reason') or item.get('detail') or '未通過'}"
 1043                  )
 1044              elif item:
 1045                  reasons.append(str(item))
 1046          if requires_human:
 1047              reasons.extend(str(item.get("reason") or item) if isinstance(item, dict) else str(item) for item in requires_human)
 1048          detail = "\n".join(dict.fromkeys(reason for reason in reasons if reason))
 1049          if not detail:
 1050              detail = f"錯誤 {errors} 項、警告 {warnings} 項；請開啟健康報告查看。"
 1051          # Warnings and human review items mean the service is still
 1052          # operational.  Reserve red for an active error (or an unclassified
 1053          # failed report), and use amber for review/retry work.
 1054          hard_failure = errors > 0 or (
 1055              payload.get("ok") is not True
 1056              and warnings == 0
 1057              and not requires_human
 1058          )
```

### magi_v3/process_monitor.py

**位置：** [magi_v3/process_monitor.py:118](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/process_monitor.py#L118)<br>

Golem 與 Menubar 共用的實際 worker、owner ancestry 與持續殭屍分類契約。

```python
  118  def _is_shell_command_wrapper(command: str) -> bool:
  119      try:
  120          argv = shlex.split(command or "", posix=True)
  121      except ValueError:
  122          return True
  123      if not argv or Path(argv[0]).name not in _SHELL_NAMES:
  124          return False
  125      return any(token == "-c" or (token.startswith("-") and "c" in token[1:]) for token in argv[1:3])
  126
  127
  128  def _worker_marker(command: str, worker_markers: Iterable[str]) -> str:
  129      """Return a worker marker only for the actual Python process.
  130
  131      A shell command line can contain the complete future Python command.  It is
  132      a launcher, not a worker, and must never inflate worker/orphan counts.
  133      """
  134      if _is_shell_command_wrapper(command):
  135          return ""
  136      if not _PYTHON_NAME_RE.fullmatch(_argv_head(command)):
  137          return ""
  138      return next((marker for marker in worker_markers if marker in command), "")
  139
  140
  141  def _core_marker(command: str, core_markers: Iterable[str]) -> str:
  142      if _is_shell_command_wrapper(command):
  143          return ""
  144      return next((marker for marker in core_markers if marker in command), "")
  145
  146
  147  def _is_managed_parent(command: str, core_markers: Iterable[str]) -> bool:
  148      if _is_shell_command_wrapper(command):
  149          return False
  150      return bool(_core_marker(command, core_markers)) or any(
  151          marker in command for marker in _MANAGED_PARENT_MARKERS
  152      )
  153
  154
  155  def _is_orphan_worker(
  156      worker: Mapping[str, Any],
  157      rows_by_pid: Mapping[int, Mapping[str, Any]],
  158      managed_pids: set[int],
  159  ) -> bool:
  160      """A worker is orphaned when its ancestry reaches init without a MAGI owner."""
  161      current: Mapping[str, Any] = worker
  162      seen = {int(worker.get("pid") or 0)}
  163      for _ in range(32):
  164          parent_pid = int(current.get("ppid") or 0)
  165          if parent_pid <= 1:
  166              return True
  167          if parent_pid in managed_pids:
  168              return False
  169          if parent_pid in seen:
  170              return True
  171          seen.add(parent_pid)
  172          parent = rows_by_pid.get(parent_pid)
  173          if parent is None:
  174              return True
  175          current = parent
  176      return True
  177
  178
  179  @dataclass
  180  class ZombiePersistence:
  181      """Suppress sub-five-second exit/reap transitions on every UI."""
  182
  183      persistence_seconds: float = 5.0
  184      first_seen: dict[tuple[int, int], float] = field(default_factory=dict)
  185
  186      def persistent_ids(
  187          self, observed: Mapping[tuple[int, int], Mapping[str, Any]], *, now: float
  188      ) -> set[tuple[int, int]]:
  189          next_seen: dict[tuple[int, int], float] = {}
  190          persistent: set[tuple[int, int]] = set()
  191          for identity in observed:
  192              first = self.first_seen.get(identity, now)
  193              next_seen[identity] = first
  194              if now - first >= self.persistence_seconds:
  195                  persistent.add(identity)
  196          self.first_seen = next_seen
  197          return persistent
```

<a id="appB"></a>
# 附錄 B. 全部 production 原始碼索引

Production Python 共 **1,096** 檔。每列顯示 path、行數、SHA 前 12 碼與前幾個符號；所有符號與完整 SHA 在 JSON 索引。

### api/（228 檔）

- [`api/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/__init__.py)｜27 行｜`7d79b664f6ca`｜__getattr__

- [`api/admin_allowlist.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/admin_allowlist.py)｜93 行｜`59202c01f555`｜_split_csv, _load_file_ids, get_discord_admin_ids, get_line_admin_user_ids, get_telegram_admin_ids, ensure_agent_dir

- [`api/agentic/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/__init__.py)｜74 行｜`f7d41258332f`｜—

- [`api/agentic/contracts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/contracts.py)｜659 行｜`d685f938d1a6`｜SideEffectLevel, SideEffectLevel.rank, StepStatus, StepStatus.terminal, PlanStatus, PlanStatus.terminal, _nonempty, _confidence, _json_safe, _mapping

- [`api/agentic/control.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/control.py)｜674 行｜`ee23d3297ef0`｜_utcnow, _iso, _parse_iso, _owner_hash, _token_hash, _reply_hash, _safe_summary, _default_db_path, PlanProposal, PlanProposal.user_message

- [`api/agentic/http_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py)｜607 行｜`3434ff3621e3`｜_start_agent_gateway_trace, _finish_agent_gateway_trace, _abort_agent_gateway_trace, _auth_and_identity, _body, _text, _int, _bool, _plan_id, _error

- [`api/agentic/mcp_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/mcp_gateway.py)｜794 行｜`a6a2d8619f3c`｜AgentGatewayError, AgentGatewayError.__init__, _text, _optional_text, _bounded_int, _bounded_bool, _validate_id, _validate_plan_id, _validate_token, MagiAgentGatewayConfig

- [`api/agentic/mcp_http.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/mcp_http.py)｜365 行｜`fb8ff97808d8`｜McpHttpSecurityError, _truthy, _https_url, protected_resource_metadata_url, OAuthResourceConfig, OAuthResourceConfig.__post_init__, OAuthResourceConfig.from_env, OAuthResourceConfig.protected_resource_metadata, OAuthAccess, LocalJwtVerifier

- [`api/agentic/planner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/planner.py)｜250 行｜`a2de0bcb3458`｜InvalidTransition, topological_order, ready_steps, pending_confirmations, derive_plan_status, build_plan, confirm_intent, confirm_step, transition_step, cancel_plan

- [`api/agentic/shadow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/shadow.py)｜152 行｜`85f4a4bed7fe`｜should_publish_public_agent_status, public_category_for_message, public_tool_category, observe_start, observe_finish, _result_failed, _public_result_text

- [`api/agentic/telemetry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/telemetry.py)｜478 行｜`e7f52710c52b`｜build_public_agent_status, write_public_agent_status, public_agent_status_path, _strip_private_fields, _is_private_key, _mapping, _first, _raw_value, _allowed_value, _intent_category

- [`api/agents/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agents/__init__.py)｜12 行｜`2bc47f782d10`｜—

- [`api/agents/models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agents/models.py)｜42 行｜`c26970a826b0`｜utcnow, AgentSpec, AgentMessage, TeamSpec, AgentResponse

- [`api/agents/runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agents/runtime.py)｜101 行｜`3e2b170a868a`｜AgentRuntime, AgentRuntime.respond, TeamRuntime, TeamRuntime.register_agent, TeamRuntime.list_agents, TeamRuntime.dispatch, AgentCoordinator, AgentCoordinator.register_agent, AgentCoordinator.create_team, AgentCoordinator.add_agent_to_team

- [`api/answer_provenance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/answer_provenance.py)｜277 行｜`2f63746263a2`｜_label_source, _extract_web_titles, _meaningful_memories, build_provenance_footer, store_provenance, get_last_provenance, format_correction_context

- [`api/app_factory.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/app_factory.py)｜232 行｜`31c3b61c1340`｜_env_truthy, _configured_https_public_url, _formal_deployment_mode, _https_enforced, _is_sensitive_static_request, create_base_app, create_base_app._block_sensitive_static_files, install_error_handlers, install_error_handlers.handle_500, install_security_headers

- [`api/authz.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/authz.py)｜275 行｜`114583144444`｜_get_calling_user_id, _log_access, _check_api_key, _env_truthy, _extract_api_key, _formal_saas_mode, _tenant_header_matches, require_api_key, require_api_key.decorated_function, require_role

- [`api/autopilot_artifacts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/autopilot_artifacts.py)｜99 行｜`d206012f9b3d`｜_resolve_root, _resolve_runtime_dir, get_autopilot_runtime_dir, get_kill_reason_path, get_legacy_kill_reason_path, get_kill_log_path, write_kill_reason, read_kill_reason, cleanup_stale_kill_reason_files

- [`api/blueprints/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/__init__.py)｜49 行｜`05da9c869fc9`｜__getattr__

- [`api/blueprints/admin_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py)｜3775 行｜`c1487317b391`｜_mutable_static_dir, _agent_state_dir, _runtime_state_dir, _token_health_report_candidates, _apple_vision_capability_metadata, _read_faiss_metadata, _browser_core_health_hard_timeout, _wants_json_response, _render_health_html, _render_health_html.state_for

- [`api/blueprints/cookie_cutter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/cookie_cutter.py)｜533 行｜`83d28515aadb`｜_cookie_generation_child, _plain_error, _cookie_error_response, _reject_oversized_multipart_before_parse, _client_key, _within_rate_limit, _read_upload_bounded, _generate_bounded, _attest_generated_bundle, _image_dimensions

- [`api/blueprints/dashboard_pages.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py)｜1314 行｜`d814edb20404`｜_maintenance_manual_response, _mutable_static_dir, _runtime_dir, _worldmonitor_report_dir, _is_mobile_app_request, _maybe_force_mobile_app_login, _force_mobile_app_reauth_before_dashboard_page, _strip_trailing_dot, _tailscale_cli, _load_tailscale_status

- [`api/blueprints/exam_tutor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/exam_tutor.py)｜3961 行｜`c55b724e4fce`｜ExamTutorInputError, ExamTutorInputError.__init__, _clip_text, _is_judicial_bar_second_stage, _estimated_answer_visual_lines, _decode_plain_text, _extract_uploaded_text, _collect_text_field, _official_moex_url, _public_https_url

- [`api/blueprints/golem_console.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/golem_console.py)｜386 行｜`1dfe15b54bcb`｜_read_json, _is_admin_user, _admin_forbidden_response, _mask_secret, _parse_env_file, _write_env_values, _write_pending_env_values, _api_key_status, _tail, _recent_files

- [`api/blueprints/lottery.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/lottery.py)｜226 行｜`a9317de52047`｜_norm_header, _find_column, _clean_cell, mask_name, mask_phone, mask_address, _read_csv_rows, _read_excel_rows, parse_upload, _public_row

- [`api/blueprints/osc_accounting.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py)｜635 行｜`d92241ad2fd7`｜_require_accounting_operator, _get_osc_helpers, _accounting_transaction_filters, _accounting_transactions_sql, _as_amount, _iso_date_text, osc_accounting_transactions_api, osc_accounting_transactions_xlsx_api, osc_accounting_transactions_xlsx_api._cleanup, osc_accounting_transaction_detail_api

- [`api/blueprints/osc_cases.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py)｜11329 行｜`26ab4db5ff8e`｜_directory_io_route, _directory_io_route.wrapped, _osc_audit_file_event, _osc_shell_nas_helper_url, _osc_photo_path, _osc_existing_resource_path, _osc_clean_case_reason, _osc_case_uses_consumer_debt_lawyer, _osc_default_case_lawyer, _osc_normalize_case_lawyer

- [`api/blueprints/osc_debt.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py)｜981 行｜`fbcfaa35e173`｜_require_osc_debt_login, _require_debt_operator, _export_dir, _secure_temp_upload_path, _file_meta, _save_doc, _debt_osc_exec, _debt_setting_value, _prepare_debt_document_data, _first_debt_value

- [`api/blueprints/osc_files.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py)｜2394 行｜`4d250f2e0b93`｜_directory_io_route, _directory_io_route.wrapped, _audit_file_event, _require_file_operator, _actor_namespace, _locked_file, _share_store_guard, _osc_shell_nas_helper_url, _osc_shell_nas_helper_request, _nas_metadata_cache_get

- [`api/blueprints/osc_gcal.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_gcal.py)｜369 行｜`229b00d13704`｜_require_gcal_operator, _get_osc_exec, _get_setting, _write_token_atomic, _load_creds, _run_current_gcal_sync, _build_redirect_uri, _calendar_token_health, gcal_status, gcal_auth_start

- [`api/blueprints/osc_pdf.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_pdf.py)｜2657 行｜`87b54c6ab9b0`｜_LazyFitz, _LazyFitz._load, _LazyFitz.__getattr__, _upload_dir, _path_from_request, _safe_bool, _repo_root, _osc_exec, _load_headless_todo_helpers, _load_headless_date_helpers

- [`api/blueprints/osc_settings.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py)｜274 行｜`f9d03a9ebd27`｜_get_osc_helpers, osc_settings_api, osc_setting_detail_api, osc_courts_api, osc_court_detail_api, osc_legal_aid_branches_api, osc_legal_aid_branch_detail_api, osc_discord_test_api

- [`api/blueprints/raziel.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py)｜729 行｜`399b5e4495fb`｜_has_classifier_script, _has_raziel_outputs, _safe_glob, _safe_is_dir, _candidate_roots, _candidate_roots.add, _raziel_root, _config_path, _script_path, _script_supported_modes

- [`api/blueprints/sentencing_trends.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/sentencing_trends.py)｜48 行｜`5506b079d1b6`｜_search_public_judgment_candidates, page, search_api

- [`api/blueprints/video_studio.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/video_studio.py)｜541 行｜`55921fadccc0`｜_plain_error, _client_key, _render_child, _render_asset_child, _controller_rss, _process_group_absent, _render_bounded, _write_private, _bounded_uploads, _render_assets_bounded

- [`api/blueprints/web_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py)｜1304 行｜`dc15b6e82ad5`｜public_intent_summary, _parse_etime_to_sec, _process_monitor_markers, _agent_state_dir, _mutable_static_dir, _chat_upload_dir, _magi_web_outputs_dir, _extract_chat_upload_text, _extract_chat_upload_text_for_task, _safe_web_url

- [`api/case_display.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/case_display.py)｜128 行｜`2763257959db`｜normalize_person_name, is_unusable_client_label, _path_parts, folder_client_name, should_trust_folder_client_name, display_client_name, display_case_label

- [`api/case_path_mapper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/case_path_mapper.py)｜937 行｜`7f24b012ad3e`｜_load_local_dotenv, _is_dir_accessible, _is_dir_accessible._check, _is_stale_mount_path, _is_file_provider_or_user_mount, _write_safe_local_candidate, _discover_volume, _mount_output_lines, _mounted_volume_for_path, _contains_existing_symlink

- [`api/channel_context.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/channel_context.py)｜187 行｜`089da5b6616f`｜ChannelContext, _get_magi_root, reverse_lookup_telegram_topic, reverse_lookup_discord_channel, should_skip_nl_router

- [`api/command_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/command_registry.py)｜144 行｜`f17fa8aaf778`｜CommandContext, _CommandEntry, CommandRegistry, CommandRegistry.__init__, CommandRegistry.command, CommandRegistry.command.decorator, CommandRegistry.register, CommandRegistry.dispatch, CommandRegistry.list_commands

- [`api/commands/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/commands/__init__.py)｜1 行｜`14a5997b9b43`｜—

- [`api/commands/apple_commands.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/commands/apple_commands.py)｜175 行｜`d238cd6cd5a0`｜register_apple_commands, register_apple_commands._handle_trial_event, register_apple_commands._handle_spotlight_search, register_apple_commands._handle_notify_test

- [`api/commands/forensic_transcript_commands.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/commands/forensic_transcript_commands.py)｜38 行｜`5c9fdf3f06ac`｜_start_or_status, _handle_forensic_live, register_forensic_transcript_commands

- [`api/config_overlay.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/config_overlay.py)｜129 行｜`acb1475a8fc8`｜_first_env, apply_env_overlay

- [`api/coordinator/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/coordinator/__init__.py)｜12 行｜`6088728df9f9`｜—

- [`api/court_case_type_map.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/court_case_type_map.py)｜215 行｜`1dccbd48090c`｜classify_case_type, case_type_matches_db

- [`api/csrf_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/csrf_guard.py)｜337 行｜`97b1e17940a5`｜_generate_csrf_token, _is_webhook_endpoint, _is_api_endpoint, _has_valid_api_key, _env_truthy, _is_test_mode, _is_explicit_cli_request, _is_decorated_csrf_exempt_endpoint, _should_check_csrf, _get_csrf_token_from_request

- [`api/db_failover.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/db_failover.py)｜308 行｜`0ff509fff692`｜_tcp_check, probe_remote, _switch_to_local, _switch_to_remote, _sync_local_to_remote, _monitor_loop, _do_check, get_osc_host, get_osc_port, get_failover_status

- [`api/db_helper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/db_helper.py)｜57 行｜`534271b37384`｜_default_config, get_connection, get_cursor

- [`api/db_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/db_sync.py)｜380 行｜`54b06288f464`｜get_strategy, _get_primary_keys, _has_column, _get_all_columns, _quote, _pk_tuple, _build_insert_ignore, _build_replace_into, _row_values, _fetch_rows_by_pks

- [`api/debt_document_generator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/debt_document_generator.py)｜1988 行｜`d34652153678`｜get_debt_address_book_dir, _read_address_text, _write_address_text, get_robot_source_status, scan_evidence_folder, ValidationError, ValidationError.__init__, create_error_response, validate_application_data, validate_asset_statement_data

- [`api/debug_capture.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/debug_capture.py)｜72 行｜`e9e03e32eff0`｜_ensure_dirs, save_debug_screenshot, save_debug_html, _append_md, cleanup_old

- [`api/deep_task_control.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/deep_task_control.py)｜309 行｜`bf35efac7c91`｜DeepAdmission, DeepTaskDeferred, assess_deep_admission, infer_local_deep_task_type, DeepTaskController, DeepTaskController.__init__, DeepTaskController.run, _resource_governor_allows_deep, runtime_owner_worker_transaction_free, runtime_owner_worker_transaction_free._json_url

- [`api/discord_bot.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/discord_bot.py)｜1804 行｜`2e4beb745698`｜_sigchld_handler, _sigterm_handler, _normalize_discord_output_text, _split_discord_chunks, _audit_preview, _audit_sha1, _append_channel_delivery_audit, _save_last_channel_id, _load_last_channel_id, _load_last_line_callback_ts

- [`api/discord_channel_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/discord_channel_router.py)｜849 行｜`6e9870fcb804`｜_canonical_topic_key, _infer_sub_topic, _is_unknown_business_topic, _is_noop_completion_notification, _load_channel_map, _load_all_routed_channel_ids, _reverse_lookup_channel, _normalize_channel_name, infer_topic_from_channel_metadata, save_channel_map

- [`api/domains/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`api/domains/acquisition_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/acquisition_flow.py)｜147 行｜`10c9c8cbf8ec`｜auto_acquire_and_execute, auto_acquire_and_execute._notify, auto_acquire_and_execute._rebuild_embed_cache, auto_acquire_and_execute._run_forge_with_retry, auto_acquire_and_execute._run_forge_with_lock

- [`api/domains/calendar_agent.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/calendar_agent.py)｜670 行｜`d4926ceeb2f2`｜CalendarIntent, DateMatch, TimeMatch, CalendarEvent, CalendarDraft, CalendarDraft.needs_clarification, CalendarDraft.is_mutating, ConfirmationResult, EventChecks, EventChecks.has_duplicate

- [`api/domains/calendar_agent_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/calendar_agent_runtime.py)｜406 行｜`d3b728f760e6`｜CalendarRepository, CalendarRepository.list_events, CalendarRepository.create_event, CalendarRepository.update_event, CalendarRepository.cancel_event, looks_like_calendar_request, _draft_to_dict, _draft_from_dict, _draft_from_dict._dt, _session_id

- [`api/domains/calendar_metadata.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/calendar_metadata.py)｜42 行｜`57710564c6c1`｜encode_calendar_source, decode_calendar_source

- [`api/domains/calendar_sync_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/calendar_sync_policy.py)｜74 行｜`dea2cb42ca9c`｜is_osc_only_overdue_confirmation, osc_only_overdue_confirmation_sql, is_osc_only_calendar_review, osc_only_calendar_review_sql

- [`api/domains/case_file_operation_lock.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/case_file_operation_lock.py)｜114 行｜`2e8c2a9e2825`｜_pid_alive, _read_pid, case_file_operation_lock_path, acquire_case_file_operation_lock, release_case_file_operation_lock

- [`api/domains/codex_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/codex_flow.py)｜31 行｜`cb6cee5a1066`｜parse_codex_distributed_features, format_codex_distributed_status, handle_codex_distributed_command

- [`api/domains/collab_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/collab_flow.py)｜63 行｜`49a0fb6a49ee`｜get_collaboration_status

- [`api/domains/export_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/export_flow.py)｜116 行｜`586a24704e80`｜export_summary_docx_or_txt

- [`api/domains/judgment_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_flow.py)｜1508 行｜`de64940c7a99`｜_get_local_db_manager, extract_judgment_collect_payload, format_judgment_collect_result, _judgment_item_quality_issue, _is_high_quality_judgment_item, _high_quality_judgment_items, _judgment_quality_rejection_counts, _high_quality_judgment_count, _payload_with_high_quality_judgments, _run_skill_json

- [`api/domains/judgment_nvidia_summary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_nvidia_summary.py)｜515 行｜`b5939a7fa456`｜NvidiaSummaryResult, NvidiaSummaryResult.audit_dict, _candidate_records, _selection_prompt, _parse_json_object, _validate_selection, _render_summary, _source_bound_application_rescue, _can_pass_without_application, summarize_with_nvidia

- [`api/domains/judgment_official_source.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_official_source.py)｜124 行｜`44155082bcc5`｜normalize_judgment_date, is_official_judgment_url, _url_jid_matches, official_judgment_page_url, _date_from_full_text, validate_official_judgment_candidate

- [`api/domains/judgment_summary_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_summary_quality.py)｜920 行｜`89cccc1c60ac`｜_primary_issue_terms, _normalize_caption_issue, infer_case_issue, SummaryQuality, SummaryQuality.as_dict, PracticeSpan, _norm, _display_source_span, _section, _reason_section

- [`api/domains/judgment_value_filter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judgment_value_filter.py)｜195 行｜`1a8fe07efb6b`｜JudgmentValueDecision, JudgmentValueDecision.to_dict, _s, _jid_prefix, _is_upper_court, _has_substantive_signal, _text_head, classify_judgment_record, classify_jdoc_payload

- [`api/domains/judicial_api_backlog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judicial_api_backlog.py)｜170 行｜`e277b62a401e`｜_i, _f, format_count, format_duration_hours, build_backlog_interpretation, format_backlog_notice

- [`api/domains/judicial_api_cache.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judicial_api_cache.py)｜204 行｜`ae80096746f3`｜_default_shared_cache_root, _expand, _truthy, _ordinary_pytest_mode, _is_managed_mount_path, _managed_mountpoint, _managed_mount_is_mounted, _prepare_root, nas_judgment_cache_candidates, preferred_nas_judgment_cache_root

- [`api/domains/judicial_api_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/judicial_api_policy.py)｜157 行｜`53cc66db9c2f`｜judicial_api_load_mode, judicial_api_default, judicial_api_env_default, apply_judicial_api_env_defaults, judicial_api_policy_report

- [`api/domains/laf_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/laf_flow.py)｜827 行｜`985c31ea6e45`｜_parse_subprocess_result, _subprocess_payload_succeeded, _pending_stale_timeout_sec, _recover_pending_entries, _save_pending_path, load_laf_submit_pending, save_laf_submit_pending, update_laf_status_after_action, update_laf_status_after_action._case_status_for_laf_status, register_laf_go_live_submit_pending

- [`api/domains/market_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/market_flow.py)｜230 行｜`3a790404f100`｜load_market_watch_state, is_stock_like_token, looks_like_market_watchlist_reply, try_market_watchlist_quick_set, run_stock_briefing_command, _looks_like_capability_question, _strip_intent_prefixes

- [`api/domains/memory_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/memory_flow.py)｜239 行｜`6952f0aeee01`｜is_ambiguous_rule, handle_memory_confirmation_if_any, maybe_capture_user_rules, maybe_capture_chatlog

- [`api/domains/multimedia_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/multimedia_flow.py)｜1021 行｜`eeaf0e37d78d`｜_magi_root, _translation_rebuild_cache_path, _file_fingerprint, _load_translation_rebuild_cache, _save_translation_rebuild_cache, _try_rebuild_pdf_translation_delivery, vision_classify_and_route_image, handle_payment_proof_from_channel, handle_payment_proof_from_channel._run_payment_subprocess, handle_multimedia

- [`api/domains/schedule_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/schedule_flow.py)｜123 行｜`18ff59f11918`｜get_schedule

- [`api/domains/skill_interview_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/domains/skill_interview_flow.py)｜329 行｜`97a68e1f13ea`｜skill_interview_default_reply, skill_interview_cancel_reply, skill_interview_status_reply, skill_interview_split_items, parse_skill_interview_io, format_skill_interview_progress, render_skill_interview_question, start_skill_interview, finalize_skill_interview, handle_skill_interview_if_any

- [`api/durable_notifications.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/durable_notifications.py)｜81 行｜`e24116e3e7ae`｜_path, _load, _platform, enqueue, claim_for_user

- [`api/durable_rate_limit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/durable_rate_limit.py)｜300 行｜`3e2945d6c94b`｜_truthy, formal_saas_mode, default_database_path, hash_client_identity, inspect_rate_limit_storage, RateLimitDecision, RateLimitStorageError, DurableRateLimiter, DurableRateLimiter.__init__, DurableRateLimiter._prepare_path

- [`api/events/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/events/__init__.py)｜27 行｜`24bbb94bce67`｜—

- [`api/events/emitter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/events/emitter.py)｜85 行｜`907418a3471f`｜Subscription, Subscription.unsubscribe, EventEmitter, EventEmitter.__init__, EventEmitter._normalize_event_type, EventEmitter.subscribe, EventEmitter._unsubscribe, EventEmitter.emit, EventEmitter.add_jsonl_sink, EventEmitter.subscribers_for

- [`api/events/models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/events/models.py)｜101 行｜`bd2f0d3d4179`｜_utcnow, EventModel, EventModel.to_dict, EventModel.to_json, PreToolHookEvent, PostToolHookEvent, RouteDecisionEvent, FallbackEvent, MemoryWriteEvent, TaskLifecycleEvent

- [`api/events/sinks.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/events/sinks.py)｜107 行｜`677624e6582e`｜rotate_jsonl, JsonlSink, JsonlSink.__init__, JsonlSink.write, jsonl_sink, append_jsonl

- [`api/hallucination_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/hallucination_guard.py)｜205 行｜`9dba8b83777f`｜_compact_ref, classify_risk, check_fact_grounding, needs_grounding_check, rewrite_ungrounded_attribution, build_anti_hallucination_prompt_rules

- [`api/handlers/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`api/handlers/document_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/document_handler.py)｜1654 行｜`0960eea3c09c`｜strip_pdf_running_identifiers, normalize_tw_legal_translation_terms, normalize_txt_body, prepare_document_text_for_llm, prepare_document_text_for_llm._looks_like_heading, prepare_document_text_for_llm._clean_page, prepare_document_text_for_llm._clean_page._flush, prepare_document_text_for_llm._clean_page._should_join, polish_translated_document_text, translation_idiom_issues

- [`api/handlers/laf_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/laf_handler.py)｜421 行｜`61894b42cc3c`｜laf_portal_manual_links, laf_report_command_help, detect_laf_report_action, _clean_client_name, _looks_like_laf_action_name, parse_laf_report_payload, _expand_reason_keywords, parse_laf_status_update

- [`api/handlers/output_quality_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/output_quality_handler.py)｜779 行｜`7bc0822482eb`｜_norm_anchor, _anchors, _law_anchors, _chinese_integer, _money_anchors, _date_anchors, _duplicate_line_ratio, _meaningful_paragraph_count, _office_fidelity_metrics, _office_fidelity_issue

- [`api/handlers/summary_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/summary_handler.py)｜808 行｜`07498d8f57ef`｜summary_length_prompt, _is_synthetic_timeout_fallback, _summary_chunk_usable, summarize_text_resilient, summarize_text_resilient._chunk_by_paragraph, summarize_text_resilient._sample_evenly_chunks, summarize_text_resilient._summary_output_usable, summarize_text_resilient._extractive_fallback_summary, summarize_text_resilient._extractive_fallback_summary._clean_sentence, summarize_text_resilient._extractive_fallback_summary._extract_section_candidates

- [`api/handlers/text_processing_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/text_processing_handler.py)｜135 行｜`d241e69f54e5`｜sanitize_incoming_message, strip_intent_prefixes, redact_secrets, apply_long_dialog_guard, postprocess_router_reply, output_guard_issues

- [`api/handlers/translation_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/handlers/translation_handler.py)｜1583 行｜`905e40caa491`｜_doc_run_root, _atomic_write_json, _read_json, _translation_checkpoint_state_path, _build_document_glossary, _build_export_term_glossary, translate_text_complete, translate_text_complete._is_nvidia_model, translate_text_complete._should_verify_chunk, translate_text_complete._normalize_gtx_lang

- [`api/help_text.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/help_text.py)｜179 行｜`1ce37375d15b`｜build_help_text

- [`api/hooks/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/hooks/__init__.py)｜22 行｜`cbfbef4e3dfa`｜—

- [`api/hooks/bus.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/hooks/bus.py)｜152 行｜`c6d0e104b055`｜HookBus, HookBus.subscribe, HookBus.add_jsonl_sink, HookBus.publish, HookBus.pre_tool, HookBus.post_tool, HookBus.route_decision, HookBus.fallback, HookBus.memory_write

- [`api/hooks/events.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/hooks/events.py)｜17 行｜`f78df26da64b`｜—

- [`api/hooks/subscribers.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/hooks/subscribers.py)｜21 行｜`e216f2d0bea4`｜HookEventCollector, HookEventCollector.__call__, jsonl_hook_subscriber

- [`api/laf_branch_profiles.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/laf_branch_profiles.py)｜349 行｜`687cee6c5e74`｜LawFirmProfile, LafBranchProfile, LafBranchProfile.footer_text, _text, _configured_seed_value, normalize_branch_label, _load_config, _law_firm_profile_from_seed, fetch_law_firm_profile_from_db, get_law_firm_profile

- [`api/laf_case_classifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/laf_case_classifier.py)｜201 行｜`e8e781b0c185`｜is_administrative_laf_reason, normalize_laf_case_type, normalize_laf_case_fields, clean_laf_case_reason, is_pending_laf_reason, extract_laf_staff_case_hint, _strip_known_case_tokens

- [`api/laf_closing_transfer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/laf_closing_transfer.py)｜461 行｜`b3a81a029a62`｜LAFClosingTransferNotice, LAFClosingTransferNotice.safe_dict, _clean_mail_text, _field_map, _pick, _parse_staff, parse_laf_closing_transfer_notice, _db_fetch_one, _query_case_by_laf_number, _normalize_name

- [`api/laf_go_live_rules.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/laf_go_live_rules.py)｜156 行｜`6cfdac9ae07b`｜_contains_any, _path_text, is_opening_notice_filename, is_stored_pleading_proof, is_go_live_receipt_proof, is_consumer_debt_text, go_live_proof_files, go_live_notice_files, is_go_live_ready, go_live_missing_labels

- [`api/laf_poa_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/laf_poa_docx.py)｜2431 行｜`4aef1ac458ae`｜laf_poa_docx_enabled, laf_poa_docx_templates_enabled, laf_poa_static_templates_enabled, laf_poa_pdf_render_fallback_enabled, laf_poa_exact_pdf_layout_enabled, is_laf_power_of_attorney_pdf, laf_poa_docx_path, laf_poa_template_docx_path, laf_poa_case_docx_path, _text

- [`api/law_firm_contact.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/law_firm_contact.py)｜174 行｜`c5ac8ce19516`｜_text, _usable, _first, _requested_fields, resolve_lawyer_contact

- [`api/legal_research_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/legal_research_quality.py)｜624 行｜`dc9ebeffdf88`｜PrivacyDecision, PrivacyDecision.as_dict, prepare_external_legal_query, prepare_external_legal_query._redact, canonical_case_key, verification_state, is_draft_eligible, _court_authority_score, _query_terms, factual_similarity_score

- [`api/legal_workflow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/legal_workflow.py)｜350 行｜`9b1ace85b615`｜_copy, _find, _haystack, is_legal_workflow_candidate, select_practice_profile, select_legal_agent, detect_legal_workflow, workflow_prompt_block, append_workflow_footer, source_tag_for_provenance

- [`api/line_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/line_compat.py)｜307 行｜`97aa50736820`｜_env_flag, LineSDKUnavailableError, _BaseMessage, _BaseMessage.__init__, _CompatTextSendMessage, _CompatTextSendMessage.__init__, _CompatImageSendMessage, _CompatImageSendMessage.__init__, _UnavailableLineBotApi, _UnavailableLineBotApi.__init__

- [`api/model_config.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/model_config.py)｜183 行｜`fd6a4b5b6188`｜is_disallowed_model, _clean, _clean_model, _env_bool, _env_int, resolve_draft_model, mtp_draft_payload, is_text_model_alias, resolve_text_model, default_local_chat_models

- [`api/model_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/model_router.py)｜454 行｜`34446d242e55`｜ModelSpec, ModelSpec.from_dict, ResourceView, ResourceView.from_decision, ModelRouteDecision, ModelRouteDecision.to_dict, _env_bool, _env_float, load_registry, probe_active_models

- [`api/mysql_connector_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/mysql_connector_guard.py)｜97 行｜`1e01e733a36e`｜_env_on, _BlockedMySQLCExtLoader, _BlockedMySQLCExtLoader.create_module, _BlockedMySQLCExtLoader.exec_module, _BlockMySQLCExtFinder, _BlockMySQLCExtFinder.find_spec, install_mysql_cext_blocker, patch_mysql_connector_for_stability, patch_mysql_connector_for_stability._guarded_connect

- [`api/nas_mount_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/nas_mount_guard.py)｜850 行｜`58b7343eaa7f`｜_parse_env_file_values, _merge_share_csv, _load_local_nas_env_if_needed, resolve_nas_user, _synology_drive_available, get_synology_drive_fallback_path, get_lumi_fallback_path, get_share_available_path, get_share_mount_path, get_share_status

- [`api/orchestrator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/orchestrator.py)｜1633 行｜`4cd536f3c9c0`｜_v3_external_dir, _brain_sqlite_path, _magi_status_path, summarize_text, apply_manual_command, public_status_report, _get_handler, search_web, research_topic, fetch_url_content

- [`api/orchestrator_core.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/orchestrator_core.py)｜22 行｜`7d1a8c5e2b95`｜RuntimeFoundations

- [`api/osc/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/__init__.py)｜116 行｜`f9171853c6ca`｜__getattr__

- [`api/osc/accounting_bonus.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/accounting_bonus.py)｜779 行｜`6f233a4fb1f3`｜_get_osc_helpers, _as_float, _round_money, period_for_settlement_month, default_settlement_month, _settlement_date, _status_label, _calendar_months_between, _parse_iso_date, _transaction_month_scope_sql

- [`api/osc/accounting_sheet_import.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/accounting_sheet_import.py)｜1324 行｜`1a9a47421199`｜AccountingSheetRow, AccountingSheetFetch, AccountingImportError, SheetsAuthorizationRequired, _certification_fixture_fetch, _atomic_write_text, _backup_google_token, _persist_google_credentials_unlocked, _persist_google_credentials, is_revoked_google_token_error

- [`api/osc/accounting_summary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/accounting_summary.py)｜65 行｜`c56461162fce`｜accounting_summary_filters, accounting_totals_sql, accounting_by_category_sql, load_accounting_summary

- [`api/osc/calendar_event_time.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/calendar_event_time.py)｜126 行｜`8da810baceda`｜_number, is_timed_occurrence, resolve_calendar_time, require_calendar_time

- [`api/osc/calendar_sources.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/calendar_sources.py)｜98 行｜`19419a001a3e`｜_text, is_google_calendar_import, is_calendar_todo, todo_source_key, todo_source_label, calendar_todo_source_sql, osc_todo_source_sql, todo_source_api_fields, calendar_todo_to_event

- [`api/osc/case_defaults.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/case_defaults.py)｜147 行｜`18a6d94fa1b1`｜is_demo_lawyer, case_uses_consumer_debt_lawyer, _valid_lawyer, _read_setting, _read_env, default_case_lawyer, db_settings_getter, db_settings_getter._getter, normalize_case_lawyer

- [`api/osc/case_folder_schema.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/case_folder_schema.py)｜282 行｜`e08581d8a545`｜numbered_folder_name, judgment_folder_name, legacy_judgment_folder_name, strip_number_prefix, judgment_folder_aliases, legacy_judgment_folder_names, canonical_name_for_legacy_judgment_folder, canonicalize_case_subfolder_name, case_subfolders, closing_folder_names

- [`api/osc/case_intelligence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/case_intelligence.py)｜563 行｜`97387f0806b8`｜build_case_intelligence_snapshot, _fetch_cases, _fetch_recent_documents, _fetch_calendar_refs, _case_snapshot_base, _scan_known_subfolders, _doc_from_document_index, _doc_from_case_documents, _ref_from_todo, _ref_from_calendar_event

- [`api/osc/case_no_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/case_no_sync.py)｜538 行｜`32b232a52f13`｜extract_court_case_no, extract_division_from_text, extract_division_from_notice, _doc_date_sort_key, _case_no_char_priority, _case_no_source_priority, _candidate_sort_key, verify_filename_for_case, sync_case_no_from_notices, _update_db

- [`api/osc/client_ids.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/client_ids.py)｜36 行｜`6a4a08d33b54`｜is_canonical_client_id, next_client_id_from_existing, generate_next_client_id

- [`api/osc/document_reuse.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/document_reuse.py)｜671 行｜`fcc80614ff22`｜ReplacementRule, index_pleading_docx, build_pleading_index, reuse_docx_document, reuse_document, _validate_word_source, _find_soffice_binary, _convert_doc_to_docx, _default_filename, _unique_output_path

- [`api/osc/draft_learning.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/draft_learning.py)｜203 行｜`37efd358a737`｜_clean_text, _sha, _one_line, _norm_key, _diff_lessons, _line_delta, record_draft_feedback, _iter_events, _public_event, recent_draft_feedback

- [`api/osc/drafts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/drafts.py)｜1014 行｜`d0aa6213e4a9`｜_srv, _osc_exec, _osc_truthy, _osc_get_setting_value, _osc_unique_strings, _osc_collect_insights, _osc_read_reference_document, _osc_norm_path, _osc_local_path_candidates, _osc_guess_case_folder

- [`api/osc/drive_case_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/drive_case_sync.py)｜9404 行｜`6bcb43a54f44`｜_continue_signal_chain, DriveCaseSyncError, DriveCaseSyncDeadline, DriveCaseSyncStorageDeferred, local_hash_failure_code, is_retryable_local_hash_failure, is_storage_unavailable_error, is_download_target_storage_unavailable_error, BoundedEntries, BoundedEntries.__init__

- [`api/osc/folder_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/folder_utils.py)｜155 行｜`03af21d9587a`｜_env_truthy, _ordinary_test_mode, _looks_like_live_case_root, _assert_test_safe_case_folder_path, sanitize_folder_name, build_case_folder_name, resolve_type_folder, build_full_case_path, _create_folder_structure_unchecked, create_folder_structure

- [`api/osc/hearing_conflict_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/hearing_conflict_runtime.py)｜484 行｜`a687daf2522b`｜EnqueueAdmission, load_case, _open_status_sql, load_existing_schedules, _todo_start, _todo_end, _dedupe_schedules, check_candidate, _case_folder, _enrich_prior

- [`api/osc/hearing_conflicts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/hearing_conflicts.py)｜868 行｜`1965e05011cc`｜NormalizedSchedule, ConflictDecision, ConflictDecision.as_dict, _text, _truthy, _parse_datetime, _schedule_text, is_excluded_schedule, classify_schedule, normalize_schedule

- [`api/osc/insight_filters.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/insight_filters.py)｜170 行｜`bffe0b73fc77`｜normalize_insight_marker_text, is_extractive_fast_judgment_digest, mark_extractive_fast_digest_summary, is_non_extractable_legal_insight, displayable_insight_item, non_extractable_legal_insight_sql_where

- [`api/osc/judicial.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/judicial.py)｜376 行｜`d38d308dd01e`｜_osc_show_fast_insight_candidates, _osc_show_external_insight_candidates, _osc_probable_fast_judgment_candidate, _osc_court_summary_displayable, _osc_insight_merge_key, _osc_collect_insights, _osc_doc_kind_match, _osc_doc_kind_label

- [`api/osc/legaltech_taiwan_law_mcp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/legaltech_taiwan_law_mcp.py)｜346 行｜`a2c50fb1ff4b`｜legaltech_mcp_enabled, _endpoint, _timeout, _decode_json_or_sse, _post_jsonrpc, _structured_result, _official_urls, legaltech_tool_catalog, call_legaltech_tool, analyze_legal_intent_via_legaltech

- [`api/osc/preview.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/preview.py)｜426 行｜`6d8d42f159c5`｜_soffice_path, _cache_key, _cache_lru_cleanup, _write_text_pdf, _extract_office_text, _preview_office_text_fallback, preview_office_to_pdf, preview_heic_to_jpg, preview_csv_to_rows, preview_email

- [`api/osc/saas_workbench.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/saas_workbench.py)｜1537 行｜`4c93a8afe35f`｜_text, _norm, _one_line, _sha, _safe_int, _action, _rows, _row, _count, _read_json

- [`api/osc/taiwan_legal_mcp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/taiwan_legal_mcp.py)｜311 行｜`fcd032d6361c`｜taiwan_legal_mcp_root, taiwan_legal_mcp_available, taiwan_legal_mcp_enabled, _install_import_path, _maybe_await, _server_context, call_taiwan_legal_tool_async, call_taiwan_legal_tool, parse_taiwan_case_number, _first_text

- [`api/osc/tw_legal_rag.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/tw_legal_rag.py)｜399 行｜`20c7a1f93a19`｜TLRRetrievalError, TLRJudgment, tw_legal_rag_enabled, tw_legal_rag_base_url, tw_legal_rag_api_key, _loads_lenient, sanitize_tlr_query, TLRClient, TLRClient.__init__, TLRClient.__enter__

- [`api/osc/utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc/utils.py)｜2698 行｜`4672376fe31f`｜_NoUser, _osc_file_stage_slot, _osc_stage_bulkhead, _osc_stage_bulkhead.wrapped, _osc_directory_io_slot, _load_mysql, _load_local_dotenv, _osc_closed_share_aliases, _osc_canonical_active_share_windows, _osc_canonical_active_share_posix

- [`api/osc_document_generator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/osc_document_generator.py)｜289 行｜`9b4c81c256b0`｜set_font_style, generate_receipt, generate_poa, generate_engagement_agreement, generate_engagement_agreement.add_article

- [`api/pending_config.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pending_config.py)｜90 行｜`3416aec9f448`｜pending_env_update_path, write_pending_env_updates, _write_pending_env_updates_unlocked

- [`api/permissions/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/permissions/__init__.py)｜24 行｜`3808818f68f4`｜—

- [`api/permissions/enforcer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/permissions/enforcer.py)｜97 行｜`51d8f3bd5649`｜PermissionEnforcer, PermissionEnforcer.__init__, PermissionEnforcer.evaluate_command, PermissionEnforcer.evaluate_path, PermissionEnforcer.can_command, PermissionEnforcer.can_path, PermissionEnforcer._evaluate, PermissionEnforcer._format_rule_reason

- [`api/permissions/models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/permissions/models.py)｜151 行｜`1b71ceab822b`｜PermissionMode, PermissionMode.coerce, PermissionEffect, PermissionEffect.coerce, _normalize_text, _normalize_path, _match_prefix, PermissionRule, PermissionRule.matches_command, PermissionRule.matches_path

- [`api/permissions/policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/permissions/policy.py)｜40 行｜`63ba11fe61e3`｜PermissionPolicy, PermissionPolicy.from_rules, PermissionPolicy.with_mode, PermissionPolicy.with_rules

- [`api/permissions/rules.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/permissions/rules.py)｜43 行｜`2f7a562e7677`｜allow_command, deny_command, allow_path, deny_path

- [`api/pipelines/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/__init__.py)｜29 行｜`36d686a37548`｜__getattr__

- [`api/pipelines/attachment_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/attachment_pipeline.py)｜161 行｜`79c1ddb6d0f6`｜handle_multimedia, process_image, load_recent_attachments, save_recent_attachments, prune_recent_attachments, remember_recent_attachment, get_recent_attachment, looks_like_attachment_followup, has_recent_attachment_followup, maybe_reuse_recent_attachment

- [`api/pipelines/chat_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/chat_pipeline.py)｜487 行｜`dd3c2c02b9d0`｜estimate_tokens, append_history, compress_history, build_conversation_history, record_assistant_reply, handle_query, handle_query._run_query_background, handle_chat_async, handle_chat_async._deferred_chat, handle_chat_async._run_chat_background

- [`api/pipelines/command_dispatch.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/command_dispatch.py)｜2719 行｜`cd4dbee78f6a`｜split_heavy_prefix, _delivery_result_ok, _lazy_brain, _lazy_brain._wrapper, research_topic, fetch_url_content, summarize_text, _strip_cmd_prefix, _parse_subprocess_json, _short_diag_text

- [`api/pipelines/command_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/command_pipeline.py)｜49 行｜`b3ba0116e1a9`｜_get_handler, list_skills, handle_command

- [`api/pipelines/fuzzy_match.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/fuzzy_match.py)｜296 行｜`adb7f416854c`｜_is_admin_keyword, fuzzy_correct, suggest_correction

- [`api/pipelines/message_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/message_pipeline.py)｜4445 行｜`37645524c4fd`｜split_heavy_prefix, _state_agent_dir, fetch_url_content, fetch_url_sections, get_brain_status, _looks_like_payment_slip_scan_request, _resolve_message_route_intent, _maybe_direct_case_lookup, _maybe_direct_case_statistics, _handle_docx_chat_edit_if_any

- [`api/pipelines/message_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/message_router.py)｜1578 行｜`be6c0308a2a1`｜split_heavy_prefix, read_openclaw_primary_model, magi_capability_overview, extract_laf_progress_reported_target, handle_laf_progress_reported_message, extract_osc_todo_completion_target, _run_court_hearing_done, handle_osc_todo_completion_message, handle_gibberish_report, quick_fixed_reply

- [`api/pipelines/skill_dispatch.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/skill_dispatch.py)｜1249 行｜`35c75e3cba8a`｜run_transcribe_guidance, looks_like_capability_question, dispatch_safe_semantic_skill, generic_skill_dispatch, polish_skill_output, output_looks_messy, basic_cleanup, try_safe_semantic_skill_route, dispatch_doc_producer, dispatch_case_management

- [`api/pipelines/skill_listing.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/skill_listing.py)｜91 行｜`21e44e0cf6c9`｜_legacy_openclaw_enabled, iter_skill_roots, build_skill_list_response

- [`api/pipelines/specialized_commands.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/specialized_commands.py)｜540 行｜`73e859cba550`｜looks_like_inline_summary_command, _clean_inline_summary_body, _extract_inline_summary_text, run_labor_law_command, run_inline_translation_command, run_translate_file_command, run_inline_summary_command, run_inline_summary_command._summary_requests_more_content, run_inline_summary_command._inline_extractive_summary, run_court_hearing_command

- [`api/platforms/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/platforms/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`api/platforms/remote_health_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/platforms/remote_health_gate.py)｜266 行｜`3d8998640061`｜PeerConfig, PeerState, RemoteHealthGate, RemoteHealthGate.__init__, RemoteHealthGate.register, RemoteHealthGate.is_reachable, RemoteHealthGate.mark_success, RemoteHealthGate.mark_failure, RemoteHealthGate.circuit_status, RemoteHealthGate.all_status

- [`api/platforms/runtime_dir.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/platforms/runtime_dir.py)｜215 行｜`563930da43ff`｜_append_lock, _enabled, _magi_root, root, pending, metrics, cron_state, _validate_name, atomic_write_json, atomic_append_jsonl

- [`api/platforms/safe_process.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/platforms/safe_process.py)｜1293 行｜`e9212b165d09`｜SafeRunResult, _SafeProcessCleanupError, _SafeProcessCancelledError, reset_for_test, _current_python_alias_target, _is_current_python_alias, _validate_argv, _filter_env, _cap, _ProcessIdentity

- [`api/poa_chat_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/poa_chat_handler.py)｜703 行｜`b02b4026e474`｜_load_state, _save_state, _clear_user, _load_config, _parse_case_type, _parse_role, _try_extract_fields_from_init, _build_poa_preview, _build_contract_preview, _build_receipt_preview

- [`api/product_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/product_runtime.py)｜239 行｜`fe6483c6edcc`｜_load_json, _save_json, _load_config, _normalize_codex_mode, _normalize_portal_env, load_product_runtime, save_product_runtime, update_product_runtime, _config_profile, get_product_profile

- [`api/red_phone.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/red_phone.py)｜12 行｜`fbc858568609`｜notify

- [`api/request_guards.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/request_guards.py)｜240 行｜`c8bd6ae921c5`｜_path_matches_prefix, _is_cloudflare_tunnel_request, _env_truthy, _formal_saas_mode, _expected_tenant_id, install_request_guards, install_request_guards._block_retired_legacy_entrypoints, install_request_guards._limit_cloudflare_tunnel_surface, install_request_guards._enforce_formal_saas_session_tenant, install_request_guards._audit_protected_mutation_start

- [`api/routing/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/__init__.py)｜72 行｜`be4085ef0227`｜—

- [`api/routing/clarification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/clarification.py)｜341 行｜`2e9fa032986a`｜ClarificationDecision, ClarificationResolution, resolve_recent_case_reference, detect_clarification_need, _pending_store, _pending_key, remember_clarification, _case_scope_answer, resolve_pending_clarification, request_clarification_if_needed

- [`api/routing/command_prefixes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/command_prefixes.py)｜56 行｜`80ba168ef3d2`｜normalize_command_prefix_text, split_heavy_prefix, strip_heavy_prefix

- [`api/routing/context.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/context.py)｜111 行｜`baeea51696fe`｜RoutingContext, RoutingContext.has_attachment, RoutingContext.is_admin, RoutingContext.with_overrides, RoutingContext.as_dict

- [`api/routing/datastore_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/datastore_registry.py)｜146 行｜`5437079db6cf`｜Datastore, _load_registry, _load_registry._env_or, _ensure_loaded, reload, get_datastore, get_connection_params, list_datastores

- [`api/routing/inference_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/inference_router.py)｜193 行｜`c94948698651`｜InferenceRouter, InferenceRouter.__init__, InferenceRouter.resolve, InferenceRouter.resolve_embedding, InferenceRouter._provider_to_service, InferenceRouter._resolve_endpoint

- [`api/routing/intent_contract.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/intent_contract.py)｜533 行｜`d49aaa255346`｜split_heavy_prefix, strip_heavy_prefix, IntentDecision, NormalizedIntent, compact_message, looks_like_cancel_request, looks_like_correction_request, looks_like_model_capability_query, looks_like_tool_capability_query, looks_like_busy_meta_query

- [`api/routing/model_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/model_registry.py)｜204 行｜`7371e3d6026a`｜ModelRole, _resolve_env, _load_registry, _ensure_loaded, reload, get_role_model, is_alias, resolve_model, list_roles, get_role

- [`api/routing/models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/models.py)｜230 行｜`638b34ef4944`｜ServiceTarget, ServiceTarget.as_dict, FallbackPlan, FallbackPlan.primary, FallbackPlan.has_fallback, FallbackPlan.as_dict, FallbackPlan.from_targets, RoutingDecision, RoutingDecision.success, RoutingDecision.requires_admin

- [`api/routing/node_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/node_registry.py)｜169 行｜`65b3f04397a7`｜NodeService, Node, Node.preferred_ip, _load_registry, _ensure_loaded, reload, get_node, get_node_ip, get_node_url, list_nodes

- [`api/routing/office_cognition.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/office_cognition.py)｜282 行｜`ab4ffe470d47`｜DomainCandidate, OfficeUnderstanding, OfficeUnderstanding.needs_clarification, _side_effect, _operation, _has_case_target, _clarification, assess_office_request

- [`api/routing/policy_engine.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/policy_engine.py)｜245 行｜`af34f24d18ef`｜PolicyEngine, PolicyEngine.__init__, PolicyEngine._load_overrides, PolicyEngine.reload_overrides, PolicyEngine.get_override, PolicyEngine.evaluate, PolicyEngine._fallback

- [`api/routing/request_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/request_router.py)｜215 行｜`fbfcf73af183`｜RoutingStage, RoutingStage.__call__, _keyword_stage, _semantic_stage, RequestRouter, RequestRouter.__init__, RequestRouter.route, RequestRouter.add_stage, RequestRouter.insert_stage, RequestRouter._pick_best

- [`api/routing/route_decision.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/route_decision.py)｜27 行｜`3f91302825e4`｜build_route_decision

- [`api/routing/route_explanations.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/route_explanations.py)｜128 行｜`6b38b4a77ab3`｜RouteExplanation, RouteExplanation.as_dict, RouteExplanationCollector, RouteExplanationCollector.__init__, RouteExplanationCollector.record, RouteExplanationCollector.record_rejection, RouteExplanationCollector.as_trace, RouteExplanationCollector.dispatched_skill, RouteExplanationCollector.had_dispatch, RouteExplanationCollector.__len__

- [`api/routing/route_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/route_policy.py)｜283 行｜`3fbae2189f10`｜get_skill_min_confidence, _skill_aliases, is_high_risk_skill, user_declines_tool_dispatch, direct_skill_run_denial_reason, is_generic_word_only, should_dispatch_skill, build_route_explanation, should_cache_intent, _tokenize

- [`api/routing/service_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/service_registry.py)｜145 行｜`8a7de88b26de`｜ServiceEndpoint, ServiceEndpoint.base_url, _load_registry, _ensure_loaded, reload, get_service, get_service_url, get_service_host_port, list_services

- [`api/routing/telemetry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/routing/telemetry.py)｜190 行｜`17493da49079`｜RoutingTelemetry, RoutingTelemetry.__init__, RoutingTelemetry.record, RoutingTelemetry.record_raw, RoutingTelemetry.read_all, RoutingTelemetry.summary, RoutingTelemetry.summary_from_disk, RoutingTelemetry._build_entry, RoutingTelemetry._write_line

- [`api/runtime_diagnostics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/runtime_diagnostics.py)｜100 行｜`f4363b3c3b10`｜_normalize_error_text, classify_runtime_error, classify_model_health

- [`api/runtime_paths.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/runtime_paths.py)｜550 行｜`a0698e167086`｜_env_path, _env_flag, dotenv_override_allowed, _unique_paths, ensure_path_on_sys_path, ensure_magi_root_on_sys_path, ensure_orch_on_sys_path, get_magi_root_dir, get_runtime_dir, get_agent_dir

- [`api/saas_audit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/saas_audit.py)｜279 行｜`f3ce185ec60a`｜_sha, _actor, _request_meta, _clean_value, file_ref, _canonical_event_bytes, _event_hash, verify_audit_chain, _safe_append, append_audit_event

- [`api/saas_readiness.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/saas_readiness.py)｜417 行｜`514d546f70c2`｜_truthy, _env_truthy, deployment_mode, formal_saas_enabled, _audit_event_path, _public_source_root, _sha256, _check, _value_strong, _url_is_https

- [`api/saas_schema.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/saas_schema.py)｜402 行｜`4f4bf93a6bb9`｜_truthy, tenant_id_from_env, auth_db_config, osc_db_config, _safe_ident, _safe_tenant_literal, _connect, _table_exists, _column_exists, _index_exists

- [`api/sentencing_trends.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/sentencing_trends.py)｜936 行｜`06ab0ee8047c`｜_public_exclusion_reason, _compact, _clean_text, _normalized_iso_date_filter, format_roc_date, _cn_number, _duration_months, _main_text, _signature_judges, _sentence_items

- [`api/server.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py)｜1003 行｜`4fd0f73f4bea`｜_sigchld_handler, _rate_limit_client_identity, _check_rate_limit, _rate_limit_retry_after, _load_runtime_config, _skill_doc_path, _skill_action_path, _skill_summary, _nerv_product_runtime_payload, _list_skill_docs

- [`api/server_auth.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server_auth.py)｜39 行｜`b3ae6261eaa7`｜env_truthy, sanitize_login_next, default_tenant_id, tenant_id_from_user_data

- [`api/session/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/__init__.py)｜75 行｜`cfb794bd753f`｜—

- [`api/session/context.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/context.py)｜11 行｜`f185d440c2fe`｜—

- [`api/session/context_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/context_builder.py)｜191 行｜`010e8ef2fa19`｜SessionContextBuilder, SessionContextBuilder.__init__, SessionContextBuilder.build, SessionContextBuilder.assemble, SessionContextBuilder.render_text, SessionContextBuilder.build_prompt, build_session_context, assemble_session_messages

- [`api/session/context_labels.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/context_labels.py)｜153 行｜`63837d2a7892`｜TrustTier, classify_trust_tier, label_single_memory, label_memory_context, build_trust_system_instruction

- [`api/session/conversation_history.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/conversation_history.py)｜119 行｜`b037bcd5cff7`｜_utcnow, ConversationHistoryStore, ConversationHistoryStore.__init__, ConversationHistoryStore._connect, ConversationHistoryStore._ensure_db, ConversationHistoryStore.append, ConversationHistoryStore.last_n, ConversationHistoryStore.last_sessions, ConversationHistoryStore.clear_session, ConversationHistoryStore.purge_expired

- [`api/session/history.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/history.py)｜88 行｜`a56b3e419955`｜_resolve_store_and_args, SessionHistory, SessionHistory.__init__, SessionHistory.append, SessionHistory.list, SessionHistory.tail, SessionHistory.last, append_message, list_messages, tail_messages

- [`api/session/memory_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/memory_policy.py)｜213 行｜`318edda25ad4`｜MemoryWriteDecision, _allow_assistant_chatlog, evaluate_memory_write, _looks_like_prompt_leak, _resolve_provenance, _allow, _deny

- [`api/session/models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/models.py)｜217 行｜`4e918ee69b6d`｜utcnow, _required_text, _json_object, _as_utc, _parse_timestamp, SessionKey, SessionKey.__post_init__, SessionKey.to_dict, SessionKey.serialize, SessionKey.from_dict

- [`api/session/pending.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/pending.py)｜73 行｜`8aa9c0085820`｜_resolve_store_and_args, SessionPendingManager, SessionPendingManager.__init__, SessionPendingManager.set, SessionPendingManager.update, SessionPendingManager.get, SessionPendingManager.snapshot, SessionPendingManager.clear, set_pending_state, update_pending_state

- [`api/session/provenance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/provenance.py)｜235 行｜`9780368d306e`｜_to_bool, _to_confidence, _normalize_source_type, MemoryProvenance, MemoryProvenance.trust_label, MemoryProvenance.as_dict, namespace_for_source_type, default_confidence_for_source, parse_source_provenance, build_source_signature

- [`api/session/references.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/references.py)｜272 行｜`9be46c69be33`｜_as_utc, ReferenceCandidate, ReferenceCandidate.to_dict, ReferenceResolution, ReferenceResolution.requires_clarification, ReferenceResolution.to_dict, _active_references, _candidates, _from_candidates, _ambiguous

- [`api/session/store.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/store.py)｜348 行｜`517292a4db41`｜_as_utc, _parse_timestamp, SessionStore, SessionStore.__init__, SessionStore._clone, SessionStore.bind_identity, SessionStore.get_identity_binding, SessionStore.session_id_for, SessionStore.remember_recent, SessionStore.list_recent

- [`api/session/summary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/summary.py)｜82 行｜`af98b840b972`｜_resolve_store_and_args, SessionSummaryManager, SessionSummaryManager.__init__, SessionSummaryManager.add, SessionSummaryManager.list, SessionSummaryManager.latest, add_summary, list_summaries, latest_summary

- [`api/session/verified_fact_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/session/verified_fact_gate.py)｜76 行｜`72ca791ff2c4`｜is_reflexive_query, promote_to_verified

- [`api/startup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/startup.py)｜2094 行｜`036406b3a9d8`｜_startup_feature_enabled, _inprocess_laf_gmail_monitor_enabled, _load_dotenv_value, _load_json, _write_json_atomic, _maybe_handle_laf_captcha_reply, _maybe_handle_generic_captcha_reply, _is_loopback_base_url, _normalize_public_base_url, _is_trycloudflare_base_url

- [`api/tasks/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tasks/__init__.py)｜30 行｜`88402050ecf2`｜—

- [`api/tasks/execution.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tasks/execution.py)｜170 行｜`ca784632fc51`｜_resolve_runtime_and_args, TaskExecutionResult, TaskExecutionResult.success, TaskExecution, TaskExecution.__init__, TaskExecution.create, TaskExecution.start, TaskExecution.update, TaskExecution.complete, TaskExecution.fail

- [`api/tasks/models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tasks/models.py)｜50 行｜`65cae48c7c77`｜utcnow, TaskStatus, TaskRecord, TaskRecord.as_dict

- [`api/tasks/runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tasks/runtime.py)｜44 行｜`b27b8c4a8ccd`｜TaskRuntime, TaskRuntime.__init__, TaskRuntime.register, TaskRuntime.update, TaskRuntime.complete, TaskRuntime.fail, TaskRuntime.cancel, TaskRuntime.get, TaskRuntime.list, TaskRuntime.active

- [`api/tasks/store.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tasks/store.py)｜138 行｜`5b9cfc386db2`｜TaskStore, TaskStore.__init__, TaskStore._clone, TaskStore.register, TaskStore.get, TaskStore.update, TaskStore.complete, TaskStore.fail, TaskStore.cancel, TaskStore.list

- [`api/thread_pools.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/thread_pools.py)｜56 行｜`34d98205e5ae`｜_pool_size, shutdown_all

- [`api/tools/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/__init__.py)｜39 行｜`c809b5ac759f`｜—

- [`api/tools/base.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/base.py)｜11 行｜`77f9a8139f1b`｜ToolExecutor, ToolExecutor.execute

- [`api/tools/contracts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/contracts.py)｜154 行｜`cd10b2e38786`｜ToolSideEffect, normalize_tool_side_effect, GeneralErrorCategory, GeneralError, GeneralError.as_dict, ToolContext, ToolSpec, ToolSpec.__post_init__, ToolSpec.as_dict, ToolResult

- [`api/tools/executors.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/executors.py)｜55 行｜`c2f304bc37a8`｜CallableToolExecutor, CallableToolExecutor.execute, HttpJsonToolExecutor, HttpJsonToolExecutor.execute

- [`api/tools/policies.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/policies.py)｜264 行｜`63d304a728ff`｜ToolRequirement, classify_tool_requirement, requires_fresh_web_source, format_tool_failure_response

- [`api/tools/registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/registry.py)｜510 行｜`577a37296c54`｜RegisteredTool, RegisteredTool.as_dict, ToolRegistry, ToolRegistry.__init__, ToolRegistry.register, ToolRegistry.register_callable, ToolRegistry.get, ToolRegistry.list_tools, ToolRegistry.execute, ToolRegistry._contract_metadata

- [`api/tools/tool_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools/tool_router.py)｜93 行｜`586f719501ee`｜ToolRouteResult, ToolRouteResult.as_context, route_to_tool

- [`api/tools_api.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py)｜4007 行｜`5f7e50a08251`｜_sigchld_handler, _to_bool, _host_is_blocked_for_fetch, _host_is_blocked_for_fetch._blocked_ip, _fetch_private_urls_allowed, _validate_fetch_url, _skill_runtime_env_opt_in, _skill_runtime_default, _resolve_skill_runtime_flags, _resolve_skill_runtime_flags._flag

- [`api/tw_output_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tw_output_guard.py)｜1001 行｜`00f689577328`｜_char_entropy, _bigram_repetition_ratio, _has_semantic_breaks, _is_gibberish, _unwrap_json_fence, _machine_output_to_natural_text, _strip_customer_service_template, _strip_generic_refusal_template, _strip_internal_leaks, detect_output_guard_issues

- [`api/verification/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/verification/__init__.py)｜19 行｜`a5d861a3cbfb`｜—

- [`api/verification/agent_workflow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/verification/agent_workflow.py)｜412 行｜`384cd72f8d61`｜TriAgentVerificationReport, should_trigger_tri_agent, _summarize_evidence, _call_llm, _parse_json_response, run_tri_agent_verification, _notify, _make_report, format_verification_footer

- [`api/verification/answer_verifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/verification/answer_verifier.py)｜115 行｜`c27d74d15c60`｜AnswerVerificationResult, _has_user_chatlog_support, _contains_false_memory_claim, _contains_overclaim, verify_answer

- [`api/webhooks/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/webhooks/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`api/webhooks/line.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/webhooks/line.py)｜1661 行｜`23e89c9aa3a2`｜get_line_admin_user_ids, init_line_module, _write_json_atomic, _load_json, _record_last_line_sender, _record_last_line_callback, _load_last_line_sender_user_id, _safe_remove_tmp, _cleanup_user_context, cleanup_old_exports

- [`api/webhooks/telegram.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/webhooks/telegram.py)｜1546 行｜`7fa1f7be1f57`｜_get_orchestrator, _load_telegram_bot_token, _load_admin_telegram_ids, _load_notify_telegram_ids, _load_telegram_webhook_secret, _env_truthy, _env_falsey, _telegram_production_mode, _telegram_webhook_secret_required, _telegram_verify_webhook_secret

- [`api/wsgi_server.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/wsgi_server.py)｜23 行｜`a2679a86a654`｜serve

### bin/（1 檔）

- [`bin/agent_mcp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/bin/agent_mcp.py)｜87 行｜`88d57283888b`｜_config_from_args, main

### casper_ecosystem/（13 檔）

- [`casper_ecosystem/law_firm_orchestrators/file_review_automation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/file_review_automation.py)｜15954 行｜`9889569bd6d6`｜_infer_file_review_sys_type, _ordered_file_review_sys_candidates, _normalize_file_review_text, _file_review_case_signature_present, _file_review_submit_success_from_text, _file_review_alert_looks_rejected, _file_review_submit_evidence_is_success, _is_production_host, CaptchaSolver, CaptchaSolver.__init__

- [`casper_ecosystem/law_firm_orchestrators/judicial_automation_v2.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/judicial_automation_v2.py)｜6714 行｜`dc8e26acdaaa`｜_safe_print, _safe_log_callback, _is_production_host, _clean_transcript_parse_value, _valid_transcript_record_date, _valid_transcript_record_type, _record_parse_ready_for_filename, _transcript_filename_metadata_category, _chinese_calendar_number, _extract_transcript_metadata_from_text_pages

- [`casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py)｜12592 行｜`432511f600a6`｜_safe_print, _safe_log_callback, _safe_logger, _laf_default_case_lawyer, _eventlog, normalize_laf_portal_report_status, classify_laf_portal_report_row, laf_portal_attachment_receipt_key, _record_laf_portal_attachment_receipt, _record_laf_external_action_receipt

- [`casper_ecosystem/law_firm_orchestrators/laf_flow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_flow.py)｜19 行｜`149e422aee97`｜—

- [`casper_ecosystem/law_firm_orchestrators/laf_folder_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_folder_builder.py)｜466 行｜`77fc31df90d6`｜LAFFolderBuilder, LAFFolderBuilder.__init__, LAFFolderBuilder._load_config, LAFFolderBuilder._init_path_mappings, LAFFolderBuilder._detect_local_mount, LAFFolderBuilder._ensure_authoritative_root, LAFFolderBuilder.create_case_folder, LAFFolderBuilder.get_local_path_from_canonical, LAFFolderBuilder.folder_exists, LAFFolderBuilder._safe_makedirs

- [`casper_ecosystem/law_firm_orchestrators/laf_handler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_handler.py)｜29 行｜`6036c23ac0f5`｜—

- [`casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py)｜5304 行｜`15c09e0417f5`｜_sync_case_path_references, _sync_case_path_references._exec, PortalNewFilesScanResult, PortalNewFilesScanResult.__init__, _db_probe, _get_db, _load_config, _normalize_status_text, _normalize_person_name, _is_unusable_client_label

- [`casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py)｜11033 行｜`aa587c1a490d`｜_portal_retry_initial_delay_seconds, _sync_case_path_references, _sync_case_path_references._exec, _create_laf_upload_staging_dir, _eventlog, _safe_listdir, _safe_listdir._runner, _safe_getmtime, _safe_getmtime._runner, _PortalRetryCycleTimeout

- [`casper_ecosystem/law_firm_orchestrators/laf_orchestrator_docmixins.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_orchestrator_docmixins.py)｜949 行｜`d1110f4403e1`｜is_closing_fee_filename, LAFOrchestratorDocumentMixin, LAFOrchestratorDocumentMixin._text_contains_any, LAFOrchestratorDocumentMixin._find_first_existing, LAFOrchestratorDocumentMixin._dedupe_sorted, LAFOrchestratorDocumentMixin._normalize_date_text, LAFOrchestratorDocumentMixin._extract_date_from_filename, LAFOrchestratorDocumentMixin._extract_date_from_office_text, LAFOrchestratorDocumentMixin._get_doc_hint_ocr_engine, LAFOrchestratorDocumentMixin._ocr_text_from_image

- [`casper_ecosystem/law_firm_orchestrators/laf_progress_helper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_progress_helper.py)｜209 行｜`24cfa8cc4438`｜_is_doc_excluded, _stem_ends_with_zhuan, _score_pdf, pick_latest_pdf, pick_latest_pdf._sort_key, extract_date_from_pdf_name, build_progress_remark, classify_progress_email

- [`casper_ecosystem/law_firm_orchestrators/laf_vision.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/laf_vision.py)｜440 行｜`4960cd7e59ce`｜LAFVision, LAFVision.__init__, LAFVision._env_bool, LAFVision._consensus_enabled, LAFVision._consensus_shadow, LAFVision._extract_via_legacy, LAFVision._run_consensus_ocr, LAFVision._write_consensus_metrics, LAFVision.extract_start_date, LAFVision.extract_text

- [`casper_ecosystem/law_firm_orchestrators/line_notifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/line_notifier.py)｜772 行｜`449a11e95b6f`｜_guard_text, _load_env, _load_config, _split_csv, _load_admin_line_ids_from_allowlist, _is_line_429_active_today, LAFNotifier, LAFNotifier.__init__, LAFNotifier.notify_admin, LAFNotifier.notify_admin_with_files

- [`casper_ecosystem/law_firm_orchestrators/osc/folder_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/casper_ecosystem/law_firm_orchestrators/osc/folder_utils.py)｜3 行｜`73539ca40c38`｜—

### daemon.py/（1 檔）

- [`daemon.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/daemon.py)｜2191 行｜`ea3dc14f7ca8`｜file_lock, file_unlock, get_venv_python, get_magi_root, _feature_enabled, _is_training_locked, _is_night_window, _expected_omlx_profile_now, _is_omlx_night_window, _read_omlx_main_model_id

### gui/（1 檔）

- [`gui/magi_menubar.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/gui/magi_menubar.py)｜6621 行｜`4fdf5cf9b9dc`｜_acquire_menubar_singleton, _acquire_menubar_singleton._release_lock, _FallbackMenu, _FallbackMenu.insert_after, _FallbackMenuItem, _FallbackMenuItem.__init__, _FallbackMenuItem.set_callback, _FallbackApp, _FallbackApp.__init__, _FallbackApp.menu

### integrations/（6 檔）

- [`integrations/debt_robot/01_A.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/01_A.py)｜184 行｜`a2a3af7251cf`｜DocumentEditor, DocumentEditor.__init__, DocumentEditor.init_ui, DocumentEditor.open_folder, DocumentEditor.load_documents, DocumentEditor.save_document

- [`integrations/debt_robot/02_B.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/02_B.py)｜288 行｜`4fffa277fb35`｜TableEditor, TableEditor.__init__, TableEditor.init_tabs, TableEditor.add_tab, TableEditor.add_income_tab, TableEditor.add_generic_row, TableEditor.add_expense_tab, TableEditor.add_expense_row, TableEditor.add_expense_row.update_total, TableEditor.update_total_sum

- [`integrations/debt_robot/03_C.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/03_C.py)｜218 行｜`765ca8ac3b54`｜CreditorEditor, CreditorEditor.__init__, CreditorEditor.read_csv_files, CreditorEditor.init_tab, CreditorEditor.add_creditor_row, CreditorEditor.add_creditor_row.update_address, CreditorEditor.update_total_sum, CreditorEditor.save_document, CreditorEditor.update_csv

- [`integrations/debt_robot/04_D.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/04_D.py)｜142 行｜`5b6c19b5a300`｜add_blank_page_if_needed, merge_single_pdf, merge_pdfs, select_files, convert_docx_to_pdf, move_up, move_down, remove_file, merge_and_save

- [`integrations/debt_robot/05_E.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/05_E.py)｜579 行｜`a4b7b8dc504f`｜DocumentGenerator, DocumentGenerator.__init__, DocumentGenerator.handle_c3_selection, DocumentGenerator.handle_d3_selection, DocumentGenerator.init_ui, DocumentGenerator.init_ui.add_input, DocumentGenerator.init_ui.add_textarea, DocumentGenerator.apply_inputs_to_doc, DocumentGenerator.apply_inputs_to_doc.get_text, DocumentGenerator.apply_inputs_to_doc.num_to_chinese

- [`integrations/debt_robot/06_F.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/06_F.py)｜549 行｜`68b8cb915c20`｜ExtractWorker, ExtractWorker.__init__, ExtractWorker.run, SupplementGenerator, SupplementGenerator.__init__, SupplementGenerator._init_ui, SupplementGenerator._build_left_panel, SupplementGenerator._build_right_panel, SupplementGenerator._refresh_case_list, SupplementGenerator._on_case_selected

### magi_v3/（64 檔）

- [`magi_v3/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/__init__.py)｜47 行｜`e5e5180d3013`｜__getattr__, __dir__

- [`magi_v3/__main__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/__main__.py)｜38 行｜`c83c03470d5c`｜main

- [`magi_v3/a2a_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/a2a_adapter.py)｜114 行｜`8d4f9e345a2d`｜A2AAdapterError, _federation_host_forbidden, A2AAdapterPolicy, A2AAdapterPolicy.__post_init__, A2AAdapterPolicy.load, create_proposal

- [`magi_v3/business_events.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/business_events.py)｜278 行｜`6136ed92fe60`｜default_ledger_path, _safe_token, _source_digest, BusinessEventLedger, BusinessEventLedger.__init__, BusinessEventLedger._connect, BusinessEventLedger._init_schema, BusinessEventLedger.emit, BusinessEventLedger.claim, BusinessEventLedger.complete

- [`magi_v3/business_outcome_eval.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/business_outcome_eval.py)｜120 行｜`4a66425c1267`｜_canonical, _hash, _contains_pii, anonymise_finding, merge_manual_finding, write_manual_finding, evaluate_outcome_slo

- [`magi_v3/business_recovery.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/business_recovery.py)｜410 行｜`43d8572b3aa7`｜RecoveryDecision, RecoveryDecision.delay_for_attempt, _last_json_object, _bool, _bounded_int, load_recovery_catalog, contract_for_job, audit_recovery_catalog, _retry_delays, _reason_code

- [`magi_v3/case_filesystem.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/case_filesystem.py)｜249 行｜`79f53ccedcf3`｜_truthy, _inside, _tree_signature, NativeCaseFilesystemEffects, NativeCaseFilesystemEffects.__init__, NativeCaseFilesystemEffects._assert_authoritative_bindings, NativeCaseFilesystemEffects._root, NativeCaseFilesystemEffects.from_environment, NativeCaseFilesystemEffects.__call__, NativeCaseFilesystemEffects._create_folder

- [`magi_v3/case_lifecycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/case_lifecycle.py)｜91 行｜`0723693614c8`｜CaseLifecyclePhase, phase_for_status, case_lifecycle_phase, requires_closed_storage, canonical_case_status

- [`magi_v3/compat/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/compat/__init__.py)｜36 行｜`9b22ad88ae0b`｜create_admin_server

- [`magi_v3/compat/admin.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/compat/admin.py)｜119 行｜`fabf65b94d27`｜AdminCompatibilityError, HealthApplication, HealthApplication.response, _sha256, _load_admin_module, _overlay_handler, _overlay_handler.Handler, _overlay_handler.Handler.do_GET, create_admin_server

- [`magi_v3/compat/gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/compat/gateway.py)｜495 行｜`570c1b26d5d2`｜StartResponse, StartResponse.__call__, CompatibilityLoadError, CompatibilitySurfaceError, RouteSpec, RouteSpec.from_mapping, RouteInventory, RouteInventory.load, RouteInventory.counts, RouteInventory.for_service

- [`magi_v3/config.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/config.py)｜212 行｜`f027db012b4b`｜_default_state_dir, _default_host_active_lock_path, _as_bool, _as_int, _as_float, ResourcePolicy, ResourcePolicy.validate, CoreSettings, CoreSettings.resolved_ledger_path, CoreSettings.validate

- [`magi_v3/control.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/control.py)｜361 行｜`8037f1b324fd`｜HTTPServerLike, HTTPServerLike.handle_request, HTTPServerLike.server_close, _process_exists, _process_group_exists, build_supervisor_dependency_probe, build_supervisor_dependency_probe.probe, ControlHealthApplication, ControlHealthApplication.__init__, ControlHealthApplication.response

- [`magi_v3/controlled_evolution.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/controlled_evolution.py)｜906 行｜`c9a4a01124e1`｜_utc_now, _iso_now, _digest, _canonical_json, ComponentRule, _safe_signal, classify_component, _risk_for, build_structure_inventory, build_proposal

- [`magi_v3/cron_macros.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/cron_macros.py)｜43 行｜`16c44a4f834e`｜CronMacroEntrypoint, CronMacroEntrypoint.argv, resolve_exact_cron_macro

- [`magi_v3/cron_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/cron_policy.py)｜287 行｜`bd53674b88fa`｜CronDispatchPolicyError, _read_stable_regular_file, _bound_cron_snapshot, CronDispatchPolicy, CronDispatchPolicy.max_workers, CronDispatchPolicy.can_start_lane, CronDispatchPolicy.lane_for, CronDispatchPolicy.delay_for, CronDispatchPolicy.queue_all_non_durable, CronDispatchPolicy.coalesces_pending

- [`magi_v3/cron_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/cron_service.py)｜1033 行｜`b3b5c9177f18`｜_cron_occurrence_id, _cron_timeout_seconds, _load_bound_cron_environment, CronServiceError, Scheduler, Scheduler.reconcile_incomplete_jobs, Scheduler.reconcile_terminal_schedule_deferrals, Scheduler.rearm_recovered_resource_deferrals, Scheduler.peek_due_jobs, Scheduler.get_missed_jobs_v3

- [`magi_v3/dispatcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/dispatcher.py)｜777 行｜`30c1cb985d36`｜load_capability_worker_classes, _validated_capability_mapping, VerifiedCompletion, VerifiedCompletion.validate, load_capability_worker_adapter, DispatchHandle, PreemptionOutcome, DispatchOutcome, DurableDispatcher, DurableDispatcher.__init__

- [`magi_v3/drive_file_checkpoint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/drive_file_checkpoint.py)｜600 行｜`57cffe764b76`｜DriveFileCheckpointError, _now, _digest, _valid_digest, _valid_partial_row, case_token, source_fingerprint, item_token, proof_hash, snapshot_hash

- [`magi_v3/errors.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/errors.py)｜37 行｜`396b401d6a23`｜CoreError, ConfigurationError, LedgerError, JobNotFound, InvalidTransition, LeaseConflict, AdmissionDenied, SupervisorError, WorkerAlreadyRunning

- [`magi_v3/evidence_ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/evidence_ledger.py)｜554 行｜`633a6fde0f38`｜EvidenceLedgerError, _utc_now, _parse_time, _canonical, _digest, _token, _component, _safe_receipt, EvidenceEnvelope, EvidenceEnvelope.create

- [`magi_v3/external_canary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/external_canary.py)｜169 行｜`17d4ffdc2456`｜ExternalCanaryError, canonical_bytes, sign_receipt, verify_receipt, verify_receipt._time, load_from_environment

- [`magi_v3/external_inputs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/external_inputs.py)｜623 行｜`1323ded44fb5`｜ExternalInputError, named_mutable_state_paths, live_shared_state_environment, BoundCronJobs, BoundLAFConfig, BoundExternalFile, _sealed_context, bound_shared_directory, laf_download_directory, bound_shared_file

- [`magi_v3/faiss_maintenance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/faiss_maintenance.py)｜366 行｜`5bb4c5392c66`｜FaissMaintenanceError, _sha256_file, _valid_sha256, _python_command, _atomic_json, _request_lock, FaissRebuildCoordinator, FaissRebuildCoordinator.__init__, FaissRebuildCoordinator.is_source_job, FaissRebuildCoordinator.low_memory_environment

- [`magi_v3/fcntl_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/fcntl_compat.py)｜53 行｜`80ae21ae4ed6`｜_descriptor, flock, lockf

- [`magi_v3/file_review_receipts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/file_review_receipts.py)｜151 行｜`6fcde9d8d4ae`｜_first, _first_upper, canonical_portal_download_signature, normalize_signature_hashes, signature_set_hash, portal_snapshot_fingerprint, portal_observed_epoch, portal_download_snapshot

- [`magi_v3/forensic_transcript.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/forensic_transcript.py)｜659 行｜`99f315b52300`｜_sha256_file, _lease_token, _within, _mutable_root, _workspace, _input_evidence, _task_payload, _seatbelt_profile, _transcription_policy, _required_env

- [`magi_v3/gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/gateway.py)｜725 行｜`826fe2d9fe4d`｜GatewayConfigurationError, GatewayRuntimeError, RoleGuard, RoleGuard.acquired, RoleGuard.acquire, RoleGuard.release, ServerHandle, ServerHandle.run, ServerHandle.close, ReleaseOwnership

- [`magi_v3/health.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/health.py)｜81 行｜`3cdf4834da65`｜HealthReport, HealthReport.to_dict, HealthService, HealthService.__init__, HealthService.liveness, HealthService.readiness, _now

- [`magi_v3/health_presentation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/health_presentation.py)｜40 行｜`37a69a45ae3e`｜_safe_text, present_health

- [`magi_v3/instance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/instance.py)｜63 行｜`7ee0407a6ab6`｜SingleActiveError, SingleActiveGuard, SingleActiveGuard.__init__, SingleActiveGuard.acquired, SingleActiveGuard.acquire, SingleActiveGuard.release, SingleActiveGuard.__enter__, SingleActiveGuard.__exit__

- [`magi_v3/ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/ledger.py)｜2262 行｜`68ed1f4fe146`｜_utcnow, _timestamp, _json, _decode, _parse_timestamp, _canonical_resource_claim, _canonical_artifacts, _canonical_receipts, _canonical_metrics, _canonical_error

- [`magi_v3/legacy_background_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/legacy_background_service.py)｜532 行｜`e3f7ec2f7c1c`｜LegacyBackgroundError, ServiceConfig, ServiceConfig.validated, ComponentSpec, ComponentState, ComponentState.to_dict, _module_origin_within, bind_legacy_root, _periodic, _queue_recovery

- [`magi_v3/live_validation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/live_validation.py)｜195 行｜`11bea7c0572b`｜HealthApplication, HealthApplication.response, _environment, _fixture_path, _response, ValidationWSGIApp, ValidationWSGIApp.__init__, ValidationWSGIApp.__call__, create_main_app, create_tools_app

- [`magi_v3/live_validation_probe_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/live_validation_probe_service.py)｜37 行｜`efee7a32003c`｜main, main.stop

- [`magi_v3/macos_resources.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/macos_resources.py)｜450 行｜`ffa60e8b24d2`｜MemoryPressureMetrics, VMStatMetrics, SwapUsageMetrics, ProcessMetrics, ProcessFootprintMetrics, FootprintMetrics, MacOSResourceSample, MacOSResourceSample.governor_ready, _default_runner, _pressure_level

- [`magi_v3/mcp_catalog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/mcp_catalog.py)｜113 行｜`1e9de5ae99c6`｜McpCatalogError, ApprovedMcpServer, ApprovedMcpServer.__post_init__, ApprovedMcpServer.verify_executable, McpClientCatalog, McpClientCatalog.__init__, McpClientCatalog.load, McpClientCatalog.resolve

- [`magi_v3/mcp_conformance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/mcp_conformance.py)｜197 行｜`179d13cd63b5`｜McpRequestContext, McpRequestContext.modern, McpProtocolError, McpProtocolError.__init__, McpProtocolError.as_error, _mapping, _implementation, request_context, legacy_initialize_version, response_meta

- [`magi_v3/memory_backends.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/memory_backends.py)｜187 行｜`0459e1b61027`｜_receipt, _eligible, SqlMemoryBackend, SqlMemoryBackend.__init__, SqlMemoryBackend.__call__, FaissRebuildBackend, FaissRebuildBackend.__init__, FaissRebuildBackend.__call__, KnowledgeGraphBackend, KnowledgeGraphBackend.__init__

- [`magi_v3/memory_lifecycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/memory_lifecycle.py)｜544 行｜`56cbd603efa3`｜MemoryLifecycleError, utc_now, _canonical_text, content_sha256, canonical_memory_id, _safe_case_scope, MemoryRecord, MemoryRecord.__post_init__, MemoryRecord.protected_legal_record, MemoryRecord.to_dict

- [`magi_v3/model_recovery.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/model_recovery.py)｜108 行｜`c4ea676317e7`｜_epoch, assess_omlx_recovery

- [`magi_v3/mutable_state_handoff.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/mutable_state_handoff.py)｜719 行｜`670225a0a2b1`｜MutableStateHandoffError, StateSpec, ExactContext, ExactContext.validate, ExactContext.public, FileSnapshot, FileSnapshot.public_source, TargetSnapshot, TargetSnapshot.public, _validate_allowlist

- [`magi_v3/observability.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/observability.py)｜140 行｜`ce2e14cd6cf8`｜task_trace, outcome_slo, actionable_error, support_bundle, verify_dr_report, _digest, _safe_label, _safe_text, _next_step

- [`magi_v3/ocr_queue.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/ocr_queue.py)｜64 行｜`440dce9a5e73`｜OCRQueuePathError, _sealed_v3_context, resolve_nas_ocr_queue_db_path

- [`magi_v3/osc_cases.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/osc_cases.py)｜1078 行｜`2b0222fd5e02`｜OscCasesError, RequestValidationError, RequestValidationError.__init__, CaseListQuery, CreateResult, CaseTransaction, CaseTransaction.list_cases, CaseTransaction.next_case_number, CaseTransaction.find_existing, CaseTransaction.insert_case

- [`magi_v3/osc_main.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/osc_main.py)｜513 行｜`044be25a7169`｜_env_truthy, _https_enforced, V2SecurityHeaderPolicy, V2SecurityHeaderPolicy.__init__, V2SecurityHeaderPolicy.__call__, _cookie_value, FlaskSessionAuthorizer, FlaskSessionAuthorizer.__init__, FlaskSessionAuthorizer.__call__, MariaDBUserLoader

- [`magi_v3/process_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/process_compat.py)｜32 行｜`16b516a7549d`｜process_group, signal_group, group_exists

- [`magi_v3/process_monitor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/process_monitor.py)｜326 行｜`ad9f37fb3572`｜parse_etime_seconds, process_monitor_markers, parse_ps_rows, _argv_head, _is_shell_command_wrapper, _worker_marker, _core_marker, _is_managed_parent, _is_orphan_worker, ZombiePersistence

- [`magi_v3/quality_ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/quality_ledger.py)｜173 行｜`cdf2fe6ed8e1`｜_now, _json, _hash, _require_timestamp, canonical_quality_signal, attest_release, QualityOutcomeLedger, QualityOutcomeLedger.__init__, QualityOutcomeLedger._connect, QualityOutcomeLedger.upsert

- [`magi_v3/resource.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/resource.py)｜254 行｜`2404c49b6cae`｜PressureLevel, ResourceSnapshot, ResourceSnapshotProvider, ResourceSnapshotProvider.snapshot, AdmissionRequest, AdmissionRequest.validate, AdmissionDecision, ResourceLease, ResourceLease.__init__, ResourceLease.released

- [`magi_v3/runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/runtime.py)｜68 行｜`2c4c8bd222c0`｜CoreRuntime, CoreRuntime.build, CoreRuntime.initialize, CoreRuntime.activate, CoreRuntime.close, CoreRuntime.__enter__, CoreRuntime.__exit__

- [`magi_v3/selfhost.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/selfhost.py)｜1855 行｜`87b38371c478`｜_release_secret_path, _release_excluded_path, SelfHostError, HostLayout, HostLayout.as_dict, Check, Check.as_dict, ServicePlan, ServicePlan.as_dict, _system_name

- [`magi_v3/service_manifest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/service_manifest.py)｜250 行｜`70f4f355a80f`｜assert_deployment_safety, ServiceDefinition, ServiceManifest, ServiceManifest.for_role, ServiceManifest.service, _object, _identifiers, _safe_argv, load_service_manifest, load_bound_service_manifest

- [`magi_v3/service_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/service_runtime.py)｜766 行｜`9564571c63bd`｜ServiceRuntimeError, _canonical_runtime_root, ProcessRecord, OwnershipProbe, OwnershipProbe.assert_exclusive, is_transient_ownership_probe_failure, PeriodicOwnershipGuard, PeriodicOwnershipGuard.__init__, PeriodicOwnershipGuard.check, _default_process_reader

- [`magi_v3/shapely_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/shapely_runtime.py)｜23 行｜`72b765cb0df8`｜verify_shapely_runtime

- [`magi_v3/skill_manifest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/skill_manifest.py)｜218 行｜`a7f963cf5f1c`｜SkillManifestError, canonical_bytes, _unsigned, manifest_digest, _safe_root, validate_manifest, load_manifest, build_candidate_manifest, write_candidate_manifest, verify_catalog_approval

- [`magi_v3/skill_sandbox.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/skill_sandbox.py)｜151 行｜`be270cbfdb2e`｜_quoted, seatbelt_profile, _filtered_env, run_manifested_skill

- [`magi_v3/span_evaluation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/span_evaluation.py)｜81 行｜`0f5de652d3db`｜SpanExpectation, evaluate_spans

- [`magi_v3/state.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/state.py)｜105 行｜`b37e199b1ac4`｜JobStatus, ensure_transition

- [`magi_v3/supervisor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/supervisor.py)｜344 行｜`22013d896760`｜WorkerSpec, WorkerSpec.validate, WorkerResult, _WorkerHandle, WorkerSupervisor, WorkerSupervisor.__init__, WorkerSupervisor.start, WorkerSupervisor.poll, WorkerSupervisor.wait, WorkerSupervisor.terminate

- [`magi_v3/supervisor_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/supervisor_service.py)｜573 行｜`a64830621065`｜ProcessLike, ProcessLike.poll, ProcessLike.wait, ManagedChild, _group_exists, _pid_alive, _process_argv, load_supervisor_environment, ManifestProcessSupervisor, ManifestProcessSupervisor.__init__

- [`magi_v3/supply_chain.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/supply_chain.py)｜503 行｜`b7117c0ad9a2`｜SupplyChainError, _load_json_regular, _sha256, canonical_digest, installed_components, runtime_lock, cyclonedx_sbom, wheelhouse_manifest, verify_wheelhouse, scan_release_secrets

- [`magi_v3/telemetry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/telemetry.py)｜381 行｜`a63ccbd6b3bb`｜TelemetryError, TraceContext, TraceContext.__post_init__, TraceContext.traceparent, TraceContext.parse, new_trace_context, child_trace_context, _safe_name, _safe_attributes, SpanExporter

- [`magi_v3/video_autopilot_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/magi_v3/video_autopilot_adapter.py)｜792 行｜`a232348badf7`｜VideoAutopilotError, StoryboardRequest, EditPlan, AssetInput, _clean_text, _validate_storyboard_request, validate_storyboard_request, validate_asset_storyboard_request, interpret_edit_instructions, public_edit_plan

### osc.py/（1 檔）

- [`osc.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/osc.py)｜610 行｜`bebac65f97b7`｜DatabaseManager, DatabaseManager.__init__, DatabaseManager._cfg, DatabaseManager._get_connection, DatabaseManager._fetch_table_columns, DatabaseManager._execute_ddl, DatabaseManager._ensure_min_schema, DatabaseManager.execute, DatabaseManager.fetch_one, DatabaseManager.fetch_all

### scripts/（358 檔）

- [`scripts/accounting_monthly_bonus.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/accounting_monthly_bonus.py)｜64 行｜`e9287de78dc8`｜main

- [`scripts/add_skill_invocation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/add_skill_invocation.py)｜200 行｜`cae629e19aaf`｜add_invocation

- [`scripts/architecture/capture_v2_runtime_routes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/architecture/capture_v2_runtime_routes.py)｜142 行｜`946efffe73e0`｜_NullRotatingFileHandler, _NullRotatingFileHandler.__init__, _prepare_safe_import, _capture, capture, main

- [`scripts/architecture/generate_runtime_inventory.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/architecture/generate_runtime_inventory.py)｜29 行｜`c2316c375dbc`｜—

- [`scripts/architecture/generate_v2_inventory.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/architecture/generate_v2_inventory.py)｜411 行｜`1f61caa3f382`｜semantic_inventory_projection, _portable_text, _literal_string, collect_routes, collect_skills, _cron_jobs, collect_cron, collect_portable_cron_bytes, project_inventory_to_release, collect_daemon_children

- [`scripts/audit_gcal_duplicates.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/audit_gcal_duplicates.py)｜383 行｜`0bdc8f977a3b`｜_load_osc_action_module, _score_confidence, _event_start_repr, _event_brief, _group_confidence, _eligible_for_delete, _write_json, _write_jsonl, _write_summary_md, main

- [`scripts/benchmark_gemma4_mtp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/benchmark_gemma4_mtp.py)｜273 行｜`26b3eb07a39a`｜resolve_draft_model, get_service_url, BenchmarkTask, _as_bool, load_tasks, extract_content, valid_json_text, build_payload, chat_once, probe_runtime

- [`scripts/casper_night_patrol.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/casper_night_patrol.py)｜392 行｜`6600ca328f2d`｜scan_logs, run_memory_consolidation, run_health_check, ask_local_llm, generate_proposals, save_proposals, build_report, save_report, send_report, run_laf_nightly_audit

- [`scripts/ci/check_hardcodes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ci/check_hardcodes.py)｜157 行｜`183091c2b693`｜_is_comment, find_repo_root, scan, main

- [`scripts/ci/check_monolith_size.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ci/check_monolith_size.py)｜60 行｜`f6996089e05d`｜find_repo_root, main

- [`scripts/ci/check_shell_true.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ci/check_shell_true.py)｜94 行｜`d8eca52d6b7e`｜_load_grandfather, _iter_py_files, scan, main

- [`scripts/classify_supreme_interpreter_mentions.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/classify_supreme_interpreter_mentions.py)｜597 行｜`508f13611fc6`｜ClassifiedCase, compact, compile_any, is_pure_legal_template_context, first_match, extract_metadata, extract_main_text, classify_outcome, extract_prior_case_no, find_contexts

- [`scripts/code_skill_cycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/code_skill_cycle.py)｜56 行｜`9854f1e00c07`｜run_cycle

- [`scripts/configure_mobile_app.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/configure_mobile_app.py)｜57 行｜`7f979cfb0386`｜_configured_mobile_url, _write_config, main

- [`scripts/customer_install_wizard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/customer_install_wizard.py)｜379 行｜`f9db02121334`｜WizardStep, _status_from_bool, _command_text, _run_command, _summarize, _write_report, _env_step, _preflight_step, _install_steps, _public_audit_step

- [`scripts/db_sync_to_remote.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/db_sync_to_remote.py)｜165 行｜`52fa814c1456`｜_signal_handler, remote_reachable, backup_local, cleanup_old_backups, dump_remote, push_to_local, sync_once

- [`scripts/dedup_magi_brain.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/dedup_magi_brain.py)｜224 行｜`f3a405a8f945`｜get_conn, count_duplicates, count_smoke_test, get_duplicate_ids_batch, get_smoke_test_ids_batch, delete_batch, dedup_report, main

- [`scripts/docs/build_magi_encyclopedia.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/docs/build_magi_encyclopedia.py)｜1404 行｜`383fb533d8d6`｜Symbol, FileRecord, sha256_file, redact_workstation_paths, first_sentence, signature_for, python_details, python_details.Visitor, python_details.Visitor.__init__, python_details.Visitor._symbol

- [`scripts/docs/generate_implementation_status.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/docs/generate_implementation_status.py)｜309 行｜`6962544c1eaa`｜ImplementationStatusError, _load_json, _source_commit, _source_dirty, _active_release, _quality_inventory, _release_policy, build_status, render_markdown, main

- [`scripts/drive_case_sync_inventory.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/drive_case_sync_inventory.py)｜15 行｜`d4a46cda68fa`｜—

- [`scripts/drive_case_sync_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/drive_case_sync_worker.py)｜2116 行｜`2799be050680`｜state_path, file_checkpoint_path, worker_status_path, worker_lock_path, iso_now, _adaptive_all_case_limit, _fair_all_case_chunk_limit, _inner_inventory_budget, DriveCaseSyncTimeout, inventory_time_limit

- [`scripts/fetch_supreme_interpreter_texts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/fetch_supreme_interpreter_texts.py)｜268 行｜`c3c795900867`｜safe_title, clean_judgment_text, load_source_items, _read_mapping_json, _read_mapping_csv, existing_text_by_authoritative_index, load_judicial_web_search_module, materialize_complete_corpus, sync_summary_outputs, main

- [`scripts/first_run_setup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/first_run_setup.py)｜236 行｜`652dd98b0734`｜SetupItem, _parse_env, _is_placeholder, _write_env_from_example, _tracked_files, _public_isolation_findings, build_first_run_checklist, main

- [`scripts/fix_silent_except.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/fix_silent_except.py)｜429 行｜`da3d5e58b066`｜Replacement, FileResult, SilentExceptFixer, SilentExceptFixer.__init__, SilentExceptFixer._get_files_to_scan, SilentExceptFixer._is_in_skip_dir, SilentExceptFixer._line_number_at_pos, SilentExceptFixer._get_indentation, SilentExceptFixer._find_replacements, SilentExceptFixer._apply_replacements

- [`scripts/gemma4_comparison_test.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/gemma4_comparison_test.py)｜200 行｜`0af71a2daee1`｜chat, check_models, run_test

- [`scripts/generate_detailed_user_manual.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/generate_detailed_user_manual.py)｜711 行｜`f39e42af002b`｜pil_font, draw_round_rect, draw_wrapped, draw_centered_text, save_img, make_cover_image, make_module_map, make_quality_map, make_daily_workflow, make_todo_split

- [`scripts/generate_hearing_leave_public_template.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/generate_hearing_leave_public_template.py)｜63 行｜`0a7880e492aa`｜template_payload, main

- [`scripts/generate_public_manual_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/generate_public_manual_docx.py)｜194 行｜`584bca0c2c1c`｜set_run_font, add_page_number, configure_document, add_styled_text, flush_buffer, build_docx, patch_settings_xml

- [`scripts/generate_user_operation_manual_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/generate_user_operation_manual_docx.py)｜433 行｜`538a8b6a87a0`｜set_run_font, shade_cell, set_cell_border, cell_text, add_para, add_heading, add_table, add_bullets, page_break, load_json

- [`scripts/generate_verified_user_manual_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/generate_verified_user_manual_docx.py)｜637 行｜`38cea54049ae`｜_ensure_docx_dependency, load_json, first_existing, result_count, set_run_font, shade_cell, set_cell_border, set_cell_text, add_para, add_heading

- [`scripts/generate_visual_user_manual_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/generate_visual_user_manual_docx.py)｜1038 行｜`28e6eab308c6`｜font, hex_to_rgb, draw_round_rect, draw_text_box, draw_centered_text, save_image, make_cover, make_module_map, make_workflow, make_dashboard_mock

- [`scripts/import_accounting_sheet.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/import_accounting_sheet.py)｜25 行｜`d02ab2d6139b`｜—

- [`scripts/ingest_raw_judgments.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ingest_raw_judgments.py)｜215 行｜`4b3621e552ae`｜load_state, save_state, extract_fields, court_name_from_jid, parse_jdate, main

- [`scripts/install_magi.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/install_magi.py)｜141 行｜`dfa3ac716b15`｜InstallStep, venv_python, build_install_plan, run_step, live_checks, main

- [`scripts/install_omlx_embed.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/install_omlx_embed.py)｜91 行｜`00949dc591e6`｜run, main

- [`scripts/install_omlx_restore.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/install_omlx_restore.py)｜83 行｜`785244894eaf`｜run, main

- [`scripts/install_omlx_text.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/install_omlx_text.py)｜103 行｜`60aa4c99e99a`｜_enabled, run, build_launch_agent_plist, main

- [`scripts/install_omlx_watchdog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/install_omlx_watchdog.py)｜80 行｜`63656bfcdec3`｜run, main

- [`scripts/install_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/install_service.py)｜69 行｜`f626071318bc`｜install, uninstall

- [`scripts/judicial_jlist_crawl.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/judicial_jlist_crawl.py)｜253 行｜`f90723468013`｜_is_open_hours, _authenticate, _save_judgment, _mark_dedup, _is_already_fetched, run

- [`scripts/knowledge_lint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/knowledge_lint.py)｜2049 行｜`f5553dc5c185`｜_resolve_agent_dir, _reviewed_no_usable_judgment_count, _pending_nvidia_judgment_review_count, _get_vault_path, _load_env, _db_connect, _extract_insight_body, _is_low_quality_insight, _is_low_quality_judgment_summary, _vector_rows_for_doc_key

- [`scripts/laf_nightly_audit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/laf_nightly_audit.py)｜35 行｜`be8cf6fb2fb0`｜—

- [`scripts/live_magi_mtp_eval.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/live_magi_mtp_eval.py)｜476 行｜`ca3b87e9c619`｜CheckResult, _extract_json, post_chat, check_sidecar, check_json_tool_routes, check_react_tools, _instrumented_tools, _instrumented_tools._make_tool, _instrumented_tools._make_tool._fn, check_all_react_tools

- [`scripts/live_test_taiwan_legal_mcp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/live_test_taiwan_legal_mcp.py)｜61 行｜`c54f5da32f35`｜assert_ok, main

- [`scripts/live_test_tw_legal_rag.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/live_test_tw_legal_rag.py)｜139 行｜`96ad3408038e`｜main

- [`scripts/magi_doctor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/magi_doctor.py)｜1208 行｜`60e01eccecda`｜Check, _project_python, _package_available, _disk_free_gb, _ram_gb, _http_json, _mtp_sidecar_check, _mtp_sidecar_required, _runtime_dir, _live_runtime_root

- [`scripts/magi_selfhost.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/magi_selfhost.py)｜817 行｜`dda7b02747bf`｜_target_system, _json, _command_display, _write_env, _generate_local_secrets, _resolve, command_plan, command_init, _run, _wait_for_live

- [`scripts/memory_consolidation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/memory_consolidation.py)｜46 行｜`d6ebbd23bfe0`｜run_consolidation

- [`scripts/migrate_case_documents_idempotency.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/migrate_case_documents_idempotency.py)｜62 行｜`acca73e8fec0`｜migrate

- [`scripts/migrate_dedup_to_db.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/migrate_dedup_to_db.py)｜234 行｜`b98c90fd6ef5`｜get_conn, create_table, migrate_json_file, migrate_case_filename_map, main

- [`scripts/mirror_exam_tutor_source_catalog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/mirror_exam_tutor_source_catalog.py)｜234 行｜`c621b78602fe`｜now_iso, safe_stem, load_json, active_catalog_urls, build_jobs, fetch_pdf, write_manifest, main

- [`scripts/mobile_app_status.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/mobile_app_status.py)｜104 行｜`6e894da34217`｜_run, _load_mobile_config, _probe_url, main

- [`scripts/nightly_council.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/nightly_council.py)｜233 行｜`76dfcab142d3`｜_resolve_sync_path, sync_from_synology, get_node_status_summary, conduct_nightly_council, send_report

- [`scripts/nightly_distill_gemma.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/nightly_distill_gemma.py)｜809 行｜`21a03bed0625`｜_child_python, _validation_gate_passed, _gemma_distill_collector_paths, _last_accepted_pair_count, _write_rejected_deploy_record, _candidate_rejected_schedule_result, _in_e4b_window, _acquire_lock, _release_lock, _check_timeout

- [`scripts/nightly_health_report.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/nightly_health_report.py)｜504 行｜`28e67a226452`｜_find_latest_nightly_run, _recent_date_prefixes, _read_json_file, _load_recent_resource_guard_event, _format_guard_snapshot, _diagnose_missing_nightly_run, _parse_step_results, _dotenv_value, _env_truthy, _normalize_step_result

- [`scripts/obsidian_bulk_ingest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/obsidian_bulk_ingest.py)｜296 行｜`dfa92cfe41b2`｜load_progress, save_progress, discover_cases, run_bulk_ingest, main

- [`scripts/ops/active_release_input_method_watchdog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/active_release_input_method_watchdog.py)｜118 行｜`e80a6962ab01`｜ActiveWatchdogError, _sha256, _regular_file, resolve_active_watchdog, main

- [`scripts/ops/active_release_service_launcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/active_release_service_launcher.py)｜291 行｜`a2efa5d65539`｜ServiceSpec, ActiveServiceError, ActiveServiceTarget, ActiveServiceTarget.identity, _sha256, _regular_file, _json_file, resolve_active_service, child_environment, child_argv

- [`scripts/ops/agent_readiness_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/agent_readiness_gate.py)｜311 行｜`efeb3c0c6db9`｜_issue, _has_value, _text_for_public_check, _contains_private_content, _normalize_side_effects, _risk_for, _tool_issues, _validate_capability, build_report, load_catalog

- [`scripts/ops/apply_tenant_scope.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/apply_tenant_scope.py)｜52 行｜`aded7ae235c1`｜main

- [`scripts/ops/audit_calendar_completeness.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/audit_calendar_completeness.py)｜346 行｜`9a9a1b7b4422`｜_text_date, _text_time, _event_type, _event_key, _is_osc_source, _has_google_event_id, _final_document_distribution, _write_json, build_report, main

- [`scripts/ops/audit_judgment_extractive_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/audit_judgment_extractive_quality.py)｜121 行｜`d4ce4c8f916d`｜main

- [`scripts/ops/audit_judicial_api_summary_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/audit_judicial_api_summary_quality.py)｜329 行｜`bd9a98ab70f9`｜_load_collector, _sha256, _atomic_json, _atomic_jsonl, _start_transaction, _raw_json_basenames, _source_text_for_row, main

- [`scripts/ops/audit_operational_hardening.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/audit_operational_hardening.py)｜1556 行｜`e41ecb5006e8`｜_FixtureExternalProvider, _FixtureExternalProvider.__init__, _FixtureExternalProvider.cron_jobs, _FixtureExternalProvider.models, _FixtureExternalProvider.omlx_state, _FixtureExternalProvider.run, _FixtureExternalProvider.evidence, _cron_jobs, _external_run, _runtime_override

- [`scripts/ops/audit_osc_buttons.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/audit_osc_buttons.py)｜260 行｜`a98a43c3caa0`｜Finding, _collect_html_files, _collect_js_files, _extract_html_buttons, _extract_js_api_calls, _extract_backend_routes, _url_match, _url_match.norm, run_audit, main

- [`scripts/ops/audit_single_source.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/audit_single_source.py)｜63 行｜`3b3e9657ae37`｜_iter_python_files, audit, main

- [`scripts/ops/autonomy_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/autonomy_worker.py)｜272 行｜`ef70c305e118`｜_bounded_float, resource_snapshot, _load_indexer, _marker_path, _full_scan_due, _write_marker, run_once, main

- [`scripts/ops/backfill_pdf_todo_share_links.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/backfill_pdf_todo_share_links.py)｜249 行｜`329f838a46ed`｜_share_expires_soon, _needs_share_repair, _source_pdf_name, _first_existing, _find_under, _resolve_todo_pdf, _candidate_rows, backfill, main

- [`scripts/ops/background_task_locks.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/background_task_locks.py)｜351 行｜`587948010462`｜_iso_now, lock_dir, lock_path, file_review_portal_lock_path, metadata_path, read_json, write_json_atomic, pid_is_alive, cleanup_stale_lock_metadata, BackgroundLock

- [`scripts/ops/backup_market_watchlist.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/backup_market_watchlist.py)｜137 行｜`c6c6fef01c12`｜_load_watchlist_count, _latest_backup, _purge_old_backups, _alert_tg, main

- [`scripts/ops/benchmark_chat_vs_recall.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_chat_vs_recall.py)｜45 行｜`ba1d66f873d3`｜main

- [`scripts/ops/benchmark_graph_rag.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_graph_rag.py)｜77 行｜`5959a214775d`｜_build_fixture_graph, main

- [`scripts/ops/benchmark_hermes_helpers.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_hermes_helpers.py)｜75 行｜`cfa564a55815`｜_fake_messages, main

- [`scripts/ops/benchmark_models.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_models.py)｜280 行｜`6a1e80090a8f`｜run_legal_retrieval_benchmark, run_ocr_benchmark, run_summary_translation_benchmark, main

- [`scripts/ops/benchmark_osc_todos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_osc_todos.py)｜123 行｜`2c96c27354a0`｜main, main._default_serial

- [`scripts/ops/benchmark_pdf_bookmarker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_pdf_bookmarker.py)｜382 行｜`b1c9dda5b34c`｜_load_module, _load_bookmarker_modules, find_pdfs, _select_case_root, _compute_recall_metrics, _is_legacy_image_label, _looks_like_single_doc_form, _collect_legacy_cleanup_candidate, _build_legacy_cleanup_plan, main

- [`scripts/ops/benchmark_pdf_namer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_pdf_namer.py)｜498 行｜`941e20a2876f`｜_warmup_vision_model, _load_certification_proposals, _threshold_from_env, find_pdfs, _select_case_root, _collect_threshold_failures, _failure_results, main

- [`scripts/ops/benchmark_pdf_namer_archived_golden.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_pdf_namer_archived_golden.py)｜94 行｜`7d3c9d78563d`｜_load_namer, _collect_cases, main

- [`scripts/ops/benchmark_pkuseg_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_pkuseg_quality.py)｜62 行｜`adf03da1b2da`｜main

- [`scripts/ops/benchmark_translator_ape.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_translator_ape.py)｜436 行｜`384cc616190d`｜_warmup_omlx, _load_certification_rows, _int_env, _selected_suite, _llm_timeout, _skip_gtx, _term_hit_rate, _bench_gtx, _bench_apple_baseline, _bench_ape

- [`scripts/ops/benchmark_tri_model.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/benchmark_tri_model.py)｜356 行｜`dafc18f23544`｜_tw_guard, get_model_id, call_chat, run_benchmark, main

- [`scripts/ops/build_exam_practice_weights.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/build_exam_practice_weights.py)｜290 行｜`9964f1c0add3`｜_number, _clean, _is_structure, _importance_weight, _partition, _allocate, _compile_entry, build, main

- [`scripts/ops/build_ocr_training_dataset.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/build_ocr_training_dataset.py)｜608 行｜`fb3fd868363f`｜FilenameFields, FilenameFields.usable, SourceResult, _sha256_file, _clean_text, parse_filename_fields, _redact_text, _redacted_fields, _canonicalize_training_path_segment, _safe_training_relative_path

- [`scripts/ops/build_operational_attestation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/build_operational_attestation.py)｜118 行｜`d4f57a7b3596`｜_regular_json, build_attestation, _atomic_write, main

- [`scripts/ops/business_module_live_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/business_module_live_check.py)｜2998 行｜`c15f2613e92d`｜_redact_text, _redact_obj, _run, _load_live_environment, _bind_release_local_environment, _infer_live_shared_state_root, _bind_live_shared_state_environment, _parse_last_json, _load_json_file, _pid_alive

- [`scripts/ops/business_outcome_regression.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/business_outcome_regression.py)｜26 行｜`d229f69ac428`｜main

- [`scripts/ops/business_readiness_snapshot.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/business_readiness_snapshot.py)｜935 行｜`8df6cd914800`｜_load_json, _truthy, _pid_alive, _mutable_static_dir, _runtime_dir, _agent_dir, _parse_time, _latest_successful_nvidia_usage, _recent_report_failures, _report_failure_is_now_resolved

- [`scripts/ops/chandra_ocr_healthcheck.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/chandra_ocr_healthcheck.py)｜99 行｜`397899bec763`｜_runtime_path, _entity_counts, run, main

- [`scripts/ops/check_discord_identity.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/check_discord_identity.py)｜55 行｜`aacf4b6bdd95`｜load_token, check_identity

- [`scripts/ops/check_judicial_api_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/check_judicial_api_pipeline.py)｜661 行｜`33bfeb04a515`｜env_path, load_json, read_text, is_judicial_raw_payload, parse_iso, age_hours, list_files, iso_or_empty, rounded, env_bool

- [`scripts/ops/clean_closed_case_residue.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/clean_closed_case_residue.py)｜309 行｜`94114adb4394`｜CaseDir, _is_skip_file, _case_id, _iter_dirs, _iter_case_dirs, _sha256, _conflict_path, _copy_file_verified, _tree_file_count, _plan_merge

- [`scripts/ops/clean_degraded_memories.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/clean_degraded_memories.py)｜166 行｜`c10b81693b8c`｜_get_conn, find_degraded_ids, delete_by_ids, rebuild_faiss, main

- [`scripts/ops/cleanup_fast_judgment_digests.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/cleanup_fast_judgment_digests.py)｜158 行｜`6c7eb4d86ef4`｜_db, _count, _backup_rows, main

- [`scripts/ops/cleanup_judgment_value_noise.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/cleanup_judgment_value_noise.py)｜249 行｜`95fe3f45515b`｜_summary_is_low_value, _is_missing_text_low_value, _is_upper_protected, _load_judgment_collector_action, _write_json, _write_jsonl_gz, _chunks, _fetch_archive_rows, _delete_by_jids, build_cleanup_report

- [`scripts/ops/cleanup_judgments_leaks.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/cleanup_judgments_leaks.py)｜472 行｜`a1a4e946901b`｜has_leak, has_real_content, strip_preamble, url_to_jid, url_to_jid_prefix, jid_to_data_url, _get_db_conn, cleanup_json, cleanup_db, main

- [`scripts/ops/cleanup_synology_empty_case_shells.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/cleanup_synology_empty_case_shells.py)｜508 行｜`893f3de51851`｜_include_local_synology_roots, _scan_local_synology_roots, _is_local_synology_root, _load_env, _db_config, _active_roots, _real_file_count, _case_number_from_folder_name, _same_active_shell_paths, _path_exists

- [`scripts/ops/commercial_readiness_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/commercial_readiness_live.py)｜811 行｜`702e9ef3201f`｜_load_runtime_env, _is_git_worktree, _installed_release, public_source_root, Check, _python, _run_json, _sha256, check_deployment_bindings, check_doctor

- [`scripts/ops/configure_mariadb_master_backup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/configure_mariadb_master_backup.py)｜236 行｜`b07c701597c2`｜_csv_env, _random_password, _socket_conn, _query_status, _load_or_create_credentials, _write_master_config, _restart_mariadb, _create_repl_users, _remote_instruction, parse_args

- [`scripts/ops/configure_mariadb_replica.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/configure_mariadb_replica.py)｜488 行｜`79b0f5a59121`｜_load_env_file, _env, _redact, _truthy, DBProfile, DBProfile.safe_dict, ReplicaTarget, ReplicaTarget.safe_dict, _connect, _query_one

- [`scripts/ops/continuous_longfile_3ch_stress.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/continuous_longfile_3ch_stress.py)｜518 行｜`68c0307bfc73`｜TaskSpec, _now_iso, _append_jsonl, _write_json, _extract_file_path, _read_head, _load_observer_status, _estimate_end_time, _discover_long_pdfs, _worker_process

- [`scripts/ops/controlled_self_evolution.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/controlled_self_evolution.py)｜139 行｜`b776f3241dcc`｜_runtime_dir, _store, _load_json, _write_json, _release_id, main

- [`scripts/ops/controlled_self_evolution_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/controlled_self_evolution_smoke.py)｜105 行｜`b22f9520f554`｜_git, main

- [`scripts/ops/convert_nemotron_parse_to_mlx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/convert_nemotron_parse_to_mlx.py)｜162 行｜`aef8992e8cf3`｜_sha256, _target_dtype, _cast_float, convert, main

- [`scripts/ops/day2_autopilot_monitor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/day2_autopilot_monitor.py)｜200 行｜`a89a3e2e94ba`｜Row, _load_json, _parse_ts, collect, _load_export_txt, render_txt, main

- [`scripts/ops/day3_non24h_test_suite.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/day3_non24h_test_suite.py)｜296 行｜`de3f165f6ebe`｜Check, _http_json, _run_cmd, _load_export_txt, _ensure_tools_api_up, _check_summarize_circuit, _check_transcribe_dual, _check_translate, _check_autopilot_selftest, _check_autopilot_tick_light

- [`scripts/ops/day3_stability_report.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/day3_stability_report.py)｜489 行｜`fa4181078aa4`｜RunRow, HttpProbe, _parse_ts, _load_json, _load_jsonl, _percentile, collect_autopilot, collect_summary_metrics, collect_transcribe_metrics, collect_captcha_queue

- [`scripts/ops/debug_cleanup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/debug_cleanup.py)｜49 行｜`0a6cb07f0d46`｜main

- [`scripts/ops/diagnose_melchior.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/diagnose_melchior.py)｜77 行｜`da4bb8e4918f`｜test_payload

- [`scripts/ops/disk_cleanup_healthcheck.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/disk_cleanup_healthcheck.py)｜2272 行｜`4f68b3e482f0`｜_is_dry_run, _log, _iter_metrics_jsonl, _rotate_metrics_file, cleanup_metrics, _walk_cache_files, _omlx_cache_roots, _external_omlx_cache_roots, _cache_last_used, _omlx_cache_cap_bytes

- [`scripts/ops/disk_low_water_alarm.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/disk_low_water_alarm.py)｜259 行｜`8f9d770a37aa`｜get_disk_free_gb, _push_self_repair, _read_alert_state, _write_alert_state, _should_emit_alert, _auto_reclaim_enabled, _run_auto_reclaim, main

- [`scripts/ops/distill_live_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/distill_live_check.py)｜165 行｜`6cd2908d1973`｜_load_env, _latest_metric, main

- [`scripts/ops/distributed_code_review.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/distributed_code_review.py)｜30 行｜`68aa8a3bc082`｜—

- [`scripts/ops/document_golden_regression.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/document_golden_regression.py)｜249 行｜`91382b8ce7f2`｜load_manifest, validate_manifest, _select_cases, _run_pytest, run_manifest, _csv_set, main

- [`scripts/ops/drive_pending_checksum_verifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/drive_pending_checksum_verifier.py)｜627 行｜`49d7320f729d`｜VerificationError, HashBudgetExceeded, HashEvidence, PendingCandidate, _utc_now, _json_sha256, _file_sha256, _coerce_nonnegative_int, _valid_md5, _candidate_from_item

- [`scripts/ops/ensure_telegram_topics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/ensure_telegram_topics.py)｜35 行｜`bf94edf81075`｜main

- [`scripts/ops/evidence_ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/evidence_ledger.py)｜100 行｜`a4e634731692`｜_read_json, _print, main

- [`scripts/ops/faiss_rebuild_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/faiss_rebuild_worker.py)｜208 行｜`0060f9d2f1c9`｜_sha256_file, _peak_rss_bytes, _load_request, run, _parser, main

- [`scripts/ops/fix_degraded_summaries.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/fix_degraded_summaries.py)｜258 行｜`e97e878db29b`｜_get_db, is_degraded, main

- [`scripts/ops/function_health_index.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/function_health_index.py)｜3185 行｜`face0ad81369`｜_utc_now, _rel, default_runtime_dir, _read_json, _parse_dt, _mtime_dt, _age_hours, _has_skip_part, _literal_string, _literal_value

- [`scripts/ops/gemma4_12b_overlay_live_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/gemma4_12b_overlay_live_gate.py)｜398 行｜`ebbad7dd02c8`｜GateCase, GateReport, _request_json, _chat, _message, _content, _tool_name, _add_case, _run_case, _tools

- [`scripts/ops/generative_quality_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/generative_quality_live.py)｜275 行｜`0637a631a016`｜_load_live_environment, _digest, _quality_codes, _is_nvidia_model, certify_draft, certify_summary, certify_translation, run, main

- [`scripts/ops/git_stage_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/git_stage_guard.py)｜102 行｜`39adcd785001`｜_repo_root, _staged_paths, is_blocked_path, validate_staged_paths, main

- [`scripts/ops/heavy_fallback_live_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/heavy_fallback_live_check.py)｜228 行｜`4bd4471c3ae1`｜_load_env, _temporary_env, _text_of, _compact_result, _assert_usable_success, run_checks, main

- [`scripts/ops/heavy_translation_quality_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/heavy_translation_quality_live.py)｜710 行｜`702b80b317f9`｜_load_env, _draw_wrapped, write_generated_fixture, resolve_fixture_path, _check, _text_of, _extract_fixture, _run_nim_route_check, _read_docx_text, run_gate

- [`scripts/ops/input_method_watchdog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/input_method_watchdog.py)｜330 行｜`89dd938232ce`｜_load_state, _write_state, _processes, _text_services_healthy, _tis_libraries, _cf_string, _current_input_source_id, _select_input_source, _restart_input_stack, _wait_for_exit

- [`scripts/ops/install_gemini_skills.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/install_gemini_skills.py)｜61 行｜`e05eec5479db`｜install_skills

- [`scripts/ops/integration_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/integration_smoke.py)｜455 行｜`3d11ea1abc3f`｜_load_env_file, _now, _read_json_from_text, _http_get_json, _json_headers, _http_post_json, _extract_bool, _capture_import, _run_skill_test, _assess_skill_test

- [`scripts/ops/judgment_nvidia_summary_live_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/judgment_nvidia_summary_live_check.py)｜273 行｜`45a54a999e6a`｜_load_runtime_env, _fetch_candidates, _fetch_rows_by_ids, main

- [`scripts/ops/judgment_summary_live_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/judgment_summary_live_check.py)｜148 行｜`268cb58640b1`｜_load_env, _load_judgment_action, _select_source, _quality, main

- [`scripts/ops/judgment_summary_staged_backfill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/judgment_summary_staged_backfill.py)｜727 行｜`c80e40148bad`｜_read_rows, _write_json_atomic, _write_report, _save_queue, _append_backup, _source_sha, _fetch_rows, _store_summary, _clear_invalid_summary, _queue_row

- [`scripts/ops/laf_deep_extract_backfill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/laf_deep_extract_backfill.py)｜28 行｜`379ac9c38c10`｜—

- [`scripts/ops/laf_gmail_dispatch_scan.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/laf_gmail_dispatch_scan.py)｜599 行｜`1cc4dd3a24cf`｜_output_path, _json_default, _write_json, _load_json, _case_pending_key, _pending_case_row, _update_pending_report, _resolve_gmail_paths, _db_processed_checker, _db_processed_checker.check

- [`scripts/ops/laf_portal_new_files_scan.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/laf_portal_new_files_scan.py)｜127 行｜`8b17c65ba051`｜_write_json, _preserve_last_successful_missing_state, main

- [`scripts/ops/laf_report_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/laf_report_worker.py)｜402 行｜`e594c9f84804`｜_runtime_dir, _append_job_event, _parse_result, _topic_for_action, _action_label, _target_text, _preview_path, _preview_url, _format_success, _format_failure

- [`scripts/ops/live_test_legaltech_taiwan_law_mcp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/live_test_legaltech_taiwan_law_mcp.py)｜116 行｜`0a22e139b833`｜_digest, _official_host, _require, main

- [`scripts/ops/local_deep_queue_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/local_deep_queue_worker.py)｜259 行｜`5e853c13cd03`｜_queue_path, _records, _receipt_rows, _terminal_task_ids, _deferred_until, _receipt, _single_worker_lock, _drain_once_unlocked, _deliver, _job_id_from_reference

- [`scripts/ops/local_model_champion_eval.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/local_model_champion_eval.py)｜54 行｜`cb17982c1054`｜score, load, main

- [`scripts/ops/magi_acceptance_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/magi_acceptance_gate.py)｜1023 行｜`17aa8d92c2b5`｜GateResult, CommandResult, _python, _live_runtime_root, _active_release_marker_path, _live_runtime_state_dir, _doctor_environment, _source_root, _live_runtime_artifact, _tail

- [`scripts/ops/magi_self_repair_guardian.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/magi_self_repair_guardian.py)｜836 行｜`bc91a0755cf7`｜_failed_cron_job_ids, _utc_now, _json_dumps, _ensure_import_root, _safe_resolve, _is_relative_to, _tmp_ownership_marker, _tmp_is_owned, _candidate_age_minutes, _laf_upload_staging_child

- [`scripts/ops/manual_command_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/manual_command_smoke.py)｜273 行｜`c9f5185ae953`｜Check, run_json, route_checks, docx_pdf_checks, safe_cli_checks, main

- [`scripts/ops/memory_lifecycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/memory_lifecycle.py)｜109 行｜`a83240d86314`｜_json, _apply_backend, propagate, main

- [`scripts/ops/memory_watchdog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/memory_watchdog.py)｜816 行｜`eb0a50829ce2`｜MemoryReading, MemoryReading.free_plus_inactive_gb, _read_memory_free_percent, read_memory, memory_pressure_reasons, is_memory_pressure, MetalService, MetalService.footprint_gb, MetalService.graphics_gb, ServiceAction

- [`scripts/ops/merge_judgment_archive_to_court.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/merge_judgment_archive_to_court.py)｜530 行｜`8cf9ac4e3375`｜_db_config, _is_valid_jid, _parse_jid_from_title, _read_full_text, _parse_judgment_date, _court_name_from_level, _court_judgments_lookup, _upsert_court_judgment, merge_archive_to_court, merge_json_to_court

- [`scripts/ops/migrate_laf_status_20260426.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/migrate_laf_status_20260426.py)｜288 行｜`e6bce6e498e9`｜_connect_db, check_schema, collect_candidates, plan_migrations, run_dry, run_apply, main

- [`scripts/ops/model_live_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/model_live_gate.py)｜363 行｜`52444b6c2bdb`｜_schedule_adapter_enabled, _schedule_endpoint_urls, EndpointProbe, ModelGateReport, expected_profile_now, active_profile, probe_port, _has_keyword, _append_unique, _expected_keyword_for_profile

- [`scripts/ops/nemotron_parse_hf_baseline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/nemotron_parse_hf_baseline.py)｜263 行｜`d5f5604e7c0a`｜parse_args, peak_rss_mb, write_output, extract_blocks, run, main

- [`scripts/ops/nemotron_phase1b_compare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/nemotron_phase1b_compare.py)｜233 行｜`5c1e3d0fd135`｜rss_mb, log, render_page, main

- [`scripts/ops/nightly_regression.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/nightly_regression.py)｜574 行｜`bae1fb0b6dd1`｜_run, _suite_report_path, _discord_bot_process, _pid_list, ensure_discord_bot_for_regression, _notify, run_system_test, run_channel_smoke, run_mock_skills, run_core_routes

- [`scripts/ops/observe_stability_24h.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/observe_stability_24h.py)｜145 行｜`7b38ace7ba6a`｜_load_day3_module, _write_json, _append_jsonl, _build_report, _snapshot_row, main

- [`scripts/ops/obsidian_acceptance_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/obsidian_acceptance_gate.py)｜143 行｜`27186cc2ccf6`｜_status_from_result, _check, run_gate, main

- [`scripts/ops/ocr_training_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/ocr_training_pipeline.py)｜327 行｜`44cabaf9dfa4`｜PipelineStats, _path_allowed, collect_candidates, write_candidate_list, _record_hash, _load_state, _save_state, append_silver_to_ocr_distill, run_pipeline, main

- [`scripts/ops/offhost_funnel_canary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/offhost_funnel_canary.py)｜147 行｜`cc8f53e65fa0`｜_tailnet_connected, _addresses, _tls_family, _request, run, main

- [`scripts/ops/omlx_heartbeat_reaper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/omlx_heartbeat_reaper.py)｜273 行｜`8a7cdf996e23`｜OmlxProc, _list_omlx_serves, _parse_etime, find_duplicates, _write_decision, _kill_one, run, main

- [`scripts/ops/omlx_profile_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/omlx_profile_policy.py)｜46 行｜`a4a0ff71aa79`｜expected_profile_for_minutes, expected_profile_now, profile_transition_in_progress

- [`scripts/ops/omlx_switch_gatekeeper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/omlx_switch_gatekeeper.py)｜282 行｜`0187571e1064`｜_aborts_log, _pause_file, _now, _notify_admin, _read_pause_until, _write_pause_until, _append_abort, _read_recent_aborts, cmd_check_paused, _list_omlx_rss

- [`scripts/ops/operational_support.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/operational_support.py)｜37 行｜`b32bb727d6bf`｜main

- [`scripts/ops/osc_auto_backup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_auto_backup.py)｜48 行｜`c11d51c57bc8`｜main

- [`scripts/ops/osc_draft_live_compare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_draft_live_compare.py)｜620 行｜`a1d980f5e8bc`｜_default_sample_active_civil_root, _run, _extract_pdf_text, _ocr_pdf_text, _extract_docx_text, _pdf_page_count, _strip_pdf_artifacts, _looks_like_paragraph_boundary, _compact_ocr_text, _wrap_ocr_text

- [`scripts/ops/osc_events_refresh.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_events_refresh.py)｜2426 行｜`43bb7900b69e`｜_PdfScanTimeout, _portable_source_basename, _parse_roc_year_to_ad, _collect_source_year_candidates, _collect_source_year_candidates._add_candidate, _source_context_for_year_inference, _iter_source_context_values, _infer_source_base_year_from_todo, _source_document_date, _description_base_month_day

- [`scripts/ops/osc_gcal_port_from_paperclip.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_gcal_port_from_paperclip.py)｜111 行｜`f146e1dfef7e`｜main

- [`scripts/ops/osc_gcal_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_gcal_sync.py)｜48 行｜`71738d707336`｜main

- [`scripts/ops/osc_shell_nas_helper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_shell_nas_helper.py)｜617 行｜`6808131e5cd4`｜OscShellNASThreadingHTTPServer, _osc_closed_share_aliases, _user_nas_home, _allowed_roots, _is_path_allowed, _hidden_name, _ListdirHelperError, _ListdirHelperError.__init__, _listdir_payload_uncached, _clone_listdir_payload

- [`scripts/ops/osc_web_stress_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/osc_web_stress_live.py)｜405 行｜`9f7f30465302`｜_load_env, Sample, _percentile, _summarize, _run_json, _resource_snapshot, _build_app, _build_app._StressUser, _build_app._load_user, _route_sample

- [`scripts/ops/paperclip_deep_verify_v6.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/paperclip_deep_verify_v6.py)｜232 行｜`7faaa06748f1`｜ok, fail

- [`scripts/ops/paperclip_filemanager_deep_verify.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/paperclip_filemanager_deep_verify.py)｜625 行｜`076f96e0221e`｜record, login, open_file_manager, http_login_session, main, main._cleanup_sandbox, upload_via_api, upload_multi_via_api, test_chunked_upload, test_chunked_missing_chunk

- [`scripts/ops/pdf_bookmarker_boundary_training.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/pdf_bookmarker_boundary_training.py)｜105 行｜`7fc234602253`｜_training_label, build_training_rows, main

- [`scripts/ops/pdf_mutation_lock.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/pdf_mutation_lock.py)｜68 行｜`8b0a9c32f547`｜PdfMutationLockBusy, PdfMutationLockBusy.__init__, pdf_in_place_mutation_lock_path, _owner_label, pdf_in_place_mutation_lock

- [`scripts/ops/pdf_todo_training_export.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/pdf_todo_training_export.py)｜248 行｜`46c392bc0ab0`｜_TrainingScanTimeout, _time_limit, _time_limit._handle, _load_transcript_module, _folder_kind, _training_label_for_pdf, _training_label_for_transcript, collect_pdf_rows, collect_transcript_rows, write_outputs

- [`scripts/ops/prepare_omlx_gemma4_unified_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/prepare_omlx_gemma4_unified_runtime.py)｜211 行｜`8a9b4b91a12d`｜SourceRepo, _run, _ensure_repo, _patch_omlx_model_discovery, _write_wrapper, _verify, main

- [`scripts/ops/public_push_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/public_push_guard.py)｜125 行｜`d80fdc9a1801`｜_git, _remote_url, _branch_name, _status_lines, _run_audit, check_remote_push, check_public_push, main

- [`scripts/ops/purge_ops_logs_from_vectors.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/purge_ops_logs_from_vectors.py)｜292 行｜`b2fc79db5e78`｜_prefix_where, _prefix_params, _chunked, _connect, count_ops_entries, _ids_for_prefix, _delete_ids, purge_ops, dedup_vectors, rebuild_faiss

- [`scripts/ops/purify_magi_skills.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/purify_magi_skills.py)｜186 行｜`eb5208bbbf20`｜_read_text, _extract_skill_names, _collect_referenced_skills, main

- [`scripts/ops/rc271_case_consistency_repair.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/rc271_case_consistency_repair.py)｜230 行｜`a2a3f6aaef50`｜_load_live_environment, _sha256, _query_cases, _query_closed_case_future_transcript_todos, _transcript_sources, main

- [`scripts/ops/rebuild_pdf_translation_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/rebuild_pdf_translation_docx.py)｜202 行｜`7b0c8c253c28`｜_split_sentence_safe, _is_english_heavy, _translate_english_text, _read_docx_text, rebuild, main

- [`scripts/ops/reconcile_overdue_todos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/reconcile_overdue_todos.py)｜453 行｜`c96e0ef44a2d`｜_next_business_day, _source_kind, _is_optional_or_nonactionable, _original_todo_type, _is_overdue_escalation, _is_past_pure_occurrence, _terminal_status, _overdue_action_label, _actionable_overdue_description, _document_day

- [`scripts/ops/rename_judgment_case_folders.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/rename_judgment_case_folders.py)｜238 行｜`542f354145b2`｜_md5, canonical_name_for_legacy, _same_file, _unique_conflict_path, _merge_dir, _remove_empty_dirs, rename_folder, find_legacy_judgment_folders, run, main

- [`scripts/ops/repair_big_brain.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_big_brain.py)｜48 行｜`e0b4cd2e1b00`｜main

- [`scripts/ops/repair_calendar_false_pdf_todos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_calendar_false_pdf_todos.py)｜453 行｜`5aae0fc025a9`｜_portable_basename, _parse_roc_year_to_ad, _collect_source_year_candidates, _collect_source_year_candidates._add_candidate, _source_context_for_year_inference, _infer_source_base_year_from_row, _source_context_text, _source_context_has_explicit_same_todo_date, _source_document_date, _description_base_month_day

- [`scripts/ops/repair_client_ids.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_client_ids.py)｜110 行｜`c0d5b1e8c38e`｜RepairItem, _fetch_bad_clients, _fetch_reference_columns, build_repair_plan, execute_plan, main

- [`scripts/ops/repair_drive_imported_case_folders.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_drive_imported_case_folders.py)｜700 行｜`2e87c0754187`｜file_md5, is_noncanonical_drive_folder, mapped_file_relative_path, _case_category_from_path, _canonical_case_first_segments, _existing_case_first_segments, _iter_files, _remove_empty_dirs, _unique_conflict_target, _record_file_move

- [`scripts/ops/repair_drive_native_case_layout.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_drive_native_case_layout.py)｜397 行｜`a3a81d10dcf8`｜_native_target_for_numbered_folder, _case_review_bucket, repair_case_layout, _run_unlocked, run, main

- [`scripts/ops/repair_fileprovider_case_to_nas.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_fileprovider_case_to_nas.py)｜435 行｜`6e24fdb327fe`｜RepairError, _utc_now, _canonical_json, _sha256_bytes, _is_lower_hex64, _is_file_provider_source, _regular_directory, _hash_regular_file, build_tree_manifest, _copy_file_exact

- [`scripts/ops/repair_insight_summaries.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_insight_summaries.py)｜246 行｜`de06cd346a03`｜_get_db, _is_raw_unsummarized, _extract_raw_text_from_insight, _summarize_via_gateway, _update_insight, _ensure_legal_insights_degraded_column, main

- [`scripts/ops/repair_pdf_bookmark_labels.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_pdf_bookmark_labels.py)｜1424 行｜`0dc8cd341a55`｜_resolve_runtime_dir, PerFileTimeout, _retry_identity, _retry_key, _load_retry_state, _save_retry_state, _active_retry_entry, _is_promoted_repair_retry, _repair_timeout_for, _record_retry_backoff

- [`scripts/ops/repair_transcript_filenames.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_transcript_filenames.py)｜258 行｜`9a374e7991fe`｜HashOnlyDownloader, HashOnlyDownloader._calculate_file_md5, HashOnlyDownloader._parse_record_pdf, _load_downloader, _safe_name, _unique_path, _standard_name, _strip_collision_suffix, _collision_rank, _folder_matches

- [`scripts/ops/repair_unusable_judgment_summaries.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/repair_unusable_judgment_summaries.py)｜389 行｜`0a67f0a7057f`｜_source_sha, _write_json_atomic, _append_backup, _court_backup_row, _court_quality, _fetch_court_batch, _delete_placeholder_legal_insights, _clear_degraded_archive_payloads, _queue_payloads, main

- [`scripts/ops/resource_governor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/resource_governor.py)｜276 行｜`dd55d73ff9b2`｜_memory_watchdog, ResourceSnapshot, ResourceDecision, collect_snapshot, _read_memory_free_percent, classify, classify.raise_to, append_metric, safe_cleanup, prepare_switch

- [`scripts/ops/resource_guarded_run.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/resource_guarded_run.py)｜588 行｜`4f581f69ba2b`｜_ForwardedTermination, _ForwardedTermination.__init__, _reap_after_signal, _append_event, _retryable_interruption_deferred_payload, _iso_now, _write_json_atomic, _drive_status_is_terminal_completion, _mark_drive_sync_guard_timeout, _drive_sync_kind_for_job

- [`scripts/ops/restart_discord.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/restart_discord.py)｜65 行｜`7a40fae759fb`｜restart_discord_bot

- [`scripts/ops/resummary_legacy_judgments_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/resummary_legacy_judgments_quality.py)｜708 行｜`ea9500718d8c`｜_load_judgment_action, _needs_resummary, _new_summary_is_usable, _generate_summary, _count_summary_meta, _append_backup, _load_resume_cursor, _save_resume_cursor, _record_quality_rejection, _load_reviewed_quality

- [`scripts/ops/rotate_service_logs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/rotate_service_logs.py)｜74 行｜`0ff642df5389`｜rotate_file, main

- [`scripts/ops/run_after_token_refresh.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/run_after_token_refresh.py)｜143 行｜`5b971fd05c5c`｜_short_failure_summary, _parse_env_prefix, _parse_required_checks, _gate_failures, main

- [`scripts/ops/run_auto_skill_import.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/run_auto_skill_import.py)｜312 行｜`e096a67db03f`｜_runtime_dir, _receipt_path, _atomic_write_json, _toolsai_count, _fresh_payload, _open_guardian_signals, _open_business_signals, _plan_controlled_candidates, _summary_text, _notify

- [`scripts/ops/run_daemon_no_site.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/run_daemon_no_site.py)｜40 行｜`01893e563b76`｜—

- [`scripts/ops/run_menubar_no_site.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/run_menubar_no_site.py)｜44 行｜`a6c9c2991283`｜—

- [`scripts/ops/run_test_suite.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/run_test_suite.py)｜300 行｜`4856e3a58035`｜load_bound_test_environment, resolve_runtime_output, CheckResult, SuiteReport, load_matrix, resolve_command, should_skip, tail, run_check, list_suites

- [`scripts/ops/run_with_env.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/run_with_env.py)｜37 行｜`a17ae19aacee`｜main

- [`scripts/ops/sanitize_definitions.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/sanitize_definitions.py)｜194 行｜`ec6fcd876e17`｜_discover_runnable_skill_dirs, _extract_default_skill_from_tool, _infer_skill_from_run_tool_name, _sanitize_payload, main

- [`scripts/ops/schedule_auto_skill_import_daily.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/schedule_auto_skill_import_daily.py)｜29 行｜`65628b20edb2`｜main

- [`scripts/ops/schedule_code_skill_cycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/schedule_code_skill_cycle.py)｜21 行｜`7711e4d55c55`｜main

- [`scripts/ops/schedule_db_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/schedule_db_sync.py)｜16 行｜`666270813de5`｜—

- [`scripts/ops/schedule_fixture_contract.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/schedule_fixture_contract.py)｜336 行｜`57d81a56682b`｜ScheduleFixtureError, _SafetyObservation, _SafetyObservation.__init__, _SafetyObservation._path, _SafetyObservation._open_is_write, _SafetyObservation.record, _SafetyObservation._is_loopback, _SafetyObservation._is_nas_path, _SafetyObservation.receipt, _audit_hook

- [`scripts/ops/scheduled_reboot_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/scheduled_reboot_guard.py)｜322 行｜`baa4efde6f11`｜RebootDecision, _truthy, _minute_of_day, _window_for_mode, _mode_from_auto, _inside_window, _ps_rows, _active_magi_blockers, _office_unsaved_blockers, _already_rebooted_today

- [`scripts/ops/selfhost_minimal_import_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/selfhost_minimal_import_check.py)｜90 行｜`edab495d1903`｜_environment, main

- [`scripts/ops/selfhost_portability_audit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/selfhost_portability_audit.py)｜260 行｜`250fc284a73c`｜_finding, collect, main

- [`scripts/ops/selfhost_release_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/selfhost_release_smoke.py)｜170 行｜`5893f50be9e3`｜_check, run_smoke, main

- [`scripts/ops/selftest_big_brain.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/selftest_big_brain.py)｜39 行｜`2a312fe24894`｜main

- [`scripts/ops/skill_realworld_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/skill_realworld_smoke.py)｜336 行｜`a5a75eb03b7c`｜SkillRunResult, _create_sample_pdf, _create_sample_fields_json, _env, _pick_command, _timeout_for, _classify, run_matrix, write_reports, _parse_args

- [`scripts/ops/slow_archive_closed_cases.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/slow_archive_closed_cases.py)｜1600 行｜`0579c307c116`｜_default_archive_roots, _offpeak_now, _stat_quick, _stat_quick._run, _is_dir_quick, _is_skip_name, _iter_case_dirs_at_depth, _case_root_rank, _unique_paths, _closed_case_roots

- [`scripts/ops/smart_model_router_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smart_model_router_live.py)｜155 行｜`12824c20ddc5`｜_case, _chat_probe, build_report, main

- [`scripts/ops/smoke_all_desktop_pdfs_3tasks.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_all_desktop_pdfs_3tasks.py)｜412 行｜`e758ba847d73`｜_now_ts, _discover_pdfs, _extract_file_path, _contains_error_text, _read_head, main, main._forced_dist_fail, main._quick_stub, main._summary_pdf_stub, main._resilient_summary_stub

- [`scripts/ops/smoke_core_routes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_core_routes.py)｜209 行｜`caaa4dcad5ac`｜Case, _cases, _run_case, CaseTimeoutError, _alarm_handler, _normalize_tokens, _classify_case_output, main

- [`scripts/ops/smoke_external_chat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_external_chat.py)｜124 行｜`2768eeca4111`｜main

- [`scripts/ops/smoke_judgment_translation_3ch.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_judgment_translation_3ch.py)｜239 行｜`0382c2a2391c`｜_now_ts, _pick_three_pdfs, _extract_file_path, _read_head, _make_fake_route_functions, _make_fake_route_functions._fake_distributed_chat, _make_fake_route_functions._fake_quick_local_chat, _run_case, main, main._fake_summary

- [`scripts/ops/smoke_nvidia_nim.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_nvidia_nim.py)｜280 行｜`a4a091ad602d`｜_ok, _fail, _skip, test_env_config, test_model_allowlist, test_pii_scrubber, test_nim_config_layer, test_live_api_call, test_usage_log, main

- [`scripts/ops/smoke_test_full.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_test_full.py)｜1284 行｜`e09c692974da`｜_load_runtime_env, _installed_release, _installed_release_note, _output_path, _env_int, TestResult, SmokeReport, SmokeReport.add, run_test, _git_is_tracked

- [`scripts/ops/smoke_three_channels.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_three_channels.py)｜605 行｜`7eb98f12c033`｜Check, _load_json, _mask, _is_privateish_webhook_endpoint, _http_json, _parse_webhook_id_token, _line_channel_access_token, _line_channel_secret, _discord_bot_token, _discord_webhook

- [`scripts/ops/smoke_three_channels_e2e.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_three_channels_e2e.py)｜175 行｜`183f68f52e77`｜_ok, _fail, _skip, test_telegram_e2e, test_discord_webhook_e2e, test_line_webhook_e2e, main

- [`scripts/ops/smoke_translation_docx_quality_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/smoke_translation_docx_quality_gate.py)｜69 行｜`44a450cfca7b`｜_make_docx, main

- [`scripts/ops/start_slow_archive_closed_cases.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/start_slow_archive_closed_cases.py)｜175 行｜`2fb6382a44a9`｜_pid_alive, _read_pid, _write_json, main

- [`scripts/ops/strip_cron_last_run.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/strip_cron_last_run.py)｜72 行｜`eec3c010b7ee`｜main

- [`scripts/ops/sync_keeper_db.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/sync_keeper_db.py)｜108 行｜`0188c4546760`｜sync_database

- [`scripts/ops/system_diagnostic_report.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/system_diagnostic_report.py)｜358 行｜`6a5196e37ace`｜_health_urls, _probe_url, _memory_free_percent, _command_references_root, _rss_summary, _load_json, _parse_state_time, _schedule_summary, collect_report, _write_atomic

- [`scripts/ops/tailscale_funnel_healthcheck.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/tailscale_funnel_healthcheck.py)｜1545 行｜`4a24c97003ed`｜_load_dotenv, _run, _append_unique, _parse_curl_http_code, _is_public_ip, _extract_location_header, _mobile_redirect_ok, _tailscale_bin, _tailscale_cli_usable, _load_funnel_status

- [`scripts/ops/test_distributed_review_small.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/test_distributed_review_small.py)｜31 行｜`a8244be9e9bf`｜test_small_review

- [`scripts/ops/test_melchior_connection.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/test_melchior_connection.py)｜76 行｜`7e148c04bbe8`｜check_health, switch_mode, main

- [`scripts/ops/test_skill_sync_sim.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/test_skill_sync_sim.py)｜63 行｜`007208a4c0da`｜Logger, Logger.info, Logger.error, test_sync_skills

- [`scripts/ops/test_summarize_judgments.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/test_summarize_judgments.py)｜198 行｜`515873fd6b1b`｜extract_text

- [`scripts/ops/token_health_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/token_health_check.py)｜757 行｜`5cc618f205e3`｜GoogleTokenSpec, ApiKeySpec, _load_local_env, _truthy, _configured_env, _usable_secret, _safe_status_ok, _parse_expiry, _normalize_scopes, _decode_jwt_payload

- [`scripts/ops/tool_confusion_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/tool_confusion_guard.py)｜41 行｜`a2e4d30f39aa`｜main

- [`scripts/ops/transcript_archive_consistency.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/transcript_archive_consistency.py)｜270 行｜`ab65065dea31`｜_sha256, _load_live_environment, _load_judicial_module, _pdf_dockets, _transcript_pdfs, _unique_destination, main

- [`scripts/ops/triage_transcript_duplicates.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/triage_transcript_duplicates.py)｜319 行｜`8ef3bb58be8f`｜_md5, _content_hash, _find_n_suffix_files, _strip_suffix, _bucket, _move_to_duplicates, _append_jsonl, main

- [`scripts/ops/tune_judicial_api_load.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/tune_judicial_api_load.py)｜206 行｜`29d84057f913`｜_python_bin, _truthy, _env_assignment, _job_command, _find, tune_jobs, main

- [`scripts/ops/v3_host_singleton_migration.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/v3_host_singleton_migration.py)｜312 行｜`42f1b3324c1b`｜HostSingletonMigrationError, _contains_v2, _contains_versioned_release, _path, render_migrated_plist, stage_migrations, main

- [`scripts/ops/weekly_cache_cleanup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/weekly_cache_cleanup.py)｜509 行｜`7f5e9bb55dfb`｜_external_omlx_cache_targets, _is_protected, _dir_size_bytes, _has_preserved_standalone_content, _max_atime, cleanup_target, cleanup_retired_root, write_metrics, report_failure, main

- [`scripts/packaging/build_installers.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/packaging/build_installers.py)｜287 行｜`7873001cc760`｜_run, _copy, build_release_archive, _write_macos_launcher, _write_info_plist, _write_macos_readme, build_macos_app, _write_windows_cmd, _write_windows_builder, _zip_folder

- [`scripts/packaging/magi_install_launcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/packaging/magi_install_launcher.py)｜376 行｜`5ea947475bd1`｜is_frozen, resource_dir, default_archive, default_install_base, _safe_zip_target, _single_top_level, extract_release_archive, _run_probe, find_python, python_command

- [`scripts/packaging/runtime_bootstrap.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/packaging/runtime_bootstrap.py)｜1007 行｜`055f828ac32d`｜HardwareProfile, ModelDownload, RuntimePlan, BootstrapStep, AuxiliaryDependency, _run, _which, _which_any, _probe, _subprocess_text

- [`scripts/packaging/supply_chain_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/packaging/supply_chain_gate.py)｜140 行｜`3e1cb39ed8bc`｜_write, main

- [`scripts/packaging/validate_installer_payload.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/packaging/validate_installer_payload.py)｜141 行｜`d5d126429146`｜_find_archive, _extract_release, _all_release_paths, _run, validate_payload, main

- [`scripts/packaging/vulnerability_receipt.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/packaging/vulnerability_receipt.py)｜42 行｜`8f410f588ace`｜main

- [`scripts/public_release_audit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/public_release_audit.py)｜358 行｜`e45f8023f34f`｜_is_git_worktree, default_audit_root, Finding, _git_ls_files, _walk_release_files, _is_probably_text, _is_blocked_tracked_path, _is_allowed_secret_example, _is_allowed_pii_example, scan_text

- [`scripts/purge_persona_memories.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/purge_persona_memories.py)｜122 行｜`d99d105fd92a`｜get_conn, find_polluted, delete_ids, main

- [`scripts/refresh_all_law_school_catalog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/refresh_all_law_school_catalog.py)｜342 行｜`090dc3730842`｜institution, fetch, paper_uid, answer_fields, base_paper, parse_ncku, parse_ccu, nccu_papers, refresh, main

- [`scripts/refresh_moex_judicial_bar_bank.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/refresh_moex_judicial_bar_bank.py)｜955 行｜`507969ac3e50`｜now_iso, normalized, subject_key, exam_kind, Builder, Builder.__init__, Builder.fetch, Builder.archive_pdf, Builder.write_manifest, pdf_text

- [`scripts/reprocess_insights.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/reprocess_insights.py)｜860 行｜`f5fbf02ee814`｜_looks_degraded, _write_json_report, _load_reprocess_fixture_provider, _fixture_provider_value, _require_formal_callable, _select_reprocess_rows, _run_schedule_fixture, _run_schedule_fixture._call_bounded_provider, _get_db, _build_ssl_context

- [`scripts/resummary_batch.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/resummary_batch.py)｜95 行｜`4e5fdd72f31c`｜wait_for_omlx

- [`scripts/revise_feature_manual.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/revise_feature_manual.py)｜135 行｜`797ca46fa050`｜_replace_paragraphs, _replace_cells, main

- [`scripts/seed_cron_jobs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/seed_cron_jobs.py)｜1871 行｜`1ee6a2320e24`｜qcmd, quote_repo_root_paths, default_python_path, guarded_cron_command, token_refresh_cron_command, worldmonitor_job, deterministic_legacy_replacements, business_jobs, operational_jobs, load_jobs

- [`scripts/serve_mlx_mtp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/serve_mlx_mtp.py)｜208 行｜`a7918b34e45b`｜ChatMessage, ChatRequest, RuntimeState, RuntimeState.__init__, _model_id, _content_to_text, _normalize_messages, _load_runtime, create_app, create_app.lifespan

- [`scripts/serve_nemotron_parse_omlx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/serve_nemotron_parse_omlx.py)｜93 行｜`47a0dec835dc`｜_image_from_request, create_app, create_app.runtime, create_app.health, create_app.parse, main

- [`scripts/setup_taiwan_legal_mcp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/setup_taiwan_legal_mcp.py)｜38 行｜`5f2ad56d82c4`｜run, main

- [`scripts/share_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/share_gateway.py)｜181 行｜`ab9dc7cc159c`｜_header_get, build_upstream_headers, ShareGatewayHandler, ShareGatewayHandler.do_GET, ShareGatewayHandler.do_HEAD, ShareGatewayHandler.do_POST, ShareGatewayHandler.do_PUT, ShareGatewayHandler.do_DELETE, ShareGatewayHandler.log_message, ShareGatewayHandler._not_found

- [`scripts/share_tunnel_supervisor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/share_tunnel_supervisor.py)｜211 行｜`4bdd0c569de7`｜_load_dotenv_value, _normalize_base_url, _stable_share_base_url, _gateway_health_ok, _wait_for_gateway, _cleanup_orphan_cloudflared, main

- [`scripts/sunrise_protocol.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/sunrise_protocol.py)｜13 行｜`89eb9b9ab3f2`｜—

- [`scripts/supreme_interpreter_pdf_backfill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/supreme_interpreter_pdf_backfill.py)｜329 行｜`46e5be636b31`｜load_dotenv, safe_title, text_width_units, wrap_text_line, wrap_text, render_text_pdf, render_text_pdf.new_page, txt_to_pdf_name, build_authoritative_index_by_current_txt, generate_existing_pdfs

- [`scripts/sync_exam_tutor_trends.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/sync_exam_tutor_trends.py)｜823 行｜`912eed727501`｜_document_text, now_iso, load_json, atomic_json, _VisibleText, _VisibleText.__init__, _VisibleText.handle_starttag, _VisibleText.handle_endtag, _VisibleText.handle_data, _VisibleText.text

- [`scripts/sync_exam_tutor_yearly.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/sync_exam_tutor_yearly.py)｜706 行｜`293fda158356`｜now_iso, current_roc_year, sha256, atomic_json, load_json, fetch, discover_exam_code, _subject_key, discover_choice_papers, pdf_text

- [`scripts/sync_insights_to_vectors.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/sync_insights_to_vectors.py)｜660 行｜`745e601c876c`｜_embedding_is_valid, _get_embedding, _get_embeddings_batch, _content_hash, _build_mem_content, _plan_new_insights, _load_embedding_fixture_provider, _fixture_embedding, _require_formal_callable, sync

- [`scripts/tests/apple_intelligence_smoke_test.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/tests/apple_intelligence_smoke_test.py)｜224 行｜`56e50c4e990a`｜_run, _pick_default_files, _pick_default_files._first_existing, main, main._post_json

- [`scripts/tests/intent_model_ab.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/tests/intent_model_ab.py)｜137 行｜`b1ee8543ed6e`｜_extract_label, _ask_label, run_benchmark, main

- [`scripts/tests/test_legal_skills.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/tests/test_legal_skills.py)｜99 行｜`3b5e2939a9c7`｜_analysis_worker, test_doc_analysis

- [`scripts/tests/test_url_browsing.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/tests/test_url_browsing.py)｜31 行｜`180daeaa15b7`｜test_url_browsing

- [`scripts/train_gemma_e4b_lora.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/train_gemma_e4b_lora.py)｜621 行｜`a5f8524cd40f`｜_atomic_json, _bounded_owned_profile, _bounded_rows, _bounded_loss, run_bounded_training, _version_tag, _adapter_dir, _merged_dir, _check_simplified_chinese, _build_validation_messages

- [`scripts/v3_backup_prepare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_backup_prepare.py)｜662 行｜`b4349a31bf84`｜BackupBlocked, SourceEntry, SourceEntry.capture, SourceEntry.signature, _sha256, _write_json, _overlaps, _safe_source, _safe_website_root, _quick_check

- [`scripts/v3_backup_verify.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_backup_verify.py)｜39 行｜`a6d7168e95a0`｜main

- [`scripts/v3_campaign/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_campaign/__init__.py)｜22 行｜`bb2f30b66025`｜__getattr__

- [`scripts/v3_campaign/__main__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_campaign/__main__.py)｜3 行｜`74fff10eb2bd`｜—

- [`scripts/v3_campaign/offline_probes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_campaign/offline_probes.py)｜1018 行｜`f64963b6006a`｜OfflineProbeError, _sha256, bound_cron_jobs, bound_dispatch_policy, _cron_values, _cron_matches, _worker_class, _timeout, _replay_profile, _base_arrivals

- [`scripts/v3_campaign/runner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_campaign/runner.py)｜3934 行｜`231da508da4c`｜_route_external_storage_roots, _route_live_root, _route_seatbelt_profile, _route_seatbelt_attestation, _route_attested_seatbelt_workspace, _route_runtime_site_packages, _route_inside, _validate_route_runtime_binding, CampaignSafetyError, CampaignContext

- [`scripts/v3_campaign/schedule_realism.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_campaign/schedule_realism.py)｜1034 行｜`3d879dbcdf98`｜_sha256_bytes, _source_evidence_receipt_sha256, _fixture_inventory, _diagnostic_text, _write_execution_diagnostic, _p95, _logical_definition_sha256, _command_definition_sha256, _load_baseline, _validate_baseline

- [`scripts/v3_credential_handoff_prepare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_credential_handoff_prepare.py)｜273 行｜`6dd0bb491905`｜SecretHandoffError, _sha256, _regular_bytes, _atomic_replace, _safe_directory, materialize_secret_handoff, main

- [`scripts/v3_cron_snapshot.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cron_snapshot.py)｜347 行｜`db3ae149ddc5`｜CronSnapshotBlocked, CronSourceIdentity, _identity, _source_path, _assert_source_identity, read_verified_cron_source, _clean_job, _source_roots, _release_files, _rebase_absolute

- [`scripts/v3_cutover/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/__init__.py)｜21 行｜`81d12c5d2f3f`｜—

- [`scripts/v3_cutover/__main__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/__main__.py)｜3 行｜`935a1c1166b0`｜—

- [`scripts/v3_cutover/activation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/activation.py)｜1278 行｜`2e7d449ce625`｜_now, _canonical_json, _sha256_bytes, _chain_entry, _validate_history, _safe_parent, _atomic_replace, _exclusive, _load, active_release_marker

- [`scripts/v3_cutover/cli.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/cli.py)｜358 行｜`dff29513afd2`｜_parser, _residuals, _evidence_report, _write_execution_report, run, run.current_snapshot, main

- [`scripts/v3_cutover/core.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/core.py)｜385 行｜`c927a0853bbb`｜CutoverError, GateConfigError, Owner, Owner.to_dict, Snapshot, Snapshot.to_dict, Assessment, Assessment.to_dict, assess_cutover_window, assess_cutover_window.parse_clock

- [`scripts/v3_cutover/mutation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/mutation.py)｜3338 行｜`8f929c7c20cd`｜MutationResult, BoundFile, LaunchAgent, LAFDedupHandoffPlan, PdfNamerHandoffPlan, MutableStateHandoffPlan, PreparedCutoverPlan, v2_application_set_sha256, v2_initial_loaded_set_sha256, v2_keepalive_set_sha256

- [`scripts/v3_cutover/planning.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/planning.py)｜546 行｜`80cc9b018623`｜_default_launchd_probe, _captured_launchd_state, _sha256, _regular, _object, _binding, _path_identity, _handoff_root, _private_binding, _future_output

- [`scripts/v3_cutover/probe.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/probe.py)｜908 行｜`b324db04713e`｜observe_probe_commands, _run_probe_command, ReleaseSpec, ReleaseSpec.to_dict, ProcessInfo, _all_strings, _domain_for_text, _host_singleton_process_identity, _looks_like_unapproved_model_process, _path_within

- [`scripts/v3_cutover/v3_rotation_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/v3_rotation_execute.py)｜1327 行｜`456bf8c63f4d`｜_now, _sha256_bytes, _sha256, _canonical_json, _safe_absolute_directory, _safe_file_bytes, _load_json_bytes, BoundFile, BoundDeployment, _verify_release_inventory

- [`scripts/v3_cutover/v3_rotation_recover.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/v3_rotation_recover.py)｜44 行｜`8865180b874e`｜main

- [`scripts/v3_cutover/workflow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_cutover/workflow.py)｜148 行｜`25285655e2eb`｜Step, Step.to_dict, build_workflow, _validate_workflow, authorize_mutation, _owner_templates, simulate_workflow

- [`scripts/v3_deploy_prepare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_deploy_prepare.py)｜3114 行｜`d386f8693d7d`｜DeployPrepareBlocked, ReleaseIdentity, RoleDefinition, ExternalRuntimeInputs, StaticExternalEvidence, _sha256, _sha256_file, _sandbox_literal, _probe_process_exec_paths, _probe_seatbelt_profile

- [`scripts/v3_evidence_compiler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_evidence_compiler.py)｜2602 行｜`ebab123c0bfe`｜EvidenceCompileError, CompileContext, CompileContext.as_dict, CompileContext.validate, SourceArtifact, FrozenFile, _with_compilation_scope, _with_compilation_scope.wrapped, _reject_duplicate_json_keys, _freeze

- [`scripts/v3_laf_dedup_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_laf_dedup_compat.py)｜831 行｜`a8d9ae7732d1`｜LAFDedupBlocked, _canonical_json, _sha256_bytes, _validate_message_id, _source_signature, _read_source, _records_sha256, _sources_sha256, _write_owner_only, create_manifest

- [`scripts/v3_mutable_state_handoff.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_mutable_state_handoff.py)｜79 行｜`530d0532211d`｜_parser, main

- [`scripts/v3_pdf_namer_handoff.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_pdf_namer_handoff.py)｜769 行｜`9b980fc286ed`｜StateSpec, HandoffError, SnapshotEntry, SnapshotEntry.public, _sha256_bytes, _path_binding, _is_relative_to, _reject_symlink_components, _canonical_paths, _file_signature

- [`scripts/v3_pre_cutover.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_pre_cutover.py)｜2164 行｜`81bed136c81f`｜PreCutoverError, ExpectedContext, ExpectedContext.to_dict, RequiredPaths, _load_json, _sha256, _write_json_atomic, _valid_digest, _parse_time, _safe_relative

- [`scripts/v3_python_runtime_snapshot.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_python_runtime_snapshot.py)｜583 行｜`3a39340cfd8d`｜_PortableFileLock, _PortableFileLock.flock, PythonRuntimeBlocked, _owned_by_current_user, _private_file_mode, _sha256_file, _runtime_root, _inside, _validate_pth, _validate_excluded_bytecode_cache

- [`scripts/v3_release_bundle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_release_bundle.py)｜1301 行｜`184ca8b4faa0`｜ReleaseBundleError, SourceEntry, SourceEntry.manifest_entry, _sha256_file, _excluded, _release_privacy_audit, _forbidden_secret, _entry_from_file, _scan_directory, snapshot_sources

- [`scripts/v3_release_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_release_gate.py)｜3978 行｜`159c02c655fb`｜_source_contract, _nonnegative_int, _exact_nonnegative_int, MetricRule, EvidenceSpec, BoundArtifact, _r, _reject_duplicate_json_keys, _load_json_value_bytes, _load_json_bytes

- [`scripts/v3_route_parity.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_route_parity.py)｜182 行｜`156b76ded6db`｜Route, Route.from_mapping, collect_routes, load_expected, resolve_factory, _http_services, verify_route_parity, main

- [`scripts/v3_schedule_baseline_capture.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_schedule_baseline_capture.py)｜418 行｜`f602734486a4`｜BaselineCaptureError, _sha256_file, _load_json, _normalized_timestamp, _successful_sample, _prior_samples, _observation, capture_baseline, _write_atomic, main

- [`scripts/v3_source_contract.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_source_contract.py)｜91 行｜`fcb8ce25af25`｜SourceContractError, account_home, resolve_source_contract

- [`scripts/v3_static_external_staging.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_static_external_staging.py)｜1046 行｜`cd83492f6438`｜StaticExternalStagingError, ReleaseContext, SourceBinding, SnapshotEntry, _sha256, _json_bytes, _validate_sha, _canonical_existing, _stable_regular_bytes, _load_json

- [`scripts/v3_validation/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/__init__.py)｜26 行｜`09242a9098c4`｜—

- [`scripts/v3_validation/__main__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/__main__.py)｜96 行｜`fe9056306385`｜_write, build_parser, main

- [`scripts/v3_validation/actual_route_replay.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/actual_route_replay.py)｜6051 行｜`20381dfe6a2d`｜_FormalRuntimeBindingError, _verify_formal_runtime_binding, ReplayIsolationError, _canonical, _digest, _route_key, _key_dict, _is_within, _is_lexically_within, _external_storage_roots

- [`scripts/v3_validation/adapter_spec.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/adapter_spec.py)｜130 行｜`43c5171a4212`｜LegacyResponse, assert_legacy_shape, _sse_data, _error_payload, adapt_legacy_response

- [`scripts/v3_validation/build_rc606_canonical_cron.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/build_rc606_canonical_cron.py)｜130 行｜`a86342a249fa`｜_sha, _write_json, _rc606_command, main

- [`scripts/v3_validation/change_scope.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/change_scope.py)｜169 行｜`baeb96e69975`｜ScopeDecision, _normalise, _bound_source, _is_explicit_pure, _development_reason, classify_paths, build_receipt, changed_paths, main

- [`scripts/v3_validation/controlled_restart_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/controlled_restart_evidence.py)｜751 行｜`bee5270ba12b`｜ControlledRestartBlocked, _canonical, _semantic, _sha256, _json, _write_new, _context, _time, HostObservation, HostObservation.public

- [`scripts/v3_validation/cutover_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/cutover_evidence.py)｜812 行｜`b4bde3029e0f`｜CutoverEvidenceBlocked, RawPair, _sha256, _canonical, _json, _time, _hash_context, _load_plan, _reconciliation, _v2_marker_identity

- [`scripts/v3_validation/fault_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/fault_certification.py)｜608 行｜`48e5b32cd884`｜FaultCertificationError, _canonical_json, _sha256_file, _is_relative_to, _prepare_sandbox, _compile_mach_killer, _mach_sigkill_cycle, verify_fault_certification, _validation_profile, build_fault_stimulus_plan

- [`scripts/v3_validation/fault_realism.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/fault_realism.py)｜1502 行｜`59db3c0a618f`｜FaultEvidenceError, _canonical_json, _sha256, _is_relative_to, _validate_workdir, _configure, _initialize, _initialize_apfs_enospc_database, _apfs_enospc_sqlite_full_worker, _apfs_enospc_recovery_worker

- [`scripts/v3_validation/fixtures.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/fixtures.py)｜325 行｜`1d551d53022b`｜_normalized_key, _tag, _redact_text, _redact_text.replace, _redact_text.replace.repl, _anonymize, anonymize_fixture, _walk, assert_fixture_anonymized, decode_fixture_file_payload

- [`scripts/v3_validation/g8_isolated_smb.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/g8_isolated_smb.py)｜1372 行｜`6cac3815b30f`｜G8SMBBlocked, _canonical, sha256_json, _sha, _new_file, _regular, _json_file, _empty_target, _mount_unescape, _verify_command_receipt

- [`scripts/v3_validation/g8_maintenance_safety.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/g8_maintenance_safety.py)｜118 行｜`f0be51ff769a`｜ProcessRow, parse_ps_rows, ancestor_pids, eligible_v2_process_groups, reverify_group

- [`scripts/v3_validation/golden_flows.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/golden_flows.py)｜704 行｜`489257f998a4`｜_canonical, _digest, _fixture, _User, _isolated_app, _isolated_app.load_user, _isolated_app.contract_login_page, _isolated_app.contract_login, _isolated_app.contract_gcal_state, _isolated_app.contract_gcal_state_status

- [`scripts/v3_validation/health_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/health_certification.py)｜281 行｜`f09567236103`｜HealthCertificationError, _canonical, _sha256_file, _inside, _prepare_sandbox, _heavy_imports, _validation_profile, run_health_certification, verify_health_evidence, campaign_evidence

- [`scripts/v3_validation/http_contract_runner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/http_contract_runner.py)｜640 行｜`60ccd28a1690`｜_canonical_json, _sha256, _canonical_hash, _normalized_headers, OfflineIsolationAttestation, OfflineIsolationAttestation.validate, MultipartFile, ContractRequest, InjectedTestClient, InjectedTestClient.wsgi

- [`scripts/v3_validation/human_approval.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/human_approval.py)｜1395 行｜`606418ec302f`｜HumanApprovalBlocked, FrozenSource, _canonical, _sha, _json_bytes, _freeze, _safe_artifact, _time, _write_new, _context

- [`scripts/v3_validation/ime_candidate_probe.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/ime_candidate_probe.py)｜745 行｜`b35d722f2ab0`｜ImeProbeError, _run_osascript, _frontmost_application, _activate_process, _wait_for_frontmost_application, _wait_for_input_source_id, _textedit_readiness_state, _wait_for_textedit_ready, _activate_and_wait_for_textedit_ready, _open_isolated_document

- [`scripts/v3_validation/inventory.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/inventory.py)｜233 行｜`723e8536c869`｜RouteRecord, RouteRecord.from_mapping, RouteRecord.as_dict, RouteRecord.signature, normalize_inventory, inventory_fingerprint, _capability_ids, classify_capability, build_coverage, build_coverage.describe

- [`scripts/v3_validation/isolated_live_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_live_evidence.py)｜733 行｜`ebdf8afb7329`｜IsolatedLiveEvidenceBlocked, RawRun, _sha256, _json, _time, _threshold, _campaign_policy, _subsequence, _snapshot, _canonical_json_bytes

- [`scripts/v3_validation/isolated_live_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_live_execute.py)｜1946 行｜`3cf627be750b`｜IsolatedLiveBlocked, BoundArtifact, ProbeSpec, ProbeSpec.to_dict, ValidationRole, IsolatedLivePlan, VerifiedDeployment, IsolatedLiveMachine, IsolatedLiveMachine.activate_maintenance_blackout, IsolatedLiveMachine.deactivate_maintenance_blackout

- [`scripts/v3_validation/isolated_live_macos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_live_macos.py)｜1744 行｜`df44c6b20852`｜HTTPResponse, HTTPResponse.read, HTTPResponse.__enter__, HTTPResponse.__exit__, HostLaunchAgent, CommandResult, _canonical_v2_root, _canonical_v3_runtime, _canonical_launchagents, _sha256_bytes

- [`scripts/v3_validation/isolated_live_plan_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_live_plan_builder.py)｜247 行｜`6bb60efa4d2f`｜IsolatedLivePlanBlocked, _sha256_bytes, _stable_regular, _binding, _token_digest, _target, _write_exclusive, create_isolated_live_plan, _parser, main

- [`scripts/v3_validation/isolated_resource_window.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_resource_window.py)｜1016 行｜`00d703ed747c`｜IsolatedResourceWindowError, parse_powermetrics_process_gpu, parse_powermetrics_process_gpu.walk, _canonical, sha256_json, _number, _integer, _sha, _verify_identity, _verify_preflight

- [`scripts/v3_validation/isolated_resource_window_collector.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_resource_window_collector.py)｜1943 行｜`f72c730c9c2c`｜CollectorError, Handle, Snapshot, Backend, Backend.now_ns, Backend.sleep, Backend.preflight, Backend.start, Backend.snapshot, Backend.poll

- [`scripts/v3_validation/isolated_resource_window_plan_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/isolated_resource_window_plan_builder.py)｜863 行｜`d00095415257`｜ResourceWindowPlanError, _sha256, _tree_sha256, _regular, _python_runtime_binding, _external_website, _external_file, _json, _deep_release, _exclusive_write

- [`scripts/v3_validation/live_validation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/live_validation.py)｜266 行｜`47d2440c2c28`｜plan_sha256, _timestamp, validate_live_plan, load_live_plan, validate_live_report, validate_live_report_against_plan, validate_live_campaign_reports, load_live_report

- [`scripts/v3_validation/offline_machine_gate_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/offline_machine_gate_builder.py)｜649 行｜`141fae0ad643`｜OfflineMachineGateError, FrozenFile, CandidateBinding, _canonical_bytes, _freeze, _json, _hash_runtime, verify_candidate_runtime, _verify_deploy, _safe_output_directory

- [`scripts/v3_validation/paths.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/paths.py)｜18 行｜`a51d1a770eb7`｜—

- [`scripts/v3_validation/perf_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/perf_certification.py)｜1389 行｜`3d3fa6f9eabf`｜PerformanceCertificationError, _PerfUser, _PerfUser.__init__, _canonical_json, _sha256, _sha256_file, request_plan_sha256, _is_relative_to, _prepare_sandbox, _find_executable

- [`scripts/v3_validation/perf_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/perf_compat.py)｜2020 行｜`b63b27924eb1`｜PerfEvidenceError, _canonical_json, _sha256_bytes, _sha256_file, verify_evidence_hash, _request_plan, _request_plan_sha256, _decoded_case, _expected_body, build_offline_app

- [`scripts/v3_validation/physical_fault_drill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/physical_fault_drill.py)｜1354 行｜`81fa4e2b7a8e`｜PhysicalFaultBlocked, _canonical, _sha, _semantic, _json, _write_new, _context, _time, _selected_device, _same_device_identity

- [`scripts/v3_validation/provisional_resource_window_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/provisional_resource_window_execute.py)｜911 行｜`3b04f30a03ab`｜ResourceWindowMachine, ResourceWindowMachine.capture_resource_window_host_state, ResourceWindowMachine.stop_resource_window_labels, ResourceWindowMachine.collect_resource_window_zero_receipt, ResourceWindowMachine.restore_resource_window_labels, ResourceWindowMachine.verify_resource_window_readiness, ResourceWindowCollectorTimeout, _sha256, _canonical, _semantic_receipt

- [`scripts/v3_validation/provisional_resource_window_macos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/provisional_resource_window_macos.py)｜188 行｜`146cf719dc70`｜_sha256, _bound, _write_new, build_parser, main

- [`scripts/v3_validation/pytest_transcript_plugin.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/pytest_transcript_plugin.py)｜68 行｜`bbf333a72c39`｜_digest, _file_digest, pytest_collection_finish, pytest_runtest_logreport, pytest_sessionfinish

- [`scripts/v3_validation/quality_input_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/quality_input_builder.py)｜434 行｜`e0fed12668ef`｜QualityInputBuildError, _sha256, _canonical, _fsync_directory, _write_new, _new_directory, _absolute_file, _absolute_directory, _inside, _relative_files

- [`scripts/v3_validation/release_quality_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/release_quality_certification.py)｜975 行｜`853018ca8a14`｜ReleaseQualityCertificationError, _formal_mutable_environment, _prepare_campaign_temp_root, _sha256, _load_object, _quoted, _isolated_roots, _live_mutable_read_roots, _seatbelt_profile, _release_files

- [`scripts/v3_validation/release_quality_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/release_quality_evidence.py)｜436 行｜`1ac0b0f683df`｜ReleaseQualityEvidenceError, canonical_bytes, sha256_json, _node_path, _final_outcomes, _selected_release_paths, _evaluate_selection, _verify_test_sources, _verify_flow, _verify_side_effect_snapshot

- [`scripts/v3_validation/resource_performance_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/resource_performance_certification.py)｜923 行｜`25ea0e472758`｜ResourcePerformanceCertificationError, _sha256, _load_object, _release_files, _quoted, _isolated_roots, _seatbelt_profile, _runtime_binding, _isolated_window_report, _g8_smb_report

- [`scripts/v3_validation/resource_performance_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/resource_performance_evidence.py)｜737 行｜`4f676e865a37`｜ResourcePerformanceEvidenceError, canonical_bytes, sha256_json, _finite_nonnegative, verify_g8_transport_composition_receipt, _performance_metrics, _resource_metrics, _preemption_metrics, _preemption_metrics.p95, _worker_metrics

- [`scripts/v3_validation/resource_window_core_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/resource_window_core_adapter.py)｜116 行｜`883be9b8e051`｜_sha256, _manifest_files, _member, main

- [`scripts/v3_validation/resource_window_model_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/resource_window_model_adapter.py)｜187 行｜`a066691c0b2d`｜_tree_sha256, _request, _wait_server, _response_text, _token_count, main

- [`scripts/v3_validation/route_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/route_certification.py)｜1229 行｜`24eaf9a35b4c`｜_canonical, _sha256, _key_dict, _expected_external_storage_roots, _live_magi_root, _live_mutable_read_roots, _seatbelt_profile_bytes, _seatbelt_attestation, _attested_seatbelt_workspace, _write_seatbelt_profile

- [`scripts/v3_validation/route_reviews.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/route_reviews.py)｜133 行｜`e08ca2a4a18b`｜RouteMethodKey, RouteMethodReview, RouteMethodReview.to_dict, load_route_method_reviews, _merge_route_method_review_payload, validate_reviews_against_inventory, require_reviewed_route_method

- [`scripts/v3_validation/route_success_trace_plugin.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/route_success_trace_plugin.py)｜367 行｜`da07c7d51dce`｜_trace_path, _is_within, _is_lexically_within, _external_storage_roots, _install_isolation_guard, _install_isolation_guard.block, _install_isolation_guard.audit, _auth_redirect, _rule_matches, _recording_open

- [`scripts/v3_validation/schedule_body_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schedule_body_registry.py)｜5979 行｜`c66d12c80cd2`｜ScheduleBodyRegistryError, _trusted_dependency_executable, _canonical_json, _sha256, _sha256_file, _cron_policy_source_sha256, _stable_regular_bytes, _bound_cron_bytes, _source_bound_cron_jobs, _is_relative_to

- [`scripts/v3_validation/schedule_capacity_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schedule_capacity_certification.py)｜1597 行｜`41859a5f70ba`｜ScheduleCapacityError, classify_coalescing_safety, Occurrence, Occurrence.key, OfferResult, SameJobCoalescer, SameJobCoalescer.__init__, SameJobCoalescer.offer, SameJobCoalescer.start, SameJobCoalescer.complete

- [`scripts/v3_validation/schedule_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schedule_evidence.py)｜293 行｜`d78c1c093d77`｜ScheduleEvidenceError, enabled_job_ids_from_cron, _job_ids, _string_ids, _sha, derive_schedule_gate_metrics

- [`scripts/v3_validation/schedule_nonstorage_fixture_matrix.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schedule_nonstorage_fixture_matrix.py)｜369 行｜`84ce3e33b93e`｜_contract, adapter_proposals, _news, _judgment, _cortex_input, _autopilot_input, _audit_input, _product_input, populate_nonstorage_fixture

- [`scripts/v3_validation/schedule_product_fixture_matrix.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schedule_product_fixture_matrix.py)｜564 行｜`489c03e3206a`｜_common_contract, adapter_proposals, _business_input, _distill_row, _insight, _insight_content, _insight_input, _reprocess_input, _product_input, populate_product_fixture

- [`scripts/v3_validation/schedule_sample_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schedule_sample_evidence.py)｜428 行｜`2213781594ae`｜canonical_sha256, _collect_hashes, _contract_summary, _dependency_summary, build_sample_evidence, _valid_summary_hash, verify_sample_evidence_ledger

- [`scripts/v3_validation/schema.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schema.py)｜35 行｜`4d807bc51bb4`｜ContractValidationError, load_json, validate_json, validate_json_file

- [`scripts/v3_validation/side_effects.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/side_effects.py)｜50 行｜`10bb1fe169ab`｜SideEffectDecision, evaluate_side_effect

- [`scripts/v3_validation/source_anchor_refresh.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/source_anchor_refresh.py)｜135 行｜`755da46ca80d`｜SourceAnchorError, _source_file, _function_lines, refresh_route_review_sources, refresh_readiness_evidence, _render, _refresh_file, main

- [`scripts/v3_validation/transcription_quality_benchmark.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/transcription_quality_benchmark.py)｜642 行｜`3d9cae0010a2`｜TranscriptionQualityEvidenceError, _exact_keys, _canonical_bytes, sha256_json, _verify_self_hash, _finite_nonnegative, _positive_integer, _nonnegative_integer, _sha, _reject_sensitive_payload

- [`scripts/v3_validation/v3_rotation_drill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/v3_rotation_drill.py)｜1270 行｜`8155e1d30702`｜V3RotationDrillBlocked, ReleaseIdentity, DeploymentBinding, OwnerProcess, _canonical, _digest, _sha256, _load, _time, _signature

- [`scripts/v3_validation/validation_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/validation_router.py)｜776 行｜`d5fe3ad721ff`｜ValidationRouterError, ValidationNode, ValidationPlan, ValidationPlan.blocked, ValidationPlan.pytest_args, _normalise, _normalise_glob, _canonical, _sha256_bytes, _sha256_file

- [`scripts/v3_validation/worker_soak_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/worker_soak_evidence.py)｜69 行｜`5c666d64e668`｜WorkerSoakEvidenceError, summarize_worker_soak_measurements

- [`scripts/weekend_bookmark_batch.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/weekend_bookmark_batch.py)｜2619 行｜`65862c5fb9d9`｜FileScanTimeout, VisionSourceChanged, _is_transient_storage_error, _source_unavailable_is_transient, _failed_result_is_transient_network_source, _file_timeout, _file_timeout.__init__, _file_timeout.__enter__, _file_timeout.__enter__._raise_timeout, _file_timeout.__exit__

- [`scripts/weekend_resummary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/weekend_resummary.py)｜770 行｜`348a37a7df55`｜_signal_handler, _kill_child_processes, _acquire_lock, _release_lock, _load_state, _save_state, _completed_run_evidence, _record_scheduler_success, _nim_daily_budget_exhausted, _budget_deferred_result

- [`scripts/wiki_synthesizer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/wiki_synthesizer.py)｜959 行｜`a76b97840080`｜_resolve_agent_dir, _load_state, _save_state, _get_vault_path, _gather_case_notes, _case_needs_update, _get_gateway, _omlx_chat_direct, _extract_note_text, _synthesize_overview

### skills/（411 檔）

- [`skills/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/apple/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/__init__.py)｜2 行｜`4c7e252fd074`｜—

- [`skills/apple/apple_ai.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/apple_ai.py)｜233 行｜`2cc7f36cb0ff`｜speech_to_text, extract_pdf_text, ocr_image, ocr_screenshot, check_shortcuts

- [`skills/apple/apple_intelligence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/apple_intelligence.py)｜521 行｜`b5fed4817b66`｜_resolve_shortcut_name, _run_shortcuts_list, shortcuts_status, _run_shortcut, _run_shortcut._materialize_input_path, _run_shortcut._set_clipboard_text, _run_shortcut._run, extract_pdf_text_quartz, extract_pdf_text, ocr_image_vision

- [`skills/apple/contacts_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/contacts_bridge.py)｜220 行｜`079c1147feb4`｜_run_osascript, _escape, search_contact, search_contacts, get_contact_count, search_lawyer, format_contact_info

- [`skills/apple/coreml_classifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/coreml_classifier.py)｜292 行｜`4919593bf9e9`｜is_available, DocumentClassifier, DocumentClassifier.__init__, DocumentClassifier.classify, DocumentClassifier._classify_by_keywords, DocumentClassifier.classify_batch, export_training_data, get_classifier, classify_document

- [`skills/apple/eventkit_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/eventkit_bridge.py)｜512 行｜`3de3353441f9`｜_run_osascript, _escape_applescript, ensure_calendar_exists, create_calendar_event, check_event_exists, ensure_reminder_list_exists, create_reminder, create_trial_events, create_case_deadline_reminder, parse_trial_command

- [`skills/apple/natural_language.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/natural_language.py)｜388 行｜`5676aa70dcd6`｜is_available, detect_language, _detect_language_native, _detect_language_heuristic, detect_language_with_confidence, tokenize, _tokenize_native, _tokenize_native._callback, _tokenize_simple, extract_entities

- [`skills/apple/run_shortcut.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/run_shortcut.py)｜44 行｜`97e9a1b28b1f`｜list_shortcuts, run_shortcut

- [`skills/auto-magi-skill/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/auto-magi-skill/action.py)｜14 行｜`64338bab073a`｜main

- [`skills/autoresearch/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/action.py)｜348 行｜`7fa5853a9ca2`｜_ssh, _scp_to, _scp_from, cmd_setup, cmd_run, cmd_status, cmd_results, cmd_stop, main

- [`skills/autoresearch/prepare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/prepare.py)｜389 行｜`4f2ba9cbb8ba`｜download_single_shard, download_data, list_parquet_files, text_iterator, train_tokenizer, Tokenizer, Tokenizer.__init__, Tokenizer.from_directory, Tokenizer.get_vocab_size, Tokenizer.get_bos_token_id

- [`skills/autoresearch/train.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/train.py)｜630 行｜`2954175f4ac4`｜GPTConfig, norm, has_ve, apply_rotary_emb, CausalSelfAttention, CausalSelfAttention.__init__, CausalSelfAttention.forward, MLP, MLP.__init__, MLP.forward

- [`skills/brain_manager/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/brain_manager/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/brain_manager/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/brain_manager/action.py)｜1150 行｜`06961f21ab62`｜_distributed_enabled, _remote_agent_reachable, _normalize_mode, _is_process_running, _wait_until, _write_state, _load_ngl_hint, _save_ngl_hint, get_recommended_ngl, _stop_local_server

- [`skills/bridge/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/bridge/balthasar_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/balthasar_bridge.py)｜951 行｜`444583138807`｜_format_hhmmss, _normalize_time_scale_if_needed, _normalize_segments, _segment_is_uncertain, _segment_quality_summary, _segments_to_timestamp_text, _segments_to_speaker_text, _infer_speaker_from_text, _annotate_speakers, _split_text_units

- [`skills/bridge/casper_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/casper_bridge.py)｜181 行｜`5def07770aca`｜get_latest_session_id, chat, chat._siri_remember

- [`skills/bridge/citation_format.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/citation_format.py)｜174 行｜`c93bf7ebb0cb`｜Citation, ParsedAnswer, _count_words, parse_citations, render_citations_for_telegram, build_citation_system_prompt

- [`skills/bridge/code_analysis.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/code_analysis.py)｜190 行｜`847a13f7a403`｜_resolve_alias, list_files, read_codebase, analyze_code, estimate_effort

- [`skills/bridge/distill_collector.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/distill_collector.py)｜425 行｜`0cd357251e59`｜_paths_for, _load_state, _save_state, _content_hash, _contains_any, _language_stats, _reject_reasons, _passes_quality, _record_reject_reasons, _rotate_if_needed

- [`skills/bridge/embedding_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/embedding_router.py)｜695 行｜`1fdfec7e8e61`｜_definitions_path, _cosine_similarity, _content_hash, EmbeddingRouter, EmbeddingRouter.__init__, EmbeddingRouter.initialize, EmbeddingRouter._check_definitions_changed, EmbeddingRouter.route, EmbeddingRouter.route_top_n, EmbeddingRouter.is_ready

- [`skills/bridge/ensemble_inference.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/ensemble_inference.py)｜1108 行｜`f0a4cfac50e6`｜_load_soul, ConsensusResult, ConsensusResult.__init__, ConsensusResult.to_dict, _call_omlx_chat, _call_omlx_chat_multiturn, _build_system_with_citation, _inject_citation_result, ensemble_chat, ensemble_chat._call_role

- [`skills/bridge/grounded_ai.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/grounded_ai.py)｜1521 行｜`1b83463ee5e1`｜_has_internal_badge_leak, _is_persona_hallucination, _is_garbage_output, _is_parrot_response, _get_tier_anchor_embeddings, _classify_query_tier, _filter_statute_memories, _is_incoherent_response, _embeddings_are_comparable, _has_lexical_topic_overlap

- [`skills/bridge/http_pool.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/http_pool.py)｜44 行｜`f5317c7b77fe`｜get_session

- [`skills/bridge/imgur_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/imgur_bridge.py)｜70 行｜`fd41fb66ea4f`｜upload_image

- [`skills/bridge/inference_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/inference_gateway.py)｜1872 行｜`ae4a6b6b2ee6`｜split_heavy_prefix, _env_bool, _flask_heavy_opt_in, _detect_heavy_opt_in, _is_night, classify_intent, select_model_for_task, InferenceGateway, InferenceGateway.__init__, InferenceGateway.classify_intent

- [`skills/bridge/intention_classifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/intention_classifier.py)｜446 行｜`a02c4d470f35`｜IntentionClassifier, IntentionClassifier.__init__, IntentionClassifier._is_persistable_intent, IntentionClassifier._load_persistent_cache, IntentionClassifier._save_persistent_cache, IntentionClassifier._flush_cache, IntentionClassifier._cache_get, IntentionClassifier._cache_set, IntentionClassifier._check_regex_rules, IntentionClassifier._heuristic_classify

- [`skills/bridge/iron_dome.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/iron_dome.py)｜46 行｜`846875cfcbe2`｜_main

- [`skills/bridge/legal_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/legal_bridge.py)｜69 行｜`870ada283b38`｜execute_skill

- [`skills/bridge/llm_direct.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/llm_direct.py)｜618 行｜`e8a895028289`｜_get_session, _list_openai_models, _resolve_provider_model, _provider_is_remote, _prepare_remote_messages, _call_openai_format, _call_anthropic_format, chat, feature_enabled, translate_with_codex

- [`skills/bridge/melchior_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/melchior_bridge.py)｜364 行｜`6fb9f6dc0833`｜generate_text, encode_image, analyze_image_local, analyze_image, melchior_search, _generate_image_openai, generate_image, check_health, sync_skills

- [`skills/bridge/melchior_client.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/melchior_client.py)｜1783 行｜`6c9ba55b4015`｜_avoid_distributed, _result, _start_deadline, _remaining, _local_fallback_timeout, _post_json, _get_json, _get_omlx_watchdog_state, _omlx_watchdog_blocks_service, _omlx_service_available

- [`skills/bridge/melchior_manager.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/melchior_manager.py)｜373 行｜`6df4d220f245`｜_now_iso, _load_state, _save_state, _should_exclude, _sha256_file, _scan_skills_tree, _compute_delta, _build_zip, melchior_health, _smoke_test_melchior

- [`skills/bridge/nim_heavy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/nim_heavy.py)｜799 行｜`1a7c3d14c353`｜_contains_credentials, background_heavy_authorization, _validate_background_heavy_authorization, _model_allowed, record_nim_outcome, recommend_nim_policy, reset_congestion_window, _today_key, _load_state, _save_state

- [`skills/bridge/openclaw_codex_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/openclaw_codex_bridge.py)｜170 行｜`c311e3c17f32`｜feature_enabled, apply_manual_command, public_status_report, clear_failure_cooldown, is_session_locked, normalize_feature_name, _normalize_feature_name, load_policy, load_runtime_state, save_runtime_state

- [`skills/bridge/semantic_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/semantic_router.py)｜591 行｜`82391aea01e9`｜_definitions_path, _load_skills, _get_skills, _tokenize, _tokenize._flush_cjk, _trigrams, _score, _is_soft_ambiguous_message, route, deprecated_route_hint

- [`skills/bridge/shared_utils/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/shared_utils/__init__.py)｜40 行｜`1cad03ef55be`｜—

- [`skills/bridge/shared_utils/case_number_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/shared_utils/case_number_utils.py)｜75 行｜`ef509e282e30`｜extract_case_number, parse_case_number_flexible, extract_laf_case_number

- [`skills/bridge/shared_utils/court_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/shared_utils/court_utils.py)｜258 行｜`26820827d8d6`｜normalize_court_name, get_court_code, extract_court_name

- [`skills/bridge/shared_utils/judgment_folder_names.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/shared_utils/judgment_folder_names.py)｜3 行｜`1ad37119bf2b`｜—

- [`skills/bridge/shared_utils/text_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/shared_utils/text_utils.py)｜83 行｜`f26988c812f4`｜normalize_spaces, normalize_segment_fragment, clean_text, strip_zero_width, normalize_court_char

- [`skills/bridge/tier_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/tier_router.py)｜40 行｜`07f4b237d510`｜ensure_26b_ready

- [`skills/bridge/tri_sage_collab.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/tri_sage_collab.py)｜923 行｜`e4de2b65e090`｜_safe_name, _ensure_dir, _extract_first_url, _chunk_text, _sample_chunks_evenly, _translate_workers, _bounded_translate_timeout, _strip_translation_preamble, _translate_llm_call, translate_text

- [`skills/bridge/watcher_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/watcher_bridge.py)｜179 行｜`123bfed1bab1`｜_resolve_watcher_host, check_health, get_watcher_status, query_archived_logs, get_anomalies, trigger_pull

- [`skills/bridge/web_research.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/web_research.py)｜5 行｜`d650ebbae0bc`｜—

- [`skills/brief-gen/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/brief-gen/action.py)｜615 行｜`265d50db2e7f`｜_resolve_case_base, _cmd_template, _render_template_detail, _detect_brief_type, _cmd_draft, _cmd_enrich, _cmd_export, _export_docx_direct, _extract_case_number, _find_case_folder

- [`skills/browser/browser_control.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/browser/browser_control.py)｜192 行｜`78807ce84620`｜_is_url_safe, BrowserController, BrowserController.__init__, BrowserController.start, BrowserController.stop, BrowserController.navigate, BrowserController.screenshot, BrowserController.extract_text, BrowserController.fill_form, BrowserController.click_element

- [`skills/casper-autofix-knowledge/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper-autofix-knowledge/action.py)｜50 行｜`8e32eb0a24ad`｜_extract_terms, main

- [`skills/casper-client/casper_llm_proxy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper-client/casper_llm_proxy.py)｜49 行｜`804d15982f0b`｜CasperResponse, CasperGenerativeModel, CasperGenerativeModel.__init__, CasperGenerativeModel.generate_content, CasperGenerativeModel.count_tokens

- [`skills/casper-client/casper_tools_client.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper-client/casper_tools_client.py)｜71 行｜`1581c529449f`｜_post_json, casper_chat, casper_summarize, casper_translate, casper_fetch_url, casper_research

- [`skills/casper/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper/__init__.py)｜1 行｜`bcdc3702ae3c`｜—

- [`skills/casper/code_review.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper/code_review.py)｜226 行｜`d8b8dc0aedbd`｜CodeReviewSkill, CodeReviewSkill.__init__, CodeReviewSkill._get_relevant_context, CodeReviewSkill.review_file, CodeReviewSkill._compute_hash, CodeReviewSkill.run_review

- [`skills/casper/melchior_update.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper/melchior_update.py)｜43 行｜`03cb91e620eb`｜run_update

- [`skills/catalog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/catalog.py)｜99 行｜`7c9a939dea2a`｜is_runtime_skill_dir_name, canonical_skill_dir_name, is_public_skill_dir_name, is_deprecated_skill_dir_name, is_public_definition_tool, iter_top_level_skill_dirs

- [`skills/contract-review/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/contract-review/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/contract-review/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/contract-review/action.py)｜701 行｜`5ad3ecb8afb2`｜_load_text, _truncate, _get_gateway, _llm_json, _sentences, _shorten, _detect_doc_type, _extract_parties, _find_sentence, _extract_obligations

- [`skills/cookie_stl/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/cookie_stl/__init__.py)｜14 行｜`7e36b99bcb0c`｜—

- [`skills/cookie_stl/engine.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/cookie_stl/engine.py)｜1197 行｜`064f765402fb`｜CookieSTLError, _check_deadline, CookieParameters, CookieParameters.validate, _components, _largest_component, _remove_specks, _dilate, _erode, _close

- [`skills/court-hearing-reminder/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/court-hearing-reminder/action.py)｜1273 行｜`ed2006c9e9cb`｜_get_conn, _fetch_upcoming_hearings, _load_remind_state, _is_remind_key_sent, _save_remind_state, _send_reminder, _generate_prep_summary, _is_safe_summary, _normalized_terms, _case_domain

- [`skills/crawler-targets/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/crawler-targets/action.py)｜299 行｜`3190324006da`｜_maybe_reexec_venv, _ok, _load_state, _save_state, _load_jsonish, _norm_url, _validate_url, list_targets, add_target, remove_target

- [`skills/db-dual-sync/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/db-dual-sync/action.py)｜297 行｜`5ffdfc525f7c`｜_ok, _load_jsonish, _run, _status, _sync, _backup, _list_backups, main

- [`skills/doc-producer/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/doc-producer/action.py)｜574 行｜`6eddf7f320dc`｜_find_soffice, convert_docx_to_pdf, _convert_via_libreoffice, _convert_via_docx2pdf, _add_stamp_image_to_last_page, mark_copy_type, merge_pdfs, produce, _self_test, main

- [`skills/documents/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/__init__.py)｜42 行｜`c8d5ca9269aa`｜extract_text, summarize_pdf, get_pdf_info, extract_chapters, summarize_epub, get_epub_info

- [`skills/documents/epub_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/epub_bridge.py)｜181 行｜`19b9998daf31`｜extract_chapters, get_epub_info, summarize_epub

- [`skills/documents/file_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/file_bridge.py)｜194 行｜`cd7285c60c78`｜_chunk_text, _sample_evenly, _read_text, _extract_docx_text, extract_text_from_file, summarize_extracted_text, summarize_file

- [`skills/documents/multimodal_parser.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/multimodal_parser.py)｜749 行｜`ef67ecab53c4`｜ContentType, ParsedBlock, ParsedBlock.to_dict, ParsedBlock.char_count, ParseResult, ParseResult.text_blocks, ParseResult.table_blocks, ParseResult.image_blocks, ParseResult.full_text, ParseResult.structured_text

- [`skills/documents/nas_pdf_ocr_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/nas_pdf_ocr_worker.py)｜646 行｜`a0d3bce01d8e`｜queue_db_path, _load_local_dotenv, _private_path_ref, _ocrmypdf_command_prefix, _ocr_environment, _large_ocr_allowed_now, _worker_exit_code, _ocr_timeout_seconds, acquire_nas_ocr_queue_lock, _empty_worker_counters

- [`skills/documents/pdf_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/pdf_bridge.py)｜2970 行｜`e8595f16d525`｜_doc_run_root, _safe_slug, _atomic_write_text, _atomic_write_json, _read_json, _is_synthetic_timeout_fallback, _summary_text_usable, _script_balance, _needs_crosslingual_polish, _translate_note_to_traditional_chinese

- [`skills/documents/vector_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/documents/vector_pipeline.py)｜258 行｜`b429e05339da`｜_now_iso, _sha1, _doc_key, _load_index, _save_index, _chunk_text, _prepare_embedding_inputs, _dedupe_batch_items, ingest_sections_to_vector_memory, ingest_text_to_vector_memory

- [`skills/docx-editor/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/action.py)｜516 行｜`b34e043a0802`｜cmd_apply, cmd_extract, cmd_extract.collect, cmd_find, cmd_find.collect, cmd_generate, cmd_chat_edit, cmd_self_test, main

- [`skills/docx-editor/examples/chat_edit_example.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/examples/chat_edit_example.py)｜65 行｜`51cc83427081`｜main

- [`skills/docx-editor/examples/generate_example.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/examples/generate_example.py)｜79 行｜`da8b041fcb43`｜main

- [`skills/docx-editor/examples/simple_edit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/examples/simple_edit.py)｜61 行｜`d4faf352e045`｜main

- [`skills/docx-editor/lib/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/__init__.py)｜1 行｜`84b227261cb6`｜—

- [`skills/docx-editor/lib/anchor_matcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/anchor_matcher.py)｜312 行｜`da411e762c7d`｜_pre_normalize, Normalized, Normalized.__init__, normalize_ws, map_norm_range_to_original, _find_unique_in_norm, _find_unique_in_norm.check_ctx, find_unique_anchor, _count_candidates, _count_candidates.check_ctx

- [`skills/docx-editor/lib/docx_io.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/docx_io.py)｜172 行｜`6b144ac37a66`｜_w, read_docx_to_xml, _find_zip_entry, write_xml_to_docx, extract_paragraph_text, find_max_id, get_body, collect_paragraphs

- [`skills/docx-editor/lib/generator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/generator.py)｜166 行｜`bcff6ebf3e32`｜TableSpec, SectionSpec, GenerateDocxRequest, generate_docx, _add_table

- [`skills/docx-editor/lib/llm_edit_planner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/llm_edit_planner.py)｜209 行｜`db4970c201be`｜plan_edits_with_llm, _call_llm, _parse_json_response

- [`skills/docx-editor/lib/run_splitter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/run_splitter.py)｜365 行｜`a1b978888454`｜_w, RunInfo, RunInfo.__init__, FlatParagraph, FlatParagraph.__init__, flatten_paragraph, flatten_paragraph.process_run, _build_run, _rpr_for_pos, rebuild_paragraph_with_edits

- [`skills/docx-editor/lib/tracked_edits.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/lib/tracked_edits.py)｜296 行｜`b9b71202607f`｜EditInput, AppliedChange, EditError, ApplyTrackedEditsResult, _PlannedChange, apply_tracked_edits, _truncate

- [`skills/docx/scripts/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/__init__.py)｜1 行｜`01ba4719c80b`｜—

- [`skills/docx/scripts/accept_changes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/accept_changes.py)｜135 行｜`0c991d2bc730`｜accept_changes, _setup_libreoffice_macro

- [`skills/docx/scripts/comment.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/comment.py)｜318 行｜`f35f599718b6`｜_generate_hex_id, _encode_smart_quotes, _append_xml, _find_para_id, _get_next_rid, _has_relationship, _has_content_type, _ensure_comment_relationships, _ensure_comment_content_types, add_comment

- [`skills/docx/scripts/office/helpers/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/helpers/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/docx/scripts/office/helpers/merge_runs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/helpers/merge_runs.py)｜202 行｜`0d311291e4d6`｜merge_runs, _find_elements, _find_elements.traverse, _get_child, _get_children, _is_adjacent, _remove_elements, _strip_run_rsid_attrs, _merge_runs_in, _first_child_run

- [`skills/docx/scripts/office/helpers/simplify_redlines.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/helpers/simplify_redlines.py)｜200 行｜`8e9ddfee4589`｜simplify_redlines, _merge_tracked_changes_in, _is_element, _get_author, _can_merge_tracked, _merge_tracked_content, _find_elements, _find_elements.traverse, get_tracked_change_authors, _get_authors_from_docx

- [`skills/docx/scripts/office/pack.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/pack.py)｜167 行｜`c7caca12dc58`｜pack, _run_validation, _condense_xml

- [`skills/docx/scripts/office/safe_minidom.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/safe_minidom.py)｜41 行｜`7737c3e5257a`｜UnsafeOfficeXML, _checked_bytes, parseString, parse

- [`skills/docx/scripts/office/soffice.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/soffice.py)｜205 行｜`090ee2772342`｜get_soffice_env, run_soffice, _soffice_binary, _needs_shim, _ensure_shim

- [`skills/docx/scripts/office/unpack.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/unpack.py)｜140 行｜`2d4978a1bd19`｜unpack, _pretty_print_xml, _escape_smart_quotes

- [`skills/docx/scripts/office/validate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/validate.py)｜117 行｜`a3ff56221feb`｜main

- [`skills/docx/scripts/office/validators/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/validators/__init__.py)｜15 行｜`83e0f035c5ab`｜—

- [`skills/docx/scripts/office/validators/base.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/validators/base.py)｜851 行｜`fce7a9494aa5`｜BaseSchemaValidator, BaseSchemaValidator.__init__, BaseSchemaValidator.validate, BaseSchemaValidator.repair, BaseSchemaValidator.repair_whitespace_preservation, BaseSchemaValidator.validate_xml, BaseSchemaValidator.validate_namespaces, BaseSchemaValidator.validate_unique_ids, BaseSchemaValidator.validate_file_references, BaseSchemaValidator.validate_all_relationship_ids

- [`skills/docx/scripts/office/validators/docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/validators/docx.py)｜450 行｜`3aefdc2bc1b1`｜DOCXSchemaValidator, DOCXSchemaValidator.validate, DOCXSchemaValidator.validate_whitespace_preservation, DOCXSchemaValidator.validate_deletions, DOCXSchemaValidator.count_paragraphs_in_unpacked, DOCXSchemaValidator.count_paragraphs_in_original, DOCXSchemaValidator.validate_insertions, DOCXSchemaValidator.compare_paragraph_counts, DOCXSchemaValidator._parse_id_value, DOCXSchemaValidator.validate_id_constraints

- [`skills/docx/scripts/office/validators/pptx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/validators/pptx.py)｜275 行｜`f937961e62a5`｜PPTXSchemaValidator, PPTXSchemaValidator.validate, PPTXSchemaValidator.validate_uuid_ids, PPTXSchemaValidator._looks_like_uuid, PPTXSchemaValidator.validate_slide_layout_ids, PPTXSchemaValidator.validate_no_duplicate_slide_layouts, PPTXSchemaValidator.validate_notes_slide_references

- [`skills/docx/scripts/office/validators/redlining.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/validators/redlining.py)｜248 行｜`97abb243543f`｜RedliningValidator, RedliningValidator.__init__, RedliningValidator.repair, RedliningValidator.validate, RedliningValidator._generate_detailed_diff, RedliningValidator._get_git_word_diff, RedliningValidator._remove_author_tracked_changes, RedliningValidator._extract_text_content

- [`skills/docx/template_fill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/template_fill.py)｜224 行｜`a63141991849`｜_roc_date, _fetch_case_data, fill_template, _merge_split_placeholders, _merge_split_placeholders._clean_match, _xml_escape, _repack_docx, main

- [`skills/engine/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/__init__.py)｜1 行｜`e6281d68004a`｜—

- [`skills/engine/apple_translation/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/apple_translation/__init__.py)｜305 行｜`c249c7d9aa51`｜normalize_lang, is_available, _build_sidecar_if_needed, translate

- [`skills/engine/chinese_nlp.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/chinese_nlp.py)｜320 行｜`6227781fd8df`｜_acquire_global_slot, _release_global_slot, _looks_chinese, _FallbackSegmenter, _FallbackSegmenter.cut, _FallbackSegmenter.cut_many, _AppleSegmenter, _AppleSegmenter.__init__, _AppleSegmenter.cut, _AppleSegmenter.cut_many

- [`skills/engine/chinese_nlp_sidecar.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/chinese_nlp_sidecar.py)｜39 行｜`41f60d3f1100`｜main

- [`skills/engine/doc_type_detector.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/doc_type_detector.py)｜154 行｜`c2d32ad2fff3`｜DocTypeResult, DocTypeResult.__init__, DocTypeResult.__repr__, _detect_by_regex, _detect_by_vision, detect_doc_type

- [`skills/engine/document_reader.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/document_reader.py)｜327 行｜`b4cfe011ed87`｜DocumentResult, _strip_markers, text_quality_score, _is_meaningful, _markdown_to_plain, _get_markitdown, _pdf_ocr_fallback, _file_bridge_fallback, read_document

- [`skills/engine/error_classifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/error_classifier.py)｜215 行｜`5e4877efd6da`｜FailoverReason, ClassifiedError, ClassifiedError.__str__, classify_error

- [`skills/engine/feedback_loop.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/feedback_loop.py)｜178 行｜`edb105916a09`｜RoutingFeedback, RoutingFeedback.__init__, RoutingFeedback._ensure_loaded, RoutingFeedback._save, RoutingFeedback.record, RoutingFeedback.get_skill_accuracy, RoutingFeedback.compute_threshold_adjustments, ImplicitFeedbackDetector, ImplicitFeedbackDetector.detect, record_feedback

- [`skills/engine/knowledge_extractor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_extractor.py)｜199 行｜`819d2e5ff895`｜should_extract, extract_and_store, extract_and_store._do_extract, _update_stats, get_stats, MemoryManager, MemoryManager.decay_old_memories, MemoryManager.get_memory_stats

- [`skills/engine/knowledge_graph/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_graph/__init__.py)｜13 行｜`17ab0a15d539`｜—

- [`skills/engine/knowledge_graph/community_detector.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_graph/community_detector.py)｜34 行｜`503a9c1122c1`｜detect_communities

- [`skills/engine/knowledge_graph/entity_extractor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_graph/entity_extractor.py)｜48 行｜`9589033bba1b`｜extract_entities, extract_entities._push

- [`skills/engine/knowledge_graph/graph_rag.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_graph/graph_rag.py)｜41 行｜`36fa8ebc00f1`｜_default_graph_path, graph_context

- [`skills/engine/knowledge_graph/graph_store.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_graph/graph_store.py)｜113 行｜`647a3ba6070c`｜GraphStore, GraphStore._cache_max_entries, GraphStore.__init__, GraphStore.upsert_node, GraphStore.add_edge, GraphStore.neighbors, GraphStore.find_nodes, GraphStore.save, GraphStore.load, GraphStore.cache_stats

- [`skills/engine/knowledge_graph/relation_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/knowledge_graph/relation_builder.py)｜30 行｜`1518e1bc4185`｜build_relations

- [`skills/engine/legal_web_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/legal_web_adapter.py)｜196 行｜`e99344345fca`｜_truthy, _normalize_engine, _env_name, resolve_legal_web_engine, legal_web_allowed_hosts, preinstalled_selenium_driver_kwargs, format_legal_web_engine_log

- [`skills/engine/ocr/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/__init__.py)｜35 行｜`52d2bc6fc9d3`｜—

- [`skills/engine/ocr/apple_vision_provider.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/apple_vision_provider.py)｜237 行｜`f7d82bbbc2c4`｜_env_bool, check_available, _functional_probe, _functional_probe._do_probe, _call_apple_vision, reset_probe_cache, run, run._do_ocr

- [`skills/engine/ocr/cache.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/cache.py)｜327 行｜`a8e28b9b62f6`｜_env_bool, _env_int, _env_float, _store_text_enabled, _sha256_text, _redact_sensitive_text_fields, _chmod_private, _cache_dir, _image_hash, get

- [`skills/engine/ocr/chandra_provider.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/chandra_provider.py)｜273 行｜`45556c5e6b29`｜ChandraProbe, ChandraProbe.to_dict, ChandraOCRResult, _env_truthy, enabled, private_deployment_acknowledged, model_license_accepted, qwen_backend_accepted, method, api_base

- [`skills/engine/ocr/consensus.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/consensus.py)｜428 行｜`bac0970bc4a6`｜_env_float, _env_bool, _compute_confidence, _parse_roc_date_to_days, _check_date_conflict, _select_text, _select_best_text, run_consensus, run_consensus._run_tess, run_consensus._run_vision

- [`skills/engine/ocr/legal_corrector.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/legal_corrector.py)｜173 行｜`086bf9df5e05`｜_fullwidth_to_halfwidth, CorrectionResult, correct_legal_text

- [`skills/engine/ocr/legal_entities.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/legal_entities.py)｜129 行｜`ead2b9e56c99`｜extract_case_number, extract_laf_case_number, extract_court_name, extract_entities, extract_all_case_numbers

- [`skills/engine/ocr/nemotron_mlx/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_mlx/__init__.py)｜6 行｜`c4bab3cf0763`｜—

- [`skills/engine/ocr/nemotron_mlx/config.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_mlx/config.py)｜52 行｜`67130a30d243`｜NemotronParseConfig, NemotronParseConfig.from_hf_dir

- [`skills/engine/ocr/nemotron_mlx/image_processor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_mlx/image_processor.py)｜121 行｜`70333a0f97fa`｜make_test_image, _resize_with_aspect_ratio, preprocess_image, load_golden, self_test, main

- [`skills/engine/ocr/nemotron_mlx/radio_encoder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_mlx/radio_encoder.py)｜213 行｜`15732a5db639`｜_linear, _layer_norm, _gelu, _free_memory_mb, _patches_from_nchw, _pos_embed_for_input, RadioEncoder, RadioEncoder.__init__, RadioEncoder.load, RadioEncoder._radio_backbone

- [`skills/engine/ocr/nemotron_mlx/runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_mlx/runtime.py)｜293 行｜`192b8f2ae2af`｜_split_heads, _merge_heads, _causal_self_attention, NemotronRuntime, NemotronRuntime.__init__, NemotronRuntime.load, NemotronRuntime.encode, NemotronRuntime._decoder_layer, NemotronRuntime.decoder_hidden, NemotronRuntime.logits

- [`skills/engine/ocr/nemotron_mlx/weight_map.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_mlx/weight_map.py)｜96 行｜`9c69d4fc5ec2`｜map_tensor_name, conv_tensor_names, transpose_conv_weight, expected_tensor_count

- [`skills/engine/ocr/nemotron_parse_provider.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/nemotron_parse_provider.py)｜66 行｜`9c9cf183980f`｜_enabled, run

- [`skills/engine/ocr/ocr_schema.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/ocr_schema.py)｜140 行｜`4133b9d2d093`｜OCREntities, OCREntities.to_counts, OCRProviderResult, OCRProviderResult.to_dict, OCRProviderResult.failure, OCRConsensusResult, OCRConsensusResult.to_dict, OCRConsensusResult.failure

- [`skills/engine/ocr/opendataloader_provider.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/opendataloader_provider.py)｜217 行｜`d26a84b5d510`｜_cache_max_entries, _cache_get, _cache_put, _enabled, _hybrid_mode, _max_chars, _collect_json_text, _read_output_text, _page_key, _materialize_page_subset

- [`skills/engine/ocr/preprocess.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/preprocess.py)｜237 行｜`eb1c7f2a8293`｜OCRPreprocessResult, _env_bool, _env_float, _env_int, _otsu_threshold, _projection_score, estimate_skew_angle, preprocess_image

- [`skills/engine/ocr/quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/quality.py)｜137 行｜`27d30181f561`｜ScanQualityAssessment, assess_page_scan_quality, compute_quality_score, is_likely_legal_text, score_pair

- [`skills/engine/ocr/rapidocr_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/rapidocr_worker.py)｜39 行｜`c8c68fe5d3cf`｜run_payload, main

- [`skills/engine/ocr/shared_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/shared_runtime.py)｜239 行｜`553ba4c0d8a3`｜_bounded_thread_count, _legacy_rapid_result, _LockedRapidOCR, _LockedRapidOCR.__init__, _LockedRapidOCR.__call__, _LockedDdddOCR, _LockedDdddOCR.__init__, _LockedDdddOCR.classification, _LockedDdddOCR.__getattr__, _build_capped_legacy_engine

- [`skills/engine/ocr/tesseract_provider.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/ocr/tesseract_provider.py)｜338 行｜`a15f3dd353a6`｜_env_bool, _env_str, _env_int, _probe_binary, _probe_langs, _probe_functional, check_available, reset_probe_cache, _run_tesseract_image, _ocr_noise_score

- [`skills/engine/pii_scrubber.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/pii_scrubber.py)｜408 行｜`a240b1ca9ee0`｜ScrubResult, ScrubResult.restore, ScrubResult.certificate, PIIScrubber, PIIScrubber.__init__, PIIScrubber.scrub, PIIScrubber.scrub.scrub_pattern, PIIScrubber.detect_residuals, PIIScrubber._pattern_has_unmasked_match, PIIScrubber._placeholder

- [`skills/engine/playwright_wrapper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/playwright_wrapper.py)｜1493 行｜`e02d690a3db3`｜NavigationPolicyError, _ByShim, Keys, Select, Select.__init__, Select.select_by_visible_text, Select.select_by_value, _ECShim, _ECShim.presence_of_element_located, _ECShim.presence_of_element_located._cond

- [`skills/engine/react_engine.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/react_engine.py)｜745 行｜`aa6cd4b5fb13`｜ReActEngine, ReActEngine.__init__, ReActEngine._default_llm, ReActEngine._format_tool_list, ReActEngine._build_system_prompt, ReActEngine.for_omlx, ReActEngine.for_omlx._split_messages_for_nim, ReActEngine.for_omlx._heavy_llm, ReActEngine.for_omlx._omlx_llm, ReActEngine._extract_balanced_json

- [`skills/engine/realtime_data_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/realtime_data_gateway.py)｜705 行｜`0c2f65cebd4d`｜_looks_like_realtime_action_request, _has_stock_topic, _has_fx_topic, _is_non_realtime_lookup_context, classify_realtime_query, detect_realtime_topics, _extract_location, _extract_global_location, _query_open_meteo, _query_cwa_api

- [`skills/engine/scraping_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/scraping_adapter.py)｜177 行｜`fe3dbf21b9e5`｜scrapling_enabled, _extract_text_from_html, _extract_json_payload, fetch_page, fetch_json

- [`skills/engine/tool_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/tool_registry.py)｜894 行｜`f28610ce49cd`｜_tools_api_url, _internal_api_headers, _search_memory, _remember, _web_search, _realtime_lookup, _normalize_case_query, _query_case_statistics, _query_cases, _summarize_text

- [`skills/engine/trajectory_compressor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/trajectory_compressor.py)｜293 行｜`0a38ffb391d1`｜TrajectoryCompressor, TrajectoryCompressor.__init__, TrajectoryCompressor._estimate_tokens, TrajectoryCompressor._total_tokens, TrajectoryCompressor._is_milestone, TrajectoryCompressor._prune_tool_result, TrajectoryCompressor.prune_tool_results, TrajectoryCompressor._split_head_middle_tail, TrajectoryCompressor._summarize_middle_heuristic, TrajectoryCompressor.compress_for_react

- [`skills/engine/user_insights.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/user_insights.py)｜62 行｜`5ecc383dd81a`｜UserInsightsEngine, UserInsightsEngine.__init__, UserInsightsEngine._load_events, UserInsightsEngine.extract_insights, UserInsightsEngine.get_personalization_context

- [`skills/evidence-admissibility/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evidence-admissibility/action.py)｜367 行｜`13eb104b7b58`｜_get_db_conn, _query_cases, _resolve_case_folder, _scan_index_files, cmd_help, cmd_rules, cmd_lookup, cmd_classify, main

- [`skills/evidence-admissibility/scripts/build_evidence_xlsx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evidence-admissibility/scripts/build_evidence_xlsx.py)｜383 行｜`3e3604be34a3`｜classify_record_type, get_relationship, is_investigation_stage, get_admissibility, extract_source

- [`skills/evolution/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/__init__.py)｜9 行｜`4d36f5e3497f`｜—

- [`skills/evolution/intent_forge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/intent_forge.py)｜271 行｜`3d52a8dd5174`｜_ensure_pending_store, _load_pending, _save_pending, get_pending_issue, clear_pending_issue, _set_pending_issue, _extract_error_text, _question_from_error, _build_summary, forge_execute

- [`skills/evolution/skill_genesis.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/skill_genesis.py)｜3166 行｜`f86fbfa2af49`｜_env_bool, _skill_runtime_env_opt_in, _skill_runtime_default, _skill_auto_pip_enabled, MockID, MockID.list_patterns, MockID.add_pattern, MockID.auto_harden_scope, MockID.sanitize_input, MockID.is_safe

- [`skills/evolution/skill_improver.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/skill_improver.py)｜23 行｜`6df18f30cac2`｜build_improvement_plan

- [`skills/evolution/skill_scorer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/skill_scorer.py)｜25 行｜`da22d8736905`｜score_skill_run

- [`skills/evolution/usage_tracker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/usage_tracker.py)｜89 行｜`85c34cf505c5`｜UsageTracker, UsageTracker.__init__, UsageTracker.record, UsageTracker._load_rows, UsageTracker.summarize, UsageTracker.daily_report

- [`skills/file-review-orchestrator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/file-review-orchestrator/action.py)｜9753 行｜`f7ad7a37f7de`｜_default_download_folder, _acquire_file_review_portal_lock, _portal_deferred_result, _portal_serialized, _portal_serialized.decorator, _portal_serialized.decorator.wrapped, _lower_background_priority, _flow_slug, _safe_create_flow_mirror, _safe_flow_step_status

- [`skills/forensic-transcript-verifier/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/action.py)｜328 行｜`a22149f0a938`｜_audit_projection, _parse_task, _merge_cli, _sha256_file, _write_v3_completion_binding, execute, main

- [`skills/forensic-transcript-verifier/scripts/audit_engine.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/scripts/audit_engine.py)｜890 行｜`c32fd5bb5656`｜Turn, clock_to_seconds, seconds_to_clock, _run, sha256_file, _docx_xml_text, _docx_xml_text.collect, extract_text, _compact_whitespace, _strip_outer_quote

- [`skills/forensic-transcript-verifier/scripts/live_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/scripts/live_runtime.py)｜538 行｜`0ba3568a56ff`｜_atomic_json, _read_json, _read_json_list, _pid_alive, _process_identity, _identity_matches, _terminate_owned_worker, _manifest_fingerprint, _resolve_manifest, _prepare_task

- [`skills/forensic-transcript-verifier/scripts/video_agent.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/scripts/video_agent.py)｜1637 行｜`5983adb768fc`｜_json_write, _run, _json_object, _model_text, _is_independent_provider, _baseline_context, _locked_turn_indices, _closest_turn_index, _point, prepare_autonomous_video_review

- [`skills/forensic-transcript-verifier/scripts/write_transcript_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/scripts/write_transcript_docx.py)｜218 行｜`e0ea87473edd`｜clean, set_run_font, add_run, set_cell_shading, set_cell_text, add_page_number, write_document, main

- [`skills/gmail-drafts/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/gmail-drafts/action.py)｜293 行｜`da74a49d6ecd`｜_scope_list_from_env, _maybe_reexec_venv, _ok, _load_jsonish, _eventlog, _queue_local_draft, _build_gmail_service, create_draft, main

- [`skills/hearing/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/hearing/__init__.py)｜1 行｜`44c67ae8d3ac`｜—

- [`skills/hearing/balthasar_local.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/hearing/balthasar_local.py)｜165 行｜`938226335147`｜_get_mlx_whisper, _normalize_segments, transcribe_audio

- [`skills/insight-refine/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/insight-refine/action.py)｜109 行｜`4c834497cac8`｜_load_jsonish, _ok, _build_prompt, main

- [`skills/interpreter-empirical-classifier/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/interpreter-empirical-classifier/action.py)｜488 行｜`fdaf25323eb2`｜_load_classifier, _load_judicial_search, _emit, _parse_task, _to_bool, _to_int, _to_float, _split_values, _safe_name, _derive_query

- [`skills/iron-dome/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron-dome/action.py)｜162 行｜`ccc54445882a`｜cmd_scan, cmd_list, cmd_add, cmd_sync, cmd_self_test, main

- [`skills/iron-dome/core.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron-dome/core.py)｜804 行｜`0dde8e18f7f3`｜_compile_regexes, _compile_regexes._filter_valid, _load_dynamic_state, _reload_patterns, IronDomeViolation, IronDomeViolation.__init__, sanitize_input, is_safe, get_all_patterns, audit_supply_chain

- [`skills/iron-dome/protocol_override.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron-dome/protocol_override.py)｜218 行｜`43b9a744488a`｜_load_pending, _save_pending, clear_override, _write_override_file, request_override, approve_override

- [`skills/iron-dome/sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron-dome/sync.py)｜313 行｜`315def0533cc`｜get_all_patterns, _env_str, _env_int, _tailscale_ip, _advertise_ip, _node_ip_or, get_patterns_hash, export_patterns, broadcast_update, receive_update_notification

- [`skills/iron_dome/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron_dome/__init__.py)｜8 行｜`0e00e42d9929`｜—

- [`skills/iron_dome/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron_dome/action.py)｜22 行｜`94a9615fa489`｜main

- [`skills/iron_dome/core.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron_dome/core.py)｜21 行｜`e62a039db19a`｜—

- [`skills/iron_dome/protocol_override.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron_dome/protocol_override.py)｜20 行｜`ffb22861aa73`｜—

- [`skills/iron_dome/sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron_dome/sync.py)｜21 行｜`4f870fb01749`｜—

- [`skills/judgment-collector/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judgment-collector/action.py)｜6094 行｜`e9af5ed5dcfb`｜_ensure_cache_root, _cleanup_old_cache_runs, _judgments_read_path, _summary_is_prompt_echo, _summary_is_bad_storage_value, _safe_summary_for_storage, _upsert_judgments_json, _env, _wfgy_enabled, _apply_wfgy

- [`skills/judgment_collector_module.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judgment_collector_module.py)｜14 行｜`687371157d30`｜—

- [`skills/judicial-flow-search-archive/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-flow-search-archive/action.py)｜1219 行｜`b8206fa40083`｜_ok, _load_jsonish, _safe_filename, _short, _heuristic_boolify, _casper_boolify, _looks_like_google_query, _is_keyword_query, _google_to_fjud, _google_to_fjud._sub

- [`skills/judicial-tools/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/action.py)｜357 行｜`5e9bcf7d4633`｜dispatch, _delegate_labor, print_help, parse_task, main

- [`skills/judicial-tools/calculators/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/__init__.py)｜12 行｜`c438573fbc8a`｜—

- [`skills/judicial-tools/calculators/appeal_period.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/appeal_period.py)｜250 行｜`d6e3f8b9be0d`｜_serve_addition, calc_appeal_period

- [`skills/judicial-tools/calculators/co_owner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/co_owner.py)｜133 行｜`d857aa567931`｜calc_co_owner_share, _lcm

- [`skills/judicial-tools/calculators/date_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/date_utils.py)｜187 行｜`bf5714ae4578`｜roc_to_date, date_to_roc, date_to_roc_display, _tw_holidays, is_weekend, is_holiday, next_business_day, is_weekend, is_holiday, next_business_day

- [`skills/judicial-tools/calculators/depreciation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/depreciation.py)｜282 行｜`db5f4efbd998`｜calc_depreciation, _round2, _round0, _straight_line, _declining_balance

- [`skills/judicial-tools/calculators/elapsed_time.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/elapsed_time.py)｜113 行｜`ccb692dce6d7`｜calc_elapsed_time

- [`skills/judicial-tools/calculators/hoffman.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/hoffman.py)｜159 行｜`8bd118e0db81`｜calc_hoffman, _round2, _round6

- [`skills/judicial-tools/calculators/inheritance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/inheritance.py)｜352 行｜`dfa7b225761a`｜_is_spouse, _get_order, _is_predeceased, calc_inheritance, _build_chart

- [`skills/judicial-tools/calculators/interest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/interest.py)｜259 行｜`1c3d64fd454f`｜calc_interest, _calc_monthly_mode, _parse_date, _round2

- [`skills/judicial-tools/calculators/judicial_fee.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/judicial_fee.py)｜236 行｜`e873f4ff29a0`｜_calc_bracket_fee, calc_judicial_fee_new, calc_judicial_fee_old, calc_judicial_fee

- [`skills/judicial-tools/calculators/land_division.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/land_division.py)｜142 行｜`4aaf123d4151`｜calc_land_division

- [`skills/judicial-tools/calculators/land_merge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/land_merge.py)｜246 行｜`6dad1f2dcde8`｜calc_land_merge, calc_land_partial_share, calc_land_partial_share.group_ratio

- [`skills/judicial-tools/calculators/sentence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/sentence.py)｜261 行｜`4ec6f4f1f2b3`｜_ym_to_months, _months_to_ym, _half_up, _aggravate_once, _mitigate_once, calc_sentence

- [`skills/judicial-tools/calculators/unjust_enrichment.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/calculators/unjust_enrichment.py)｜193 行｜`deaad41a0008`｜_parse_date, _days_in_year, _get_land_value, calc_unjust_enrichment

- [`skills/judicial-web-search/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-web-search/action.py)｜1206 行｜`4f26ff3528df`｜_preview_limit, _search_page_limit, _ok, _load_jsonish, _run_venv, _launch, _prefer_http_fetch, _verify_ssl, _requests_session, _extract_hidden_fields

- [`skills/judicial-web-search/scripts/judicial_web_search.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-web-search/scripts/judicial_web_search.py)｜246 行｜`9d504357fdae`｜_ok, _load_jsonish, _clean_text, _launch, search, fetch_text, self_test, main

- [`skills/labor-law-calculator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/labor-law-calculator/action.py)｜1337 行｜`49814bd2d8ba`｜WageComponents, WageComponents.total, WageComponents.breakdown_str, _components_from_wage, OvertimeResult, AnnualLeaveResult, SeveranceResult, _round2, _round0, _min_hourly

- [`skills/laf-orchestrator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-orchestrator/action.py)｜670 行｜`e7578c94c0c3`｜_candidate_pythons, _choose_runtime_python, _run_orchestrator, _probe_orchestrator_db, _retry_error_label, _retry_user_reason, _notify_retrying_after_failure, task_self_test, task_preview_counts, task_portal_action

- [`skills/laf-portal-automation/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-portal-automation/action.py)｜212 行｜`0ebc39f397a9`｜_load, _norm, _score, resolve, list_entries, extract_open_case_date, execute_workflow, main

- [`skills/laf-portal-automation/open_case_vision.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-portal-automation/open_case_vision.py)｜24 行｜`ad19cb7a3f77`｜—

- [`skills/laf-portal-automation/simulated_line.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-portal-automation/simulated_line.py)｜27 行｜`7f6c94009202`｜—

- [`skills/laf-refine-case/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-refine-case/action.py)｜139 行｜`c6906343e7d8`｜_load_jsonish, _ok, _build_prompt, refine, main

- [`skills/laf-withdrawal-report/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-withdrawal-report/__init__.py)｜2 行｜`b0330de976ee`｜—

- [`skills/laf-withdrawal-report/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-withdrawal-report/action.py)｜173 行｜`d9f1d505fc83`｜_print, _parse_task, _run, main

- [`skills/law_firm/crawler_architect.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/law_firm/crawler_architect.py)｜191 行｜`029eff3fee8c`｜CrawlerArchitect, CrawlerArchitect.__init__, CrawlerArchitect.create_backup, CrawlerArchitect.restore_backup, CrawlerArchitect.generate_crawler_code, CrawlerArchitect._validate_syntax, CrawlerArchitect.inject_code, CrawlerArchitect.execute_modification

- [`skills/law_firm/legal_crawler_wrapper.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/law_firm/legal_crawler_wrapper.py)｜899 行｜`1bfd6df1829b`｜_tools_api_default, _child_python, _post_tools_skill, _load_state, _save_state, _detect_pattern, _cooldown_remaining, _set_cooldown, _clear_cooldown, _truthy

- [`skills/law_firm/manage_clients.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/law_firm/manage_clients.py)｜172 行｜`c9559e67c36c`｜log_audit, query_clients, add_client, update_client, soft_delete_client

- [`skills/law_firm/manage_meetings.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/law_firm/manage_meetings.py)｜187 行｜`614d9be17ea0`｜_get_conn, log_audit, book_meeting, list_meetings

- [`skills/law_firm/store_crawler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/law_firm/store_crawler.py)｜58 行｜`e7fe67e03542`｜store_crawler_data

- [`skills/law_review/tw_legal_review.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/law_review/tw_legal_review.py)｜167 行｜`dff4d95b36a4`｜review_legal_text, review_legal_text._extract_reply, review_distributed_output

- [`skills/legal/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal/__init__.py)｜4 行｜`84f7dc54f505`｜—

- [`skills/legal/doc_analysis.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal/doc_analysis.py)｜111 行｜`eb61f27bc83c`｜analyze_document_content

- [`skills/legal/judicial.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal/judicial.py)｜4280 行｜`237526ad590d`｜_prepare_judicial_hosted_prompt, _clean_transcript_parse_value, _valid_transcript_record_date, _valid_transcript_record_type, _record_parse_ready_for_filename, CourtCase, FileReviewInfo, CourtMapping, CourtMapping.get_court_code, CourtMapping.get_simple_court

- [`skills/legal/laf.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal/laf.py)｜6899 行｜`4343ea0326eb`｜_laf_content_info, _laf_destination_for_content, _laf_default_case_lawyer, _get_public_base_url, _export_file_to_static, _laf_target_subfolder_for_attachment, _is_laf_staff_email_case_info, _classify_progress_email, _safe_remove, _safe_move

- [`skills/legal/runner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal/runner.py)｜168 行｜`fa8094700da7`｜load_config, run_judicial_task, run_laf_task, diagnose_and_heal, main

- [`skills/legal_attest/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/action.py)｜183 行｜`b58ffa30a566`｜_get_core, _load_state, _save_state, handle_chat, main

- [`skills/legal_attest/generator/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/generator/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/legal_attest/generator/constants.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/generator/constants.py)｜55 行｜`a14f928af96f`｜—

- [`skills/legal_attest/generator/core.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/generator/core.py)｜193 行｜`7ccff665bff1`｜read_main_article, merge_text_and_letter, clean_temp_files, generate_text_and_letter, _is_only_one_name_or_address, _fill_name_address_on_1st_page, _parse_main_article, _get_new_line_coordinate, _reset_coordinates_and_counters, _draw_info_box

- [`skills/legal_attest/generator/gui.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/generator/gui.py)｜241 行｜`f687ecf79445`｜GUI, GUI.__init__, GUI.__init_var, GUI.__do_work, GUI.__change_widgets_state, GUI.__open_old_file, GUI.__save_current_file, GUI.__save_to_new_file, GUI.__do_save, GUI.__save_info_if_zero

- [`skills/legal_attest/generator/pdfpage.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/generator/pdfpage.py)｜62 行｜`7a0f8f73ea63`｜PDFPagePick, PDFPagePick.__init__, PDFPagePick.pick_individual_pages, PDFPagePick.insert_blank_page, PDFPagePick.save, PDFPagePick.__check_page_num, PDFPageMerge, PDFPageMerge.__init__, PDFPageMerge.merge_src_page_to_dest_page, PDFPageMerge.get_src_total_page

- [`skills/legal_attest/generator/pdfpainter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/generator/pdfpainter.py)｜44 行｜`f7b8c59c859a`｜PDFPainter, PDFPainter.__init__, PDFPainter.set_font, PDFPainter.draw_string, PDFPainter.draw_line, PDFPainter.draw_rect, PDFPainter.end_this_page, PDFPainter.save

- [`skills/magi-autopilot/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi-autopilot/action.py)｜5977 行｜`4459d804a790`｜_load_runtime_env, _resolve_remote_db_endpoint, _set_db_preference_by_reachability, _load_mariadb_profiles, _db_schema_chk_nb_guard, _remember_run_event, _remember_step_events, _remember_ngl_calibration_event, _maybe_reexec_venv, _now_tag

- [`skills/magi-doctor/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi-doctor/action.py)｜690 行｜`1697989253e2`｜check_skill_imports, check_dependencies, _run, _http_get, _probe_omlx_chat, _probe_local_llm_inference, _tcp_connect, _load_db_profile, _resolve_omlx_model, _ping

- [`skills/magi-self-repair/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi-self-repair/action.py)｜121 行｜`8def8a315a89`｜_load_doctor_module, _load_guardian_module, _normalize_targets, run_guardian, repair_targets, main

- [`skills/magi/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/magi/council_approval.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/council_approval.py)｜377 行｜`28ec09f271b4`｜_now_iso, _ensure_file, _load, _save, _short, _issue_key, _risk_profile, _new_approval_id, is_core_change, queue_core_change_for_approval

- [`skills/magi/council_executor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/council_executor.py)｜417 行｜`ce0a5b3f9454`｜_is_safe_path, _safe_patch_content, _compile_check, _extract_python_block, _backup_file, _restore_backup, generate_patch, apply_patches, _rollback_all, execute_approved_change

- [`skills/magi/local_council.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/local_council.py)｜389 行｜`e3292dd4a9b9`｜_warmup_model, _ollama_chat, _post_discord, convene_council, convene_council._extract_vote, format_council_result

- [`skills/magi/night_talk.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/night_talk.py)｜664 行｜`180edd9eb39b`｜wait_for_casper, get_casper_thought, _compact_alert_text, _notify_pending_core_change, _to_yes, _enabled_cron_jobs, _managed_backlog_note, _vote_casper_safety, _vote_melchior_engineering, _vote_balthasar_ux

- [`skills/magi/skill_acquire.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/skill_acquire.py)｜230 行｜`690ccd83d963`｜_run, search_clawhub, _iron_dome_scan_file, _iron_dome_scan_dir, acquire_skill, format_search_result

- [`skills/magi/skill_learner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/skill_learner.py)｜753 行｜`a280ce155f8a`｜SkillMeta, SkillMeta.to_dict, _ensure_skills_dir, _parse_frontmatter, _build_frontmatter, _skill_path, list_skills, get_skill, save_skill, patch_skill

- [`skills/magi/sunrise.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi/sunrise.py)｜74 行｜`7ae5eb96aa51`｜execute_sunrise_protocol

- [`skills/management/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/management/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/management/auto_skill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/management/auto_skill.py)｜1417 行｜`9ce73dcb1913`｜_slugify, _uniq_keep_order, AutoSkill, AutoSkill.__init__, AutoSkill._ensure_kb, AutoSkill._load_kb, AutoSkill._save_kb, AutoSkill._load_code_index, AutoSkill._save_code_index, AutoSkill._extract_keywords

- [`skills/management/code_autofix.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/management/code_autofix.py)｜391 行｜`6cb6ee1ba59a`｜_omlx_chat_url, _extract_python_block, _resolve_target, _iter_python_files, _compile_check, _safe_patch, _build_fix_prompt, _llm_repair_code, _create_backup, _verify_tree_compile

- [`skills/management/issue_tracker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/management/issue_tracker.py)｜165 行｜`1d5a5c6f2f71`｜_scrub, _dedup_key, _is_duplicate, log_issue, _append_markdown_legacy

- [`skills/management/skill_interview.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/management/skill_interview.py)｜588 行｜`7e1ffc0dd71e`｜_clean_request, _split_items, _derive_slug, _ensure_unique_slug, infer_skill_defaults, _normalise_answers, _build_description, _to_bullets, _build_skill_md, _build_action_py

- [`skills/market-briefing/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/action.py)｜528 行｜`a7acfc1fb15b`｜_tz_now, _skill_python, _notify_log, _cmd_backtest, _cmd_sector, _cmd_comps, _cmd_export, _format_committee_reasoning, _predict_one, _predict_one._committee_cb

- [`skills/market-briefing/agents/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/market-briefing/agents/base.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/base.py)｜78 行｜`e775ef522ac6`｜BaseAgent, BaseAgent.__init__, BaseAgent.run, BaseAgent.ask_llm, BaseAgent.to_agent_signal

- [`skills/market-briefing/agents/fundamental_analyst.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/fundamental_analyst.py)｜80 行｜`29a3b0a2ee3d`｜FundamentalAnalyst, FundamentalAnalyst.__init__, FundamentalAnalyst.run

- [`skills/market-briefing/agents/portfolio_manager.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/portfolio_manager.py)｜73 行｜`7556d82caae7`｜PortfolioManager, PortfolioManager.__init__, PortfolioManager.run

- [`skills/market-briefing/agents/risk_manager.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/risk_manager.py)｜71 行｜`efc507184a5f`｜RiskManager, RiskManager.__init__, RiskManager.run

- [`skills/market-briefing/agents/sentiment_analyst.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/sentiment_analyst.py)｜87 行｜`ce481905b87b`｜SentimentAnalyst, SentimentAnalyst.__init__, SentimentAnalyst.run

- [`skills/market-briefing/agents/technical_analyst.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/agents/technical_analyst.py)｜96 行｜`fb2484d05445`｜TechnicalAnalyst, TechnicalAnalyst.__init__, TechnicalAnalyst.run, TechnicalAnalyst._ema

- [`skills/market-briefing/committee.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/committee.py)｜89 行｜`16cfb3be8c6d`｜HedgeFundCommittee, HedgeFundCommittee.__init__, HedgeFundCommittee.run_analysis

- [`skills/market-briefing/data/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/data/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/market-briefing/data/fetcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/data/fetcher.py)｜327 行｜`b61b5f4604ec`｜_fixture_market_data, _http_json, _yahoo_history, _load_cache_fetcher, _save_cache_fetcher, _tz_now_str, _get_twse_lookup, _get_sec_tickers, _latest_tw_financials, _latest_us_filing

- [`skills/market-briefing/data/indicators.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/data/indicators.py)｜154 行｜`6e4b2d6df761`｜_ema, _pct, _clamp, _rsi, _macd, _bbands, _support_resistance, _volume_trend, _adx_approx, _safe_mean

- [`skills/market-briefing/data/perf_tracker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/data/perf_tracker.py)｜579 行｜`7391fedf8b79`｜_tz_now, _load_json, _save_json, _notify_log, _load_cache, _save_cache, _load_perf, _save_perf, _parse_ymd, _next_trade_date

- [`skills/market-briefing/data/watchlist.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/data/watchlist.py)｜231 行｜`db0598103c15`｜_tz_now, _load_json_ws, _save_json_ws, WatchItem, WatchItem.to_dict, _unique, _tokenize, _resolve_tokens, _load_state, _save_state

- [`skills/market-briefing/delivery.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/delivery.py)｜181 行｜`4cca89b08110`｜_compact_lines, _shorten_line, _is_stock_summary_line, build_market_chat_summary, export_market_report, deliver_market_report

- [`skills/market-briefing/market_news.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/market_news.py)｜248 行｜`66fd26b3f955`｜_env_int, _strip_html, _load_cache, _save_cache, _fetch_text, _node_text, _split_title_source, _parse_news_rss, _dedupe, _build_queries

- [`skills/market-briefing/mbcmd/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/mbcmd/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/market-briefing/mbcmd/backtest_cmd.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/mbcmd/backtest_cmd.py)｜145 行｜`90d14ecf25b5`｜_cmd_backtest

- [`skills/market-briefing/mbcmd/comps_cmd.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/mbcmd/comps_cmd.py)｜177 行｜`865f5ef508d9`｜_fetch_comps_metrics, _cmd_comps, _cmd_comps._median, _cmd_comps._fmt, _cmd_comps._tag

- [`skills/market-briefing/mbcmd/export_cmd.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/mbcmd/export_cmd.py)｜160 行｜`e9dfd87bf3a5`｜_tz_now, _skill_python, _cmd_export

- [`skills/market-briefing/mbcmd/sector_cmd.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/mbcmd/sector_cmd.py)｜230 行｜`034b087dc5e8`｜_resolve_sector_name, _get_twse_sector_map, _find_peers, _cmd_sector

- [`skills/market-briefing/models/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/models/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/market-briefing/models/signals.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/models/signals.py)｜40 行｜`dea9220d94f9`｜TradingAction, TradingSignal, AgentSignal, CommitteeState

- [`skills/market-briefing/predict/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/predict/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/market-briefing/predict/predict_engine.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/predict/predict_engine.py)｜306 行｜`3e4238edb92d`｜_predict_one, _render_report

- [`skills/market-briefing/test_agent_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/test_agent_live.py)｜30 行｜`4449808e5ffa`｜—

- [`skills/market-briefing/test_committee_logic.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/test_committee_logic.py)｜61 行｜`8ad00e1f3097`｜test_mock_flow, test_mock_flow.mocked_ask_llm

- [`skills/market-briefing/utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/utils.py)｜93 行｜`2a0e81a03969`｜_http_json, fetch_yahoo_history, get_tw_financials, gather_all_data

- [`skills/memory/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/__init__.py)｜1 行｜`c08f6504853e`｜—

- [`skills/memory/codebase_rag.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/codebase_rag.py)｜195 行｜`8a7281a3d6bd`｜_ensure_embedding_fn, _embed, CodebaseMemory, CodebaseMemory.__init__, CodebaseMemory.ingest_file, CodebaseMemory.query, CodebaseMemory.reset

- [`skills/memory/cortex_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/cortex_sync.py)｜443 行｜`c179b6325d0b`｜_load_runtime_environment, _resolve_osc_host, CortexSync, CortexSync.__init__, CortexSync._load_state, CortexSync._save_state, CortexSync.get_source_connection, CortexSync._remember_strict, CortexSync._judgment_memory_contents, CortexSync.sync_legal_news

- [`skills/memory/faiss_index.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/faiss_index.py)｜924 行｜`c8178553ac32`｜active_generation_paths, FAISSMemoryIndex, FAISSMemoryIndex.get_instance, FAISSMemoryIndex.__init__, FAISSMemoryIndex.search, FAISSMemoryIndex.add, FAISSMemoryIndex.add_batch, FAISSMemoryIndex.total, FAISSMemoryIndex.index_type, FAISSMemoryIndex.build_from_db

- [`skills/memory/job_queue.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/job_queue.py)｜360 行｜`5a5f685c2974`｜_ConnectionProxy, _ConnectionProxy.__init__, _ConnectionProxy.__getattr__, _ConnectionProxy.__enter__, _ConnectionProxy.__exit__, _open_conn, _get_conn, _now, _row_to_dict, enqueue

- [`skills/memory/keeper_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/keeper_sync.py)｜259 行｜`8636cba818cb`｜get_embedding, check_keeper_online, _ensure_schema, _row_md5, _already_synced_in_target, _memory_is_tombstoned, sync_to_keeper, sync_loop, start_sync_daemon

- [`skills/memory/local_db.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/local_db.py)｜348 行｜`30f8216441f7`｜_get_connection, save_local, get_pending_sync, mark_synced, save_vector_local, search_local, search_local._score

- [`skills/memory/magi_brain_setup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/magi_brain_setup.py)｜50 行｜`1686d4df6e82`｜init_db

- [`skills/memory/mem_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/mem_bridge.py)｜1648 行｜`222db036e024`｜_database_port_from_env, _recall_cache_store, _normalize_source_text, _query_prefers_chatlog, _query_terms, _source_trust_weight, _rank_recall_results, _allow_result_for_query, _now_ts, _keeper_offline

- [`skills/memory/message_queue.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/message_queue.py)｜348 行｜`3986f625ffa5`｜_now, _row_to_dict, _ConnectionProxy, _ConnectionProxy.__init__, _ConnectionProxy.__getattr__, _ConnectionProxy.__enter__, _ConnectionProxy.__exit__, _open_conn, MessageQueue, MessageQueue.__init__

- [`skills/memory/migration_fulltext.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/migration_fulltext.py)｜26 行｜`265fc5c389a5`｜add_fulltext_index

- [`skills/memory/setup_rag_db.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/setup_rag_db.py)｜89 行｜`0e474cda6307`｜migrate_synced_column, migrate_source_column, setup_rag_db

- [`skills/memory/sqlite_backup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/sqlite_backup.py)｜188 行｜`e03a29dba72d`｜_get_connection, save_to_backup, get_pending_sync, mark_synced, search_backup, get_backup_count

- [`skills/memory/vector_pipeline.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/memory/vector_pipeline.py)｜5 行｜`ad0bae39f0b4`｜—

- [`skills/mock-test/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/mock-test/action.py)｜200 行｜`c0fc60ba6262`｜run_mock_test, _notify, main

- [`skills/obsidian/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/obsidian/action.py)｜2507 行｜`f522750abe70`｜_resolve_agent_dir, _is_known_malformed_pdf_skip, _load_vault_config, _save_vault_config, _load_index, _save_index, _replace_index, _prune_missing_index_entries, _get_vault_path, _has_obsidian_cli

- [`skills/obsidian/bootstrap_synology_vault.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/obsidian/bootstrap_synology_vault.py)｜252 行｜`5b06b11c2bfa`｜pick_existing, ensure_symlink, directory_snapshot, write_text, set_magi_vault, build_home_note, build_source_index, bootstrap, main

- [`skills/obsidian/extractors.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/obsidian/extractors.py)｜617 行｜`e69147a960f0`｜file_hash, _generated_image_artifact_count, _remove_generated_image_artifacts, _time_limit, _time_limit._handle_timeout, _text_signal_score, _markitdown_text_is_usable, _extract_with_markitdown, extract_text, _extract_plaintext

- [`skills/ops/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/ops/circuit_breaker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/circuit_breaker.py)｜153 行｜`899faba4058e`｜_load_state, _save_state, is_tripped, record_failure, record_success, manual_reset, get_status

- [`skills/ops/cloud_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/cloud_policy.py)｜31 行｜`7d110615381c`｜cloud_models_allowed, require_cloud_models_allowed

- [`skills/ops/config.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/config.py)｜95 行｜`6c8070d5e81c`｜_is_feature_enabled, validate_config

- [`skills/ops/cron_command_identity.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/cron_command_identity.py)｜244 行｜`78bf3267e107`｜CronCommandIdentityError, _bound_release_root, _root_relative_parts, _bound_runtime_shared_parts, _canonical_path, _canonical_token, _infer_rebased_checkout_root, canonical_command_tokens, command_definition_sha256

- [`skills/ops/cron_result_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/cron_result_policy.py)｜322 行｜`e3c691f8ea9a`｜CronResultClassification, _last_json_object, _contract_error, terminal_schedule_deferral_reason, legacy_candidate_rejection_reason, _is_resource_guard_skip, classify_cron_result, looks_successful_despite_returncode, should_log_cron_issue

- [`skills/ops/cron_runtime_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/cron_runtime_policy.py)｜64 行｜`94d34f24b847`｜cron_job_timeout

- [`skills/ops/cron_scheduler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/cron_scheduler.py)｜1659 行｜`85f7075fd708`｜_align_datetime_for_comparison, _use_runtime_dir, _cron_state_path, _file_lock, _atomic_write_json, _sanitize_job_definition, _load_cron_state, _save_cron_state, _update_cron_state, _update_cron_states

- [`skills/ops/daily_reflection.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/daily_reflection.py)｜158 行｜`780551023748`｜parse_v3_conversation_history, run_reflection

- [`skills/ops/database/backup_restore.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/database/backup_restore.py)｜643 行｜`b351ca2e5a89`｜_remote_db_ip_or, _ensure_dotenv_loaded, DBProfile, _now, _q, _load_profiles, _choose_remote_profile, _choose_local_profile, _ping_db, _find_bin

- [`skills/ops/database/sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/database/sync.py)｜100 行｜`bafd01402c61`｜sync_keeper_db

- [`skills/ops/database/sync_bidirectional.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/database/sync_bidirectional.py)｜478 行｜`6d5d9a722950`｜_remote_db_ip_or, DBProfile, _load_profiles, _connect, _choose_remote_profile, _choose_local_profile, _qname, _show_tables, _table_columns, _primary_key

- [`skills/ops/db_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/db_sync.py)｜299 行｜`bbcc17d31fff`｜check_db_availability, get_last_sync_time, sync_table, cmd_sync

- [`skills/ops/dedup_db.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/dedup_db.py)｜255 行｜`11be6eacba5d`｜_load_runtime_environment, _get_conn, normalize_case_id, is_done, mark_done, list_done, count_done, get_stats, remove

- [`skills/ops/export_docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/export_docx.py)｜1296 行｜`b373849cd8df`｜_load_public_base_url, _find_node, _find_node_path, _sealed_release_context, _node_backend, _exports_dir, _is_relative_to, _safe_generated_filename, _resolve_export_docx_path, _validate_docx_file

- [`skills/ops/export_text.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/export_text.py)｜190 行｜`7123f6d69682`｜_is_loopback_base_url, _normalize_base_url, _load_dotenv_value, _base_from_webhook_url, _build_tailscale_base_url, _load_public_base_url, export_txt

- [`skills/ops/file_manager.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/file_manager.py)｜163 行｜`e885c53a6861`｜_is_allowed, list_directory, search_files, file_info, _format_size

- [`skills/ops/file_review_auto_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/file_review_auto_worker.py)｜701 行｜`8eb0026b7078`｜_handle_stop_signal, _write_state, _completed_cycle_state, _tail, _mark_payment_scan_nonfatal, _task_ok_or_nonfatal, _parse_last_json, _parse_etime_to_sec, _write_download_ownership, _load_download_ownership

- [`skills/ops/finder_ops.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/finder_ops.py)｜328 行｜`0d0b04f514ed`｜_run_osascript, _escape, move_file, copy_file, rename_file, create_folder, reveal_in_finder, get_file_info, move_to_trash, move_files_batch

- [`skills/ops/fs_watcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/fs_watcher.py)｜306 行｜`5527ef7d3e2d`｜CaseFolderHandler, CaseFolderHandler.__init__, CaseFolderHandler.on_created, CaseFolderHandler.on_modified, CaseFolderHandler.on_moved, CaseFolderHandler._debounce, CaseFolderHandler._dispatch, CaseFolderHandler.cleanup_stale_records, FSWatcher, FSWatcher.__init__

- [`skills/ops/health_probes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/health_probes.py)｜279 行｜`4127c2c49d51`｜_build_omlx_base_url, extract_model_labels, _fetch_omlx_models, probe_omlx_models, command_executes_python_script, find_python_script_processes, python_script_process_running, resolve_omlx_model, probe_local_chat

- [`skills/ops/heartbeat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/heartbeat.py)｜320 行｜`57b65a3eb7f9`｜_node_ip_or, guard_tailscale_serve, guard_local_chat_resident, check_omlx_health, check_ping, get_node_model, update_status, _desktop_notify

- [`skills/ops/iron_dome_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/iron_dome_sync.py)｜362 行｜`705f99f87948`｜_env_str, _env_int, _tailscale_ip, _advertise_ip, _node_ip_or, _normalize_pattern_list, _load_pattern_lists, get_patterns_hash, export_patterns, broadcast_update

- [`skills/ops/keychain_manager.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/keychain_manager.py)｜372 行｜`459bdb9356ae`｜is_available, get_secret, set_secret, delete_secret, list_secrets, resolve_keychain_value, load_config_with_keychain, resolve_env_keychain, _is_sensitive_key, migrate_env_to_keychain

- [`skills/ops/macos_notify.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/macos_notify.py)｜222 行｜`715da2f6a354`｜send_notification, _send_via_terminal_notifier, _send_via_osascript, notify_omlx_error, notify_nas_status, notify_cron_failure, notify_pdf_processed, notify_case_deadline

- [`skills/ops/platform_utils.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/platform_utils.py)｜708 行｜`8f6319e37a86`｜file_lock, file_unlock, file_lock, file_unlock, locked_file, get_magi_root, get_venv_python, get_temp_dir, get_data_dir, get_config_dir

- [`skills/ops/process_cleaner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/process_cleaner.py)｜40 行｜`bd1df9b2c074`｜check_and_kill

- [`skills/ops/process_guardian.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/process_guardian.py)｜341 行｜`2ebf90027bb1`｜_write_autopilot_kill_reason, get_running_processes, check_and_clean_duplicates, force_kill_all, is_daemon_running, _get_latest_mtime, reload_stale_services, patrol_zombies

- [`skills/ops/quicklook.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/quicklook.py)｜160 行｜`26b185dfd058`｜generate_thumbnail, generate_thumbnails_batch, cleanup_thumbnails

- [`skills/ops/red_phone.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/red_phone.py)｜2507 行｜`462f1bc486a3`｜_taipei_now, _alert_timestamp, _load_runtime_dotenv, _guard_text, _preview_text, _split_text_by_lines, _numbered_chunks, _load_runtime_config, _get_line_channel_access_token, _get_line_admin_targets

- [`skills/ops/safe_state.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/safe_state.py)｜75 行｜`bbef94d9f1ff`｜safe_load_json, safe_save_json

- [`skills/ops/self_repair_reporter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/self_repair_reporter.py)｜683 行｜`96cdde6b5b75`｜_stdout_tail_payload, _resource_governor_label, _resource_governor_detail, _error_label, _error_detail, _display_error_label, _job_label, _load_agenda, _group_records, _parse_ts

- [`skills/ops/smart_summary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/smart_summary.py)｜159 行｜`82e2036d40d7`｜summarize_text, extract_key_points, summarize_url, summarize_to_docx

- [`skills/ops/spotlight_search.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/spotlight_search.py)｜234 行｜`2dba9389fbad`｜is_exact_query, normalize_case_number, spotlight_search, spotlight_search_case, spotlight_search_person, check_spotlight_indexed

- [`skills/ops/structured_log.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/structured_log.py)｜118 行｜`4a0d4eb22785`｜set_request_context, clear_request_context, RequestContextFilter, RequestContextFilter.filter, JSONFormatter, JSONFormatter.format, HybridFormatter, HybridFormatter.format

- [`skills/ops/system_monitor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/system_monitor.py)｜173 行｜`3265d14616bd`｜get_system_status, _basic_status, check_service_health

- [`skills/ops/system_test.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/system_test.py)｜405 行｜`f177c8c7ce58`｜_load_db_profile, _ping, _tcp_connect, _http_get, _probe_omlx_chat, test_casper_ollama, test_melchior_remote, test_balthasar_remote, test_keeper_db, test_memory_module

- [`skills/ops/task_tracker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/task_tracker.py)｜57 行｜`f1dc0fbaa58a`｜TaskTracker, TaskTracker.__init__, TaskTracker._load_tasks, TaskTracker._save_tasks, TaskTracker.update_task, TaskTracker.complete_task

- [`skills/ops/test_keeper_connection.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/test_keeper_connection.py)｜41 行｜`2f9e8c043e4c`｜check_connection

- [`skills/ops/update_heartbeat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/update_heartbeat.py)｜42 行｜`6da06ba1f39a`｜get_balthasar_status, get_casper_status, get_melchior_status, generate_heartbeat

- [`skills/ops/user_activity_beacon.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/user_activity_beacon.py)｜72 行｜`06ea7ab52a0d`｜touch, is_user_active, seconds_since_last_activity, last_activity_info

- [`skills/ops/watcher_daemon.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/watcher_daemon.py)｜323 行｜`dcaa3f238f4d`｜_tools_api_url, init_local_db, get_last_pull_id, pull_audit_logs, log_pull_status, detect_anomalies, log_anomaly, send_alert, get_status, main_loop

- [`skills/osc-orchestrator/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/osc-orchestrator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/action.py)｜5498 行｜`1fcf874b2037`｜_write_token_atomic, _eventlog, _maybe_reexec_venv, _json_load_maybe, _listdir_timeout, _extract_case_number_from_text, _extract_case_number_from_path, _gcal_history_cutoff_date, _gcal_event_start_date, _gcal_norm_text

- [`skills/osc-orchestrator/gcal_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/gcal_sync.py)｜940 行｜`7214eb7f4b37`｜_load_creds, _write_token_atomic, _gcal_http_timeout_sec, _build_service, _split_calendar_ids, _get_conn, _osc_exec_sql, _get_setting_value, _make_todo_event, _make_cal_event

- [`skills/osc-orchestrator/osc_headless/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/osc_headless/__init__.py)｜12 行｜`8c98bc082f71`｜—

- [`skills/osc-orchestrator/osc_headless/db.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/osc_headless/db.py)｜1166 行｜`7786268ed074`｜_load_mysql, _extract_share_url, _share_host, _share_expires_soon, _should_refresh_share_description, _normalize_court_doc_identity, _source_specificity_score, DBConfig, _failover_host, _has_explicit_env

- [`skills/osc-orchestrator/osc_headless/gcal_dedup.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/osc_headless/gcal_dedup.py)｜333 行｜`7326d6969194`｜_to_halfwidth, _coerce_text, _normalize_date, _normalize_time, _extract_case_from_text, is_invalid_case_key, normalize_case_key, classify_event_kind, normalize_subject, _event_start_date_time

- [`skills/osc-orchestrator/osc_headless/todos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/osc_headless/todos.py)｜1274 行｜`a13c0575b572`｜_bounded_relative_deadline, _portable_basename, _parse_roc_year_to_ad, _collect_source_year_candidates, _collect_source_year_candidates._add_candidate, extract_document_date_from_filename, extract_base_year_from_filename, chinese_to_number, _parse_number_token, _parse_roc_or_ad_year

- [`skills/osc-orchestrator/vision_event_parser.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/vision_event_parser.py)｜106 行｜`684f6bc58b7b`｜extract_events_from_pdf

- [`skills/osc-scan-folder/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-scan-folder/action.py)｜162 行｜`273f7e0fe037`｜_ok, _load_jsonish, _run_orch, _normalize_candidate_path, _load_config_root, _resolve_default_root, main

- [`skills/osc_orchestrator/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc_orchestrator/__init__.py)｜2 行｜`d7f58330f293`｜—

- [`skills/osc_orchestrator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc_orchestrator/action.py)｜20 行｜`6f258758dd58`｜—

- [`skills/overlay.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/overlay.py)｜364 行｜`8eadb1d5cbd6`｜base_skills_dir, skill_overlay_dir, skill_versions_dir, skill_runtime_site_packages_dir, skill_events_file, skill_usage_tracker_file, validate_skill_name, _safe_child, _file_sha256, _tracked_files

- [`skills/pdf-annotator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-annotator/action.py)｜645 行｜`3978cad68b97`｜_load_json, _save_json, _page_to_image, _get_toc, _get_gateway, _vision_suggest_bookmarks, cmd_learn, cmd_annotate, cmd_test, cmd_status

- [`skills/pdf-bookmarker/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-bookmarker/action.py)｜1836 行｜`b2d7c7729779`｜_get_ocr_engine, release_ocr_engine, _p, _build_court_watermark_line_re, _extract_roc_date, _extract_party, _is_ola_separator, _meaningful_char_count, _compute_ola_threshold, _is_prior_record_page

- [`skills/pdf-bookmarker/bookmark_validator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-bookmarker/bookmark_validator.py)｜170 行｜`b40e789be3d0`｜_valid_bookmark_date, normalize_bookmark, validate_bookmark

- [`skills/pdf-namer/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/action.py)｜5069 行｜`c1e11d15dd94`｜_is_synthetic_case_path, _with_vision_slot, _get_ocr_engine, release_ocr_engine, _resolve_pdf_with_synology_fallback, clear_batch_analysis_cache, _strip_date_prefix, _normalize_filename_key, _tokenize_filename, _extract_name_from_filename

- [`skills/pdf-namer/layout_extractor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/layout_extractor.py)｜138 行｜`c95ed083cdcc`｜_docling_enabled, _get_converter, generate_layout_sidecar, _fixture_layout_document

- [`skills/pdf-namer/naming_rules.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/naming_rules.py)｜387 行｜`d734c537e247`｜extract_date, _find_receipt_date, _find_labeled_date, extract_case_number, extract_court_name, extract_party_name, classify_document, build_few_shot_examples, load_training_data

- [`skills/pdf-namer/naming_validator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/naming_validator.py)｜521 行｜`38290d60d292`｜_BalancedBracketMatch, _BalancedBracketMatch.group, _BalancedBracketMatch.start, _BalancedBracketMatch.end, _opencc_s2t, _strip_ext, _normalize_name, _to_traditional, _extract_party_segment_with_span, _balanced_outer_brackets

- [`skills/pdf-namer/nightly_layout.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/nightly_layout.py)｜228 行｜`f7d463627b53`｜_enabled, _collect_from_filing_log, _collect_from_scan_root, _dedupe, _certification_fixture_root, _write_manifest, main

- [`skills/pdf-namer/nightly_train.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/nightly_train.py)｜779 行｜`f92591355daa`｜_enable_main_file_logging, _parse_existing_filename, _is_synthetic_case_path, _is_transient_storage_error, _sha256_file, _normalize_date, _subfolder_label, collect_samples, collect_samples._raise_transient_walk_error, analyze_one

- [`skills/pdf-namer/rag_feedback.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/rag_feedback.py)｜114 行｜`308e83bf9978`｜FeedbackRAG, FeedbackRAG.__init__, FeedbackRAG._load, FeedbackRAG.query, FeedbackRAG.log_feedback

- [`skills/pdf-namer/rename_watcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/rename_watcher.py)｜446 行｜`37d802d57558`｜_init_case_roots, _is_synthetic_case_path, _get_case_root, _parse_filename, _subfolder_to_category, scan_pdfs, load_snapshot, save_snapshot, detect_renames, extract_learning

- [`skills/pdf-namer/smart_filer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/smart_filer.py)｜1475 行｜`3837dbd83b10`｜_safe_listdir, _eventlog, _doc_type_targets_judgment_folder, _canonicalize_case_index_subfolders, _canonicalize_case_index, _is_synthetic_case_text, _is_synthetic_case_path, _is_synthetic_case_index_entry, build_case_index, _parse_case_folder

- [`skills/pdf-namer/state_paths.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/state_paths.py)｜138 行｜`81e27717d998`｜pdf_namer_state_dir, _relative, state_path, legacy_path, read_path, configured_read_path, prepare_write

- [`skills/pdf-namer/training_loader.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/training_loader.py)｜788 行｜`5a5be439c93e`｜_truthy, _bootstrap_runtime_credentials, _get_rules_bundle_path, _rules_bundle_max_age_seconds, _compute_rules_checksum, _build_rules_bundle, _parse_iso_datetime, _read_rules_bundle, _update_rules_status, get_doc_rules_status

- [`skills/pdf-namer/vision_parser.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/vision_parser.py)｜523 行｜`98d68a1ec85f`｜_get_omlx_chat, _ask_omlx_vision, _load_ollama_meta, _is_vision_model, _vision_models, _ask_openai_compatible, _ask_ollama_vision, _parse_json_object, _parse_date_from_text, extract_date_with_vision

- [`skills/pdf/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/action.py)｜724 行｜`81fd4ca8de82`｜_ensure_dir, _exports_dir, _split_env_paths, _allowed_roots, _is_relative_to, _ensure_under_allowed_root, _input_file, _output_file, _output_dir, _temp_sibling

- [`skills/pdf/scripts/check_bounding_boxes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/check_bounding_boxes.py)｜65 行｜`0ced522402dd`｜RectAndField, get_bounding_box_messages, get_bounding_box_messages.rects_intersect

- [`skills/pdf/scripts/check_fillable_fields.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/check_fillable_fields.py)｜11 行｜`1fe10b9980a5`｜—

- [`skills/pdf/scripts/convert_pdf_to_images.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/convert_pdf_to_images.py)｜33 行｜`7f58e292a67e`｜convert

- [`skills/pdf/scripts/create_validation_image.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/create_validation_image.py)｜37 行｜`8fa9fd7962c9`｜create_validation_image

- [`skills/pdf/scripts/extract_form_field_info.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/extract_form_field_info.py)｜122 行｜`bb235b36f497`｜get_full_annotation_field_id, make_field_dict, get_field_info, get_field_info.sort_key, write_field_info

- [`skills/pdf/scripts/extract_form_structure.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/extract_form_structure.py)｜115 行｜`6814e3fe8f78`｜extract_form_structure, main

- [`skills/pdf/scripts/fill_fillable_fields.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/fill_fillable_fields.py)｜98 行｜`d140872c2e92`｜fill_pdf_fields, validation_error_for_field_value, monkeypatch_pydpf_method, monkeypatch_pydpf_method.patched_get_inherited

- [`skills/pdf/scripts/fill_pdf_form_with_annotations.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/scripts/fill_pdf_form_with_annotations.py)｜107 行｜`8a56e063a49e`｜transform_from_image_coords, transform_from_pdf_coords, fill_pdf_form

- [`skills/plugin.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/plugin.py)｜734 行｜`e6562c12ef56`｜SkillMeta, SkillPlugin, SkillPlugin.execute, SkillPlugin.capability_guide, SkillPlugin.health_check, SkillRegistry, SkillRegistry.__init__, SkillRegistry.register_plugin, SkillRegistry.register_handler, SkillRegistry.register_capability_guide

- [`skills/pptx/scripts/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/pptx/scripts/add_slide.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/add_slide.py)｜195 行｜`9a6b0b573df0`｜get_next_slide_number, create_slide_from_layout, duplicate_slide, _add_to_content_types, _add_to_presentation_rels, _get_next_slide_id, parse_source

- [`skills/pptx/scripts/clean.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/clean.py)｜286 行｜`3e3966a17f48`｜get_slides_in_sldidlst, remove_orphaned_slides, remove_trash_directory, get_slide_referenced_files, remove_orphaned_rels_files, get_referenced_files, remove_orphaned_files, update_content_types, clean_unused_files

- [`skills/pptx/scripts/office/helpers/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/helpers/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/pptx/scripts/office/helpers/merge_runs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/helpers/merge_runs.py)｜199 行｜`7c40ed838b88`｜merge_runs, _find_elements, _find_elements.traverse, _get_child, _get_children, _is_adjacent, _remove_elements, _strip_run_rsid_attrs, _merge_runs_in, _first_child_run

- [`skills/pptx/scripts/office/helpers/simplify_redlines.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/helpers/simplify_redlines.py)｜197 行｜`560cb55978a8`｜simplify_redlines, _merge_tracked_changes_in, _is_element, _get_author, _can_merge_tracked, _merge_tracked_content, _find_elements, _find_elements.traverse, get_tracked_change_authors, _get_authors_from_docx

- [`skills/pptx/scripts/office/pack.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/pack.py)｜159 行｜`71212f3cbe4e`｜pack, _run_validation, _condense_xml

- [`skills/pptx/scripts/office/soffice.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/soffice.py)｜183 行｜`a3e21840e29e`｜get_soffice_env, run_soffice, _needs_shim, _ensure_shim

- [`skills/pptx/scripts/office/unpack.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/unpack.py)｜133 行｜`2cd2eccafc33`｜unpack, _pretty_print_xml, _escape_smart_quotes

- [`skills/pptx/scripts/office/validate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/validate.py)｜110 行｜`5a72593df9a6`｜main

- [`skills/pptx/scripts/office/validators/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/validators/__init__.py)｜15 行｜`83e0f035c5ab`｜—

- [`skills/pptx/scripts/office/validators/base.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/validators/base.py)｜848 行｜`13930fa2bb73`｜BaseSchemaValidator, BaseSchemaValidator.__init__, BaseSchemaValidator.validate, BaseSchemaValidator.repair, BaseSchemaValidator.repair_whitespace_preservation, BaseSchemaValidator.validate_xml, BaseSchemaValidator.validate_namespaces, BaseSchemaValidator.validate_unique_ids, BaseSchemaValidator.validate_file_references, BaseSchemaValidator.validate_all_relationship_ids

- [`skills/pptx/scripts/office/validators/docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/validators/docx.py)｜447 行｜`0bae1bd4bae3`｜DOCXSchemaValidator, DOCXSchemaValidator.validate, DOCXSchemaValidator.validate_whitespace_preservation, DOCXSchemaValidator.validate_deletions, DOCXSchemaValidator.count_paragraphs_in_unpacked, DOCXSchemaValidator.count_paragraphs_in_original, DOCXSchemaValidator.validate_insertions, DOCXSchemaValidator.compare_paragraph_counts, DOCXSchemaValidator._parse_id_value, DOCXSchemaValidator.validate_id_constraints

- [`skills/pptx/scripts/office/validators/pptx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/validators/pptx.py)｜275 行｜`f937961e62a5`｜PPTXSchemaValidator, PPTXSchemaValidator.validate, PPTXSchemaValidator.validate_uuid_ids, PPTXSchemaValidator._looks_like_uuid, PPTXSchemaValidator.validate_slide_layout_ids, PPTXSchemaValidator.validate_no_duplicate_slide_layouts, PPTXSchemaValidator.validate_notes_slide_references

- [`skills/pptx/scripts/office/validators/redlining.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/validators/redlining.py)｜248 行｜`97abb243543f`｜RedliningValidator, RedliningValidator.__init__, RedliningValidator.repair, RedliningValidator.validate, RedliningValidator._generate_detailed_diff, RedliningValidator._get_git_word_diff, RedliningValidator._remove_author_tracked_changes, RedliningValidator._extract_text_content

- [`skills/pptx/scripts/thumbnail.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/thumbnail.py)｜289 行｜`e959ecd4f197`｜main, get_slide_info, build_slide_list, create_hidden_placeholder, convert_to_images, create_grids, create_grid

- [`skills/process-hygiene/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/process-hygiene/action.py)｜723 行｜`b9bce406628f`｜_ps_all, _etime_to_seconds, _is_magi_process, _pid_alive, _is_managed_long_running, _is_expected_detached_job, _v3_pid_directory, _is_managed_v3_launchd_role, _command_executes_python_script, _duplicate_keep_key

- [`skills/reasoning/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/reasoning/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/reasoning/wfgy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/reasoning/wfgy.py)｜17 行｜`f2828ea8ab28`｜apply_wfgy_logic

- [`skills/research-brief/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/action.py)｜751 行｜`9a407e4e6bb6`｜_ok, _atomic_write_json, _load_json, _ensure_seeds_bootstrapped, _safe_filename, _list_namespaces, _load_namespace, _save_namespace, _delete_namespace, _new_empty_namespace

- [`skills/research-brief/digest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/digest.py)｜171 行｜`ffa098e58cf2`｜_hostname, _extract_tags, _truncate_zh, _zh_summarize_local, _translate_title, format_entry, format_digest

- [`skills/research-brief/fetchers.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/fetchers.py)｜371 行｜`54bd5e842a16`｜_strip_html, _http_get, fetch_rss, fetch_json_api, _autodiscover_rss, _extract_article_links, fetch_html, fetch_source

- [`skills/research-brief/translator_bridge.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/translator_bridge.py)｜131 行｜`e97240d17f6c`｜_normalize_lang, _detect_lang, translate_to_zh_hant

- [`skills/research/github_monitor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research/github_monitor.py)｜75 行｜`ccf04685f689`｜_internet_enabled, search_repos, get_trending

- [`skills/research/rss_reader.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research/rss_reader.py)｜124 行｜`5c53080810e1`｜RSSReader, RSSReader.__init__, RSSReader._load_feeds, RSSReader._save_feeds, RSSReader.add_feed, RSSReader.list_feeds, RSSReader.read_latest

- [`skills/research/web_research.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research/web_research.py)｜744 行｜`f31115071185`｜validate_skill_safety, _validate_web_content, _internet_enabled, _is_private_host, _internet_guard, search_duckduckgo, search_web, fetch_url_content, fetch_url_sections, fetch_raw_url

- [`skills/screenshot-sorter-tw/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/screenshot-sorter-tw/action.py)｜241 行｜`35ba08226f47`｜collect_images, analyze_screenshot, sort_screenshots, sort_screenshots.sort_key, rename_and_copy, add_watermark, run

- [`skills/skill_loader.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/skill_loader.py)｜166 行｜`5bc99304d34a`｜load_all_skills, _register_direct_handlers

- [`skills/source_control/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/source_control/__init__.py)｜2 行｜`cc561183bbc6`｜—

- [`skills/source_control/git_ops.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/source_control/git_ops.py)｜20 行｜`8fa3bf65f6b7`｜get_status

- [`skills/statutes-vdb/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/statutes-vdb/action.py)｜1004 行｜`1320a82a2328`｜_eventlog, _internet_enabled, _now_iso, _json_load_maybe, _load_json, _save_json, _norm_name, _case_domain, _extract_law_hints_from_case_path, _download_zip_bytes

- [`skills/transcript-downloader/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/transcript-downloader/action.py)｜3279 行｜`a860f23f626d`｜load_openclaw_config, get_legacy_telegram_settings, _flow_slug, _safe_create_flow_mirror, _safe_flow_step_status, _safe_finalize_flow, _mark_notify_step, _cancel_reason, _cancelled_result, _check_flow_cancelled

- [`skills/transcript-indexer/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/transcript-indexer/action.py)｜688 行｜`bc860f09235e`｜_load_index, _is_transcript_indexed, _save_index, _safe_child_dirs, _has_transcript_dir, _iter_case_dirs, _iter_transcript_pdfs, _extract_pages_inner, _extract_pages, _extract_pages._runner

- [`skills/transcript-todo-extractor/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/transcript-todo-extractor/action.py)｜1244 行｜`a679068db013`｜_safe_path_call, _safe_path_call._runner, _safe_exists, _safe_is_file, _safe_is_dir, _safe_stat_mtime, _safe_child_dirs, _safe_pdf_glob, _safe_pdf_rglob, _safe_pdf_rglob._scan

- [`skills/translator/_apple_post_edit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/translator/_apple_post_edit.py)｜259 行｜`3a27dc87e5d2`｜is_legal_text, _apple_translate, _lang_label, _post_edit_prompt, _detect_src_lang, _run_local_llm, _strip_preamble, _extract_case_numbers, _append_missing_case_numbers, translate_with_ape

- [`skills/translator/_post_edit_validator.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/translator/_post_edit_validator.py)｜224 行｜`383a460f90b3`｜_load_sc_chars, _extract_numbers, _extract_case_numbers, _extract_parties, _detect_simplified_chinese, _detect_repetition, validate_post_edit

- [`skills/translator/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/translator/action.py)｜1280 行｜`f3cbc69b9a5c`｜_strip_translation_preamble, _maybe_reexec_venv, _load_jsonish, _ok, _now_hint, _export_txt, _export_docx_bilingual, _load_text_from_file, _split_chunks, _normalize_target_lang_code

- [`skills/translator/legal_termbase.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/translator/legal_termbase.py)｜511 行｜`9de9e5f5df90`｜_create_db_schema, build_tier1_from_moj, _extract_legal_terms_heuristic, lookup_tier1, lookup_article, build_vector_index, _load_tier2, lookup_tier2, _detect_tier2_in_text, _is_legal_text

- [`skills/trial-prep/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/trial-prep/action.py)｜487 行｜`c787e3df12e9`｜_resolve_case_base, _query_calendar_events, _query_calendar_osascript, _extract_case_number, _find_case_folder, _scan_case_folder, _query_statutes, _query_judgments, _cmd_upcoming, _cmd_prepare

- [`skills/worldmonitor-intel/action.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/worldmonitor-intel/action.py)｜792 行｜`dd6fd0b8c0f7`｜_mutable_static_dir, _fetch, _fetch_json, _extract_model_labels, _parse_rss, collect_news, collect_markets, _collect_stooq_markets, _reason_with_melchior, _store_to_memory

- [`skills/xlsx/scripts/office/helpers/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/helpers/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`skills/xlsx/scripts/office/helpers/merge_runs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/helpers/merge_runs.py)｜199 行｜`7c40ed838b88`｜merge_runs, _find_elements, _find_elements.traverse, _get_child, _get_children, _is_adjacent, _remove_elements, _strip_run_rsid_attrs, _merge_runs_in, _first_child_run

- [`skills/xlsx/scripts/office/helpers/simplify_redlines.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/helpers/simplify_redlines.py)｜197 行｜`560cb55978a8`｜simplify_redlines, _merge_tracked_changes_in, _is_element, _get_author, _can_merge_tracked, _merge_tracked_content, _find_elements, _find_elements.traverse, get_tracked_change_authors, _get_authors_from_docx

- [`skills/xlsx/scripts/office/pack.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/pack.py)｜159 行｜`71212f3cbe4e`｜pack, _run_validation, _condense_xml

- [`skills/xlsx/scripts/office/soffice.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/soffice.py)｜183 行｜`a3e21840e29e`｜get_soffice_env, run_soffice, _needs_shim, _ensure_shim

- [`skills/xlsx/scripts/office/unpack.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/unpack.py)｜133 行｜`2cd2eccafc33`｜unpack, _pretty_print_xml, _escape_smart_quotes

- [`skills/xlsx/scripts/office/validate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/validate.py)｜110 行｜`5a72593df9a6`｜main

- [`skills/xlsx/scripts/office/validators/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/validators/__init__.py)｜15 行｜`83e0f035c5ab`｜—

- [`skills/xlsx/scripts/office/validators/base.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/validators/base.py)｜848 行｜`13930fa2bb73`｜BaseSchemaValidator, BaseSchemaValidator.__init__, BaseSchemaValidator.validate, BaseSchemaValidator.repair, BaseSchemaValidator.repair_whitespace_preservation, BaseSchemaValidator.validate_xml, BaseSchemaValidator.validate_namespaces, BaseSchemaValidator.validate_unique_ids, BaseSchemaValidator.validate_file_references, BaseSchemaValidator.validate_all_relationship_ids

- [`skills/xlsx/scripts/office/validators/docx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/validators/docx.py)｜447 行｜`0bae1bd4bae3`｜DOCXSchemaValidator, DOCXSchemaValidator.validate, DOCXSchemaValidator.validate_whitespace_preservation, DOCXSchemaValidator.validate_deletions, DOCXSchemaValidator.count_paragraphs_in_unpacked, DOCXSchemaValidator.count_paragraphs_in_original, DOCXSchemaValidator.validate_insertions, DOCXSchemaValidator.compare_paragraph_counts, DOCXSchemaValidator._parse_id_value, DOCXSchemaValidator.validate_id_constraints

- [`skills/xlsx/scripts/office/validators/pptx.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/validators/pptx.py)｜275 行｜`f937961e62a5`｜PPTXSchemaValidator, PPTXSchemaValidator.validate, PPTXSchemaValidator.validate_uuid_ids, PPTXSchemaValidator._looks_like_uuid, PPTXSchemaValidator.validate_slide_layout_ids, PPTXSchemaValidator.validate_no_duplicate_slide_layouts, PPTXSchemaValidator.validate_notes_slide_references

- [`skills/xlsx/scripts/office/validators/redlining.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/validators/redlining.py)｜248 行｜`97abb243543f`｜RedliningValidator, RedliningValidator.__init__, RedliningValidator.repair, RedliningValidator.validate, RedliningValidator._generate_detailed_diff, RedliningValidator._get_git_word_diff, RedliningValidator._remove_author_tracked_changes, RedliningValidator._extract_text_content

- [`skills/xlsx/scripts/recalc.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/recalc.py)｜184 行｜`cf419d15e029`｜has_gtimeout, setup_libreoffice_macro, recalc, main

### src/（10 檔）

- [`src/supplement_core/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/__init__.py)｜29 行｜`cde9e3bf6933`｜—

- [`src/supplement_core/attachment_matcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/attachment_matcher.py)｜448 行｜`6eceff5c10fc`｜_nfc, _collect_files, _magi_root, _ocr_cache_path, _guess_category_from_text, _run_ocr_first_page_only, _ocr_first_page, find_candidates, _stage1_filename_match

- [`src/supplement_core/case_meta.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/case_meta.py)｜336 行｜`2577c9462951`｜_chinese_to_int, _int_to_chinese, _find_subfolder, _folder_to_category, _scan_brief_seq, _scan_brief_seq._blacklisted, _try_db_merge, parse_case_meta

- [`src/supplement_core/case_no_updater.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/case_no_updater.py)｜160 行｜`816fb79f7e7b`｜_get_general, _get_general._M, _extract_case_no_from_filename, _verify, _update_db_case_no, update_case_no_from_notices

- [`src/supplement_core/docx_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/docx_builder.py)｜369 行｜`c7ceceef23d2`｜_default_template_path, _int_to_chinese, _replace_in_paragraph, _apply_fields_to_doc, _apply_proof_table, _insert_lead_in, build_supplement_docx

- [`src/supplement_core/exceptions.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/exceptions.py)｜14 行｜`4fa567bf7c6f`｜SupplementError, CaseNotFoundError, CategoryNotSupportedError, CourtNoticeFolderMissingError

- [`src/supplement_core/folder_writer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/folder_writer.py)｜158 行｜`ffbfbe4c1773`｜_sanitize_filename, _nfc, _make_folder_name, _make_docx_name, _make_attachment_name, write_brief_folder

- [`src/supplement_core/ruling_picker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/ruling_picker.py)｜86 行｜`9faf27d22a29`｜_guess_doc_type, list_court_notices

- [`src/supplement_core/ruling_text_loader.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/ruling_text_loader.py)｜341 行｜`86e1e78c3cbd`｜_magi_root, _cache_dir, _cache_key, _read_cache, _write_cache, _convert_pdf_to_images, _run_ocr_per_page, load_text

- [`src/supplement_core/supplement_extractor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/src/supplement_core/supplement_extractor.py)｜581 行｜`f9b6cc11cefb`｜_roc_to_iso, _extract_notice_date_from_text, _clean_party, _extract_case_year_western, _validate_date_against_case_year, _has_supplement_content, _extract_supplement_items_from_text, _improve_category, _get_gateway, _call_llm

### third_party/（2 檔）

- [`third_party/video_autopilot_kit/runtime/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/third_party/video_autopilot_kit/runtime/__init__.py)｜2 行｜`220f6714fbea`｜—

- [`third_party/video_autopilot_kit/runtime/portrait_normalizer.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/third_party/video_autopilot_kit/runtime/portrait_normalizer.py)｜53 行｜`d39f189c3f0e`｜normalize_to_portrait

<a id="appC"></a>
# 附錄 C. 全部測試原始碼索引

測試 Python 共 **298** 檔；測試本身是安全契約的一部分，特別是 fail-closed negative cases，不可因『現在會過』就刪除。

- [`tests/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/__init__.py)｜0 行｜`e3b0c44298fc`｜—

- [`tests/conftest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/conftest.py)｜240 行｜`e74c12672a86`｜_env_truthy, pytest_addoption, _explicit_live_request, _live_tests_enabled, _file_declares_live_marker, _argv_requests_live, pytest_ignore_collect, pytest_collection_modifyitems

- [`tests/support/__init__.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/support/__init__.py)｜1 行｜`01ba4719c80b`｜—

- [`tests/support/side_effect_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/support/side_effect_guard.py)｜114 行｜`49d5d95b161c`｜SideEffectBlocked, _truthy, live_enabled, _looks_like_real_nas_root, _is_pytest_temp_path, assert_safe_path, block_live_writer, block_live_writer._blocked

- [`tests/test_admin_runtime_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_admin_runtime_blueprint.py)｜1834 行｜`292599e8e8ad`｜_User, _User.__init__, _User.is_admin, _Orchestrator, _Orchestrator.__init__, _Orchestrator.get_skill_interview_state, _Orchestrator.start_skill_interview, _Orchestrator.reply_skill_interview

- [`tests/test_agent_readiness_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_agent_readiness_gate.py)｜197 行｜`ddd346113ddd`｜_load_gate, _capability, _write_catalog, test_repository_catalog_is_strict_ready_and_compact, test_high_risk_missing_verify_is_hard_failure, test_high_risk_missing_recovery_is_hard_failure, test_low_risk_missing_contract_is_warning_until_strict, test_missing_tool_and_private_content_fail_closed

- [`tests/test_agentic_bridges.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_agentic_bridges.py)｜187 行｜`accecbcce8ca`｜test_intent_envelope_round_trips_routing_context_and_decision, test_routing_dicts_are_supported_for_incremental_legacy_adoption, test_workflow_projects_to_task_records_without_runtime_registration, test_task_record_round_trip_preserves_step_contract_and_status, test_skipped_step_survives_task_projection, test_reconcile_task_record_updates_plan_snapshot_purely, test_task_record_without_agentic_metadata_is_rejected, test_cancelled_task_runtime_record_preserves_reason

- [`tests/test_agentic_contracts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_agentic_contracts.py)｜108 行｜`9e9d9770a5c5`｜test_intent_envelope_exposes_structured_fields_and_lookup, test_confidence_must_be_finite_and_in_range, test_boolean_is_not_accepted_as_confidence, test_contracts_reject_non_json_values_and_non_string_mapping_keys, test_missing_field_names_are_unique, test_required_confirmation_needs_reason_and_can_be_confirmed_immutably, test_confirmation_cannot_be_marked_when_not_required, test_intent_json_round_trip_preserves_unicode_and_nested_values

- [`tests/test_agentic_planner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_agentic_planner.py)｜202 行｜`c7800bf0b3c1`｜_intent, _dag_steps, test_build_plan_validates_and_stably_sorts_a_dag, test_plan_rejects_duplicate_unknown_self_and_cyclic_dependencies, test_missing_fields_block_all_steps_until_new_intent_is_planned, test_intent_confirmation_gates_whole_plan, test_step_lifecycle_unlocks_dependencies_and_local_confirmation, test_invalid_transitions_and_payloads_are_rejected

- [`tests/test_agentic_runtime_control.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_agentic_runtime_control.py)｜471 行｜`6215df61bb40`｜_token, test_root_ids_are_stable_opaque_and_platform_bound, test_event_reducer_is_deterministic_and_terminal_outcomes_are_closed, test_exact_owner_token_controls_once_only_terminal_transition, test_interrupt_resume_and_cooperative_cancel_are_root_owned, test_finishing_one_concurrent_root_run_does_not_hide_the_other, test_capacity_is_bounded_and_only_terminal_history_is_pruned, test_shadow_finish_keeps_public_status_running_when_another_run_is_active

- [`tests/test_ai_draft_dispatch_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_ai_draft_dispatch_quality.py)｜123 行｜`0bbef699d5ea`｜_context, test_ai_draft_requires_case_identifier_before_any_generation, test_ai_draft_asks_for_missing_grounding_instead_of_guessing, test_ai_draft_blocks_nonempty_output_that_fails_quality, test_ai_draft_only_previews_quality_passed_nvidia_output, test_ai_draft_only_previews_quality_passed_nvidia_output._quality, test_ai_draft_blocks_invented_judgment_citation

- [`tests/test_answer_verifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_answer_verifier.py)｜65 行｜`46a38a7756fc`｜test_verify_answer_blocks_false_memory_claim_without_support, test_verify_answer_allows_false_memory_style_phrase_when_chatlog_exists, test_verify_answer_blocks_overclaim_without_evidence, test_verify_answer_allows_overclaim_when_web_evidence_exists

- [`tests/test_archive_wizard_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_archive_wizard_execute.py)｜119 行｜`baa4fd598976`｜test_archive_execute_uses_selected_case_lookup, test_archive_execute_uses_selected_case_lookup.User, test_archive_execute_uses_selected_case_lookup._load, test_archive_execute_uses_selected_case_lookup.fake_exec, test_archive_execute_uses_selected_case_lookup.fake_preview, test_archive_execute_uses_selected_case_lookup.fake_item, test_archive_execute_uses_selected_case_lookup.fake_move, test_archive_copy_falls_back_when_ditto_fails

- [`tests/test_autopilot_child_binding.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_autopilot_child_binding.py)｜97 行｜`76a857ad2562`｜_load, test_child_binding_accepts_current_release_script, test_child_binding_rejects_old_release_path, test_autopilot_inline_children_embed_the_resolved_root, test_daily_reflection_reads_v3_history_and_empty_input_is_safe_wait, test_todo_parser_skips_absurd_relative_duration_without_crashing

- [`tests/test_balthasar_local_empty.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_balthasar_local_empty.py)｜19 行｜`2645fc8cdbfc`｜test_mlx_empty_transcript_is_failure, test_mlx_empty_transcript_is_failure.EmptyWhisper, test_mlx_empty_transcript_is_failure.EmptyWhisper.transcribe

- [`tests/test_browser_security_policy_rc643.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_browser_security_policy_rc643.py)｜111 行｜`1730b8b290c5`｜test_production_browser_sources_never_disable_chromium_sandbox, test_legal_portals_have_dedicated_profiles_and_navigation_allowlists, test_playwright_wrapper_rejects_undeclared_top_level_host, test_sealed_release_blocks_playwright_runtime_install, test_sealed_release_blocks_playwright_runtime_install.forbidden_run, test_browser_health_does_not_auto_install_by_default, test_sealed_selenium_driver_requires_exact_preinstalled_hash, test_sealed_selenium_driver_requires_exact_preinstalled_hash.InertService

- [`tests/test_business_module_live_check.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_business_module_live_check.py)｜3822 行｜`4d3fe1614784`｜_isolate_process_environment, _coverage_portal_receipt, _coverage_download_receipt, test_drive_chunk_receipt_is_operational_wait_not_full_cycle_success, test_drive_chunk_deadline_needs_scheduler_retry_and_never_advances_cursor, test_drive_chunk_with_mixed_unverified_pending_stays_blocking, test_business_live_check_redacts_sensitive_tails_and_samples, test_laf_retry_snapshot_hides_expired_history_and_translates_active_reasons

- [`tests/test_case_display_authoritative_name.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_case_display_authoritative_name.py)｜38 行｜`d410f447bb84`｜_record, test_laf_master_name_is_not_rewritten_by_folder_variant, test_variant_normalisation_is_lookup_only, test_folder_name_only_fills_missing_or_unusable_master, test_laf_email_party_spelling_is_preserved_verbatim

- [`tests/test_case_number_auto_reconcile.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_case_number_auto_reconcile.py)｜128 行｜`12b3f0e6f5b7`｜test_compact_portal_case_numbers_are_normalized, test_filename_date_is_not_misread_as_case_number, test_review_folder_substantive_number_beats_old_investigation_notice, test_update_decision_auto_upgrades_but_never_downgrades, test_same_stage_or_cross_case_conflict_requires_confirmation, test_padding_does_not_create_false_conflict, test_same_current_number_is_not_escalated_for_duplicate_owner, test_remanded_appeal_is_never_replaced_by_older_attached_civil_number

- [`tests/test_case_statistics_tool.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_case_statistics_tool.py)｜99 行｜`5a9eac6c1923`｜test_office_laf_criminal_count_is_deterministic_and_external_free, test_office_laf_criminal_count_is_deterministic_and_external_free.fake_exec, test_office_case_count_scope_can_limit_to_active, test_office_case_count_scope_can_limit_to_active.fake_exec, test_all_scope_wins_when_followup_mentions_both_statuses, test_all_scope_wins_when_followup_mentions_both_statuses.fake_exec, test_case_statistics_take_direct_database_route, test_case_statistics_take_direct_database_route.FakeOrchestrator

- [`tests/test_clarification_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_clarification_gate.py)｜261 行｜`d41739b9bd61`｜_Orchestrator, _Orchestrator.__init__, _Orchestrator._append_route_trace, _Orchestrator._append_history, _Orchestrator._sanitize_incoming_message, _Orchestrator.has_recent_attachment_followup, test_case_count_with_ambiguous_scope_requires_clarification, test_explicit_case_count_scope_does_not_ask_again

- [`tests/test_content_quality_hardening_rc390.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_content_quality_hardening_rc390.py)｜383 行｜`a3e84cb003a4`｜_load_module, _complete_draft, test_strict_draft_quality_accepts_complete_source_grounded_pleading, test_strict_draft_quality_blocks_ungrounded_statute_and_placeholder, test_strict_draft_quality_blocks_citation_lock_violation, test_strict_draft_quality_requires_both_parties, test_strict_draft_quality_blocks_invented_and_missing_factual_anchors, test_draft_frontend_round_trips_context_and_server_quality_state

- [`tests/test_context_labels.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_context_labels.py)｜141 行｜`52660898d87f`｜test_verified_high_confidence, test_user_rule_is_verified, test_user_chat_is_user_stated, test_chatlog_user_is_user_stated, test_summary_derived_is_derived, test_assistant_generated_is_derived, test_derived_from_field_makes_derived, test_moderate_confidence_is_retrieved

- [`tests/test_controlled_autonomy_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_controlled_autonomy_policy.py)｜61 行｜`730634942aca`｜_engine, test_compact_agent_tools_never_expose_persistent_memory_write, test_react_blocks_persistent_tool_even_if_manually_injected, test_react_allows_read_only_skill_and_blocks_persistent_skill_task, test_react_blocks_pdf_rewrite_but_allows_filename_proposal

- [`tests/test_controlled_autonomy_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_controlled_autonomy_runtime.py)｜296 行｜`81ecc0a1511f`｜_service, test_mutable_plan_survives_new_service_and_requires_exact_bound_token, test_repeated_identical_request_rotates_token_without_duplicate_plan, test_handoff_receipt_is_hash_only_and_does_not_claim_business_completion, test_expired_or_cancelled_plan_never_dispatches, test_command_parser_is_exact_and_confirmation_returns_dispatch_lease, test_pipeline_confirmation_replays_only_registered_flow_and_persists_receipt, test_pipeline_confirmation_replays_only_registered_flow_and_persists_receipt._registered_flow

- [`tests/test_cookie_cutter_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_cookie_cutter_blueprint.py)｜1149 行｜`a25f9cb5e0c1`｜_spawn_cookie_child_with_failed_setrlimit, _spawn_cookie_child_with_failed_setrlimit.reject_limit, _spawn_cookie_child_with_failed_setrlimit.reject_generation, _png, _face_png, _empty_frame_png, _synthetic_bundle, _summary_fixture

- [`tests/test_cookie_stl.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_cookie_stl.py)｜510 行｜`0ea988856699`｜_face, _rounded_face, _closed_details, _empty_closed_frame, _frame_with_attached_spoke, _circular_symbol_line_art, _randomized_closed_line_art, _stl_metrics

- [`tests/test_cookie_video_hotfix_sidecar.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_cookie_video_hotfix_sidecar.py)｜69 行｜`93ae1980650b`｜_module, test_sidecar_registers_only_public_hotfix_surfaces, test_sidecar_bound_sources_are_regular_and_hash_complete, test_exam_tutor_hotfix_changes_only_the_example_alias

- [`tests/test_court_hearing_reminder_completion.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_court_hearing_reminder_completion.py)｜211 行｜`612595139162`｜_load_module, _todo, _Connection, _Connection.__init__, _Connection.close, test_structured_case_number_reason_and_client_are_and_terms, test_discord_fast_path_extracts_both_supported_completion_forms, test_discord_fast_path_preserves_completion_type_for_db_filter

- [`tests/test_cron_schedule_stagger_rc230.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_cron_schedule_stagger_rc230.py)｜163 行｜`e7c39ccc327c`｜_by_id, _generated_jobs, _bound_runtime_jobs, test_profile_guard_stays_every_fifteen_minutes_without_colliding_with_resummary, test_schedule_evidence_bindings_follow_the_staggered_cron_snapshot, test_drive_all_files_scan_does_not_reintroduce_the_five_megabyte_ceiling, test_laf_nightly_audit_overwrites_stale_portal_timeout

- [`tests/test_daily_self_evolution.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_daily_self_evolution.py)｜175 行｜`ac4310b6f65c`｜_NoChangeAutoSkill, _NoChangeAutoSkill.__init__, _NoChangeAutoSkill.import_toolsai_auto_skill, _ImprovedAutoSkill, _ImprovedAutoSkill.__init__, _ImprovedAutoSkill.import_toolsai_auto_skill, test_daily_evolution_reports_honest_zero_without_repository_link, test_daily_evolution_counts_persisted_one_percent_gain

- [`tests/test_dashboard_pages_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_dashboard_pages_blueprint.py)｜857 行｜`d8853a7557e0`｜_User, _User.__init__, _make_app, _make_app._load_user, test_tailscale_serve_uses_launchd_safe_absolute_cli, test_tailscale_serve_uses_launchd_safe_absolute_cli.Result, test_tailscale_serve_uses_launchd_safe_absolute_cli.fake_run, test_redirect_routes_point_to_existing_page_targets

- [`tests/test_deadline_reminder_insight_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_deadline_reminder_insight_gate.py)｜102 行｜`d5e8d052d902`｜_load_module, _usable_debt_relief_summary, _usable_debt_relief_source, test_deadline_notice_rejects_extractive_and_cross_domain_rows, test_deadline_notice_has_no_generic_empty_reason_fallback

- [`tests/test_debt_lawyer_contact.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_debt_lawyer_contact.py)｜463 行｜`730faa6e4b5d`｜_isolate_lawyer_contact_environment, _all_text, test_contact_resolution_prefers_payload_then_public_environment, test_contact_resolution_uses_shared_debt_setting_precedence, test_contact_resolution_uses_shared_debt_setting_precedence.profile, test_requested_name_only_does_not_load_unused_profile_fields, test_requested_name_only_does_not_load_unused_profile_fields.profile, test_demo_lawyer_and_bare_client_mobile_are_not_contact_overrides

- [`tests/test_debt_robot_source_modules.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_debt_robot_source_modules.py)｜128 行｜`c2439f01859c`｜test_debt_robot_source_bundle_is_complete, test_six_debt_robot_modules_generate_outputs

- [`tests/test_deep_task_control_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_deep_task_control_rc568.py)｜140 行｜`56e5852792b9`｜test_admission_is_local_and_escalates_disagreement, test_controller_switches_one_task_and_restores_day, test_controller_defers_while_transaction_is_active_without_switching, test_controller_rolls_back_after_work_failure_without_losing_cleanup, test_controller_refuses_unhealthy_night_and_restores_day, test_controller_serializes_two_deep_tasks, test_controller_serializes_two_deep_tasks.first, test_runtime_gate_fails_closed_without_or_with_stale_evidence

- [`tests/test_document_reader.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_document_reader.py)｜300 行｜`2f750bdc712a`｜TestTextQualityScore, TestTextQualityScore.test_legal_chinese_text_scores_high, TestTextQualityScore.test_legal_english_text_scores_high, TestTextQualityScore.test_empty_text_scores_zero, TestTextQualityScore.test_garbled_text_scores_low, TestTextQualityScore.test_short_meaningful_text, TestIsMeaningful, TestIsMeaningful.test_meaningful

- [`tests/test_drive_case_sync_hash_timeout.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_drive_case_sync_hash_timeout.py)｜620 行｜`af0317067185`｜_case_folder, _file_entry, test_case_scan_stops_after_first_storage_hash_timeout, test_case_scan_stops_after_first_storage_hash_timeout.timeout_once, test_hash_storage_dropout_remains_deferred_with_normal_sync_backlog, test_all_case_chunks_fairly_cover_a_cycle_without_repeating_cursor, test_all_case_cursor_does_not_advance_for_failed_chunk, test_inner_budget_reserves_terminal_headroom

- [`tests/test_drive_large_resumable_upload_rc641.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_drive_large_resumable_upload_rc641.py)｜83 行｜`75df81a02ea9`｜_plan, test_oversized_first_item_is_classified_instead_of_zero_attempt_retry, test_resumable_large_file_within_rc641_envelope_is_attempted, test_resumable_large_file_within_rc641_envelope_is_attempted.staged

- [`tests/test_drive_sync_identical_aliases_rc245.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_drive_sync_identical_aliases_rc245.py)｜63 行｜`db5da49234db`｜_drive_entry, test_identical_drive_aliases_do_not_block_sync, test_different_content_in_alias_folders_remains_fail_closed, test_unverifiable_aliases_remain_fail_closed

- [`tests/test_durable_deep_delivery_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_durable_deep_delivery_rc568.py)｜50 行｜`c03247831264`｜_Controller, _Controller.run, test_cross_process_result_is_recipient_bound_case_normalized_and_once, test_corrupt_existing_outbox_fails_closed_without_erasing_it

- [`tests/test_exam_tutor_trend_source_monitor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_exam_tutor_trend_source_monitor.py)｜209 行｜`bd5780a73ffd`｜_load, _config, test_monitor_checks_every_configured_source_and_keeps_failed_refresh_visible, test_source_fingerprint_ignores_view_counter_but_keeps_legal_dates, test_monitor_throttles_heavy_rebuild_but_not_four_hour_source_check, test_scheduled_sync_loads_only_hash_bound_nvidia_settings, test_live_feed_exposes_safe_freshness_without_local_paths, test_config_and_page_make_automatic_all_source_monitoring_explicit

- [`tests/test_export_docx_security.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_export_docx_security.py)｜396 行｜`a5e463d089ec`｜_reload_export_docx, test_export_bilingual_docx_rejects_path_traversal_filename, test_export_summary_docx_rejects_absolute_filename, test_export_transcript_docx_rejects_non_docx_filename, test_node_module_failure_uses_valid_python_docx_fallback, test_node_success_remains_preferred_over_python_fallback, test_node_success_remains_preferred_over_python_fallback.successful_node, test_available_docx_generator_satisfies_structural_contract

- [`tests/test_export_public_urls.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_export_public_urls.py)｜112 行｜`a8387a56b865`｜test_export_text_publishes_authenticated_web_route, test_existing_export_file_uses_authenticated_web_route, test_non_export_static_asset_keeps_static_route, test_shared_export_helper_uses_authenticated_web_route, test_authenticated_export_route_serves_shared_export_dir

- [`tests/test_file_review_auto_worker.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_file_review_auto_worker.py)｜28 行｜`f06914dfc909`｜test_portal_defer_uses_bounded_early_retry_and_backoff, test_non_deferred_cycle_resets_retry_and_keeps_normal_cadence

- [`tests/test_file_review_login_readiness.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_file_review_login_readiness.py)｜94 行｜`ed02311c0b11`｜_Clock, _Clock.monotonic, _Clock.sleep, _SwitchTo, _SwitchTo.__init__, _SwitchTo.default_content, _SwitchTo.frame, _Driver

- [`tests/test_file_review_notifications.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_file_review_notifications.py)｜5537 行｜`5948a9d4a37c`｜_load_action_module, _portal_receipt, _download_signature_receipt, test_portal_download_receipt_is_alias_stable_and_pii_free, test_portal_signature_raw_public_aliases_match_when_each_alias_is_singular, test_portal_signature_uses_stable_result_before_renderer_specific_row_text, test_portal_signature_falls_back_to_row_text_only_without_result, test_portal_signature_uses_c60yyidno_as_fallback_identity_without_rowid_or_no

- [`tests/test_forensic_transcript_live_command.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_forensic_transcript_live_command.py)｜210 行｜`ecc5ad030d27`｜_load_live_runtime, _context, test_discord_single_word_command_is_exact_and_admin_guarded, _fixture_manifest, test_live_start_preflights_full_timeline_and_forces_court_contract, test_live_start_preflights_full_timeline_and_forces_court_contract.FakePopen, test_live_start_preflights_full_timeline_and_forces_court_contract.FakePopen.__init__, test_live_start_rejects_relaxed_secondary_asr

- [`tests/test_forensic_transcript_verifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_forensic_transcript_verifier.py)｜1265 行｜`2d4a36299b26`｜_load_engine, _load_soffice_helper, test_soffice_helper_resolves_known_binary_when_path_is_minimal, test_baseline_entries_can_match_one_semantically_merged_same_speaker_turn, test_baseline_text_cannot_match_an_unrelated_turn_elsewhere, test_speakerless_baseline_header_keeps_following_line_as_text, test_unresolved_items_section_is_not_appended_to_last_spoken_turn, test_cross_speaker_overlap_is_flagged_but_clock_reset_is_a_new_block

- [`tests/test_gemma_distill_schedule_semantics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_gemma_distill_schedule_semantics.py)｜36 行｜`964bbe452e53`｜test_rejected_candidate_is_visible_terminal_deferral_not_success

- [`tests/test_generation_quality_failclosed_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_generation_quality_failclosed_rc568.py)｜62 行｜`9f0f3bc1836b`｜test_translation_rejects_collapsed_source_paragraphs_even_when_anchors_survive, test_translation_accepts_preserved_short_chinese_paragraphs, test_transcript_rejects_generated_text_when_recognizer_returned_nothing, test_formal_summary_requires_extractable_source_for_claim_verification, test_strict_draft_export_fails_closed_without_grounding_text

- [`tests/test_generative_quality_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_generative_quality_live.py)｜171 行｜`4311b49d864f`｜test_generative_quality_cli_loads_bound_live_environment, test_generative_quality_certifies_content_not_just_nonempty_output, test_generative_quality_rejects_fluent_but_factually_wrong_draft, test_generative_quality_rejects_non_nvidia_translation_mislabeled_as_nim, test_generative_quality_rejects_nvidia_provider_with_local_model, test_generative_quality_rejects_local_draft_mislabeled_by_caller, test_generative_quality_rejects_summary_without_verified_pii_scrub

- [`tests/test_golem_console.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_golem_console.py)｜161 行｜`b22ce2cf4e99`｜_User, _User.__init__, _make_app, _make_app._load_user, test_golem_status_api_reports_process_skills_exports_and_memory, test_golem_command_api_supports_safe_commands, test_golem_operational_apis_do_not_expose_global_data_to_regular_users, test_golem_api_key_status_masks_secret

- [`tests/test_grounded_ai_heavy_fail_closed.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_grounded_ai_heavy_fail_closed.py)｜39 行｜`383100302600`｜test_heavy_chat_does_not_silently_fall_back_when_nim_fails, test_heavy_chat_does_not_silently_fall_back_when_nim_disabled

- [`tests/test_heavy_translation_quality_live.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_heavy_translation_quality_live.py)｜187 行｜`9540554d56d6`｜test_live_route_probe_requires_correct_taiwan_term, test_live_route_probe_requires_correct_taiwan_term.Gateway, test_live_route_probe_requires_correct_taiwan_term.Gateway.__init__, test_live_route_probe_requires_correct_taiwan_term.Gateway.chat, test_generated_heavy_translation_fixture_is_extractable, test_gate_accepts_verified_taiwan_renderings_without_forcing_english_into_target, test_run_gate_does_not_read_directory_when_docx_export_fails, test_run_gate_fails_closed_when_exported_docx_cannot_be_stably_read

- [`tests/test_input_method_watchdog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_input_method_watchdog.py)｜119 行｜`f40a96d05bf9`｜test_input_method_watchdog_keeps_normal_process_healthy, test_input_method_watchdog_requires_consecutive_strikes, test_input_method_watchdog_tracks_missing_process_as_a_strike, test_input_method_watchdog_recovers_missing_candidate_services, test_input_method_watchdog_records_whether_candidate_window_is_expected, test_input_method_watchdog_does_not_treat_intentional_us_source_as_process_failure, test_input_method_main_one_shot_selects_before_check

- [`tests/test_install_omlx_text.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_install_omlx_text.py)｜28 行｜`d87ae56a5b5f`｜test_normal_runtime_does_not_bind_release_python, test_unified_runtime_binds_explicit_python

- [`tests/test_intent_tool_adversarial_rc556.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_intent_tool_adversarial_rc556.py)｜229 行｜`40e0979d69ae`｜test_explicit_weather_actions_require_authoritative_realtime, test_weather_words_without_current_lookup_do_not_trigger_realtime, test_stock_actions_require_realtime, test_stock_vocabulary_without_quote_request_does_not_trigger_realtime, test_fx_actions_require_realtime, test_fx_vocabulary_without_quote_request_does_not_trigger_realtime, test_current_time_actions_use_deterministic_clock, test_time_vocabulary_without_clock_request_does_not_trigger_clock

- [`tests/test_intent_tool_grounding_rc555.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_intent_tool_grounding_rc555.py)｜213 行｜`df12fb57be2a`｜test_keywords_do_not_steal_unrelated_questions, test_realtime_and_capability_intents_are_distinct, test_missing_weather_location_asks_smallest_question_instead_of_guessing, test_current_facts_stay_required_even_with_memory, test_compound_request_requires_every_authoritative_source, test_compound_current_news_cannot_be_grounded_by_unrelated_tool, test_react_registry_has_authoritative_realtime_tool, test_react_coerces_generic_weather_search_to_authoritative_source

- [`tests/test_judgment_mcp_gap_fill_rc634.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_judgment_mcp_gap_fill_rc634.py)｜158 行｜`5022a7a677eb`｜_load, _Connection, _Connection.close, test_cached_jdoc_failures_are_not_misreported_as_completed, test_pipeline_reports_source_pull_debt_separately_from_local_backlog, test_existing_daily_crawl_schedule_enables_bounded_mcp_gap_fill_without_new_job, test_dispatch_policy_binds_the_reviewed_single_job_cron_source, test_mcp_gap_fill_stores_only_strict_official_fulltext_and_rebuilds_summary

- [`tests/test_judgment_nvidia_source_bound_rc194.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_judgment_nvidia_source_bound_rc194.py)｜268 行｜`bf4d12f90b8b`｜_mock_provider, _mock_provider._run_nim_chat, test_nvidia_selects_ids_but_stored_text_is_exact_source, test_selector_contract_requires_application_for_bare_statute_rule, test_bare_statute_selection_recovers_source_bound_application, test_bare_statute_selection_without_application_is_terminal_no_insight, test_bare_statute_selection_without_application_is_terminal_no_insight.forbidden_provider, test_doctrinal_rule_without_application_still_reaches_selector

- [`tests/test_judgment_staged_backfill_rc223.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_judgment_staged_backfill_rc223.py)｜82 行｜`a442486ff2e9`｜test_caption_issue_canonicalization_does_not_store_party_names, test_procedural_issue_requires_the_procedural_rule_not_underlying_offence, test_sentence_aggregation_synonyms_restore_source_bound_rule, test_relevant_statute_without_ocr_paragraph_lead_is_still_candidate, test_nvidia_queue_backoff_is_durable_and_bounded, test_provider_capacity_reports_durable_daily_budget_not_scheduler_theory

- [`tests/test_judgment_summary_quality_rc170.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_judgment_summary_quality_rc170.py)｜211 行｜`82299d56769d`｜test_source_bound_summary_contains_issue_rule_application_and_outcome, test_practice_ready_gate_rejects_trial_rule_without_case_application, test_fact_only_material_is_not_promoted_to_practical_insight, test_generic_third_instance_boilerplate_does_not_masquerade_as_damages_rule, test_procedural_rule_remains_usable_when_procedure_is_the_actual_issue, test_template_and_prompt_echo_are_rejected_even_when_long, test_old_fast_digest_is_not_promoted_even_if_one_rule_looks_useful, test_source_support_gate_rejects_fluent_hallucinated_rule

- [`tests/test_judicial_summary_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_judicial_summary_quality.py)｜203 行｜`cba8caa3435e`｜_load_judgment_module, _load_judicial_archive_module, test_case_reason_does_not_rescue_pure_fee_order, test_daily_crawl_distinguishes_upstream_outage_from_program_failure, test_skill_wrapper_failure_preserves_inner_http_error, test_extractive_summary_rejects_fact_fragment_and_keeps_grounded_reason, test_backlog_notice_distinguishes_round_result_from_project_completion, test_day_process_fails_closed_before_marking_files_when_db_is_unavailable

- [`tests/test_laf_case_classifier.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_case_classifier.py)｜49 行｜`fb4dbc3b5cbf`｜test_criminal_service_labels_are_split_into_type_and_stage, test_normalized_fields_preserve_substantive_reason, test_folder_builder_never_leaks_laf_criminal_service_label

- [`tests/test_laf_case_storage_authority.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_case_storage_authority.py)｜145 行｜`6f6872233bef`｜_builder, test_authoritative_storage_requires_real_mount_record, test_external_volume_is_storage_evidence_but_not_active_nas_write, test_file_provider_and_user_mount_are_never_authoritative, test_folder_builder_selects_only_authoritative_smb, test_folder_builder_fails_closed_when_only_file_provider_exists, test_folder_builder_returns_canonical_only_after_authoritative_creation, test_folder_builder_rejects_canonical_mapping_drift

- [`tests/test_laf_gmail_dispatch_scan.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_gmail_dispatch_scan.py)｜702 行｜`6a9303093aaf`｜_load_scan_module, test_callback_failure_is_not_processed_success, test_v3_bound_output_paths_override_legacy_cli_paths, _case_info, _FakeDurableDB, _FakeDurableDB.__init__, _FakeDurableDB.check_laf_email_exists, _FakeDurableDB.add_laf_email_record

- [`tests/test_laf_gmail_spam_restore.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_gmail_spam_restore.py)｜289 行｜`03d36ef2728f`｜_FakeRequest, _FakeRequest.__init__, _FakeRequest.execute, _FakeMessages, _FakeMessages.__init__, _FakeMessages.list, _FakeMessages.get, _FakeMessages.modify

- [`tests/test_laf_portal_new_files_scan.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_portal_new_files_scan.py)｜899 行｜`e01217427685`｜_synthetic_windows_path, _synthetic_posix_path, test_get_db_skips_lazy_profile_without_case_inventory, test_get_db_skips_lazy_profile_without_case_inventory.FakeDb, test_get_db_skips_lazy_profile_without_case_inventory.FakeDb.__init__, test_get_db_skips_lazy_profile_without_case_inventory.FakeDb.fetch_one, test_run_portal_new_files_scan_uses_portal_case_fetch, test_run_portal_new_files_scan_uses_portal_case_fetch.FakeDb

- [`tests/test_laf_portal_retry_decoupling.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_portal_retry_decoupling.py)｜248 行｜`b4c40d17f0e0`｜test_portal_retry_once_writes_terminal_heartbeat, test_download_listing_finds_table_inside_frame, test_download_listing_finds_table_inside_frame.Switch, test_download_listing_finds_table_inside_frame.Switch.__init__, test_download_listing_finds_table_inside_frame.Switch.frame, test_download_listing_finds_table_inside_frame.Switch.parent_frame, test_download_listing_finds_table_inside_frame.Switch.default_content, test_download_listing_finds_table_inside_frame.Driver

- [`tests/test_laf_portal_retry_heartbeat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_portal_retry_heartbeat.py)｜120 行｜`a429137b4a4b`｜test_portal_retry_heartbeat_is_public_safe_and_atomic, test_portal_retry_watchdog_keeps_running_heartbeat, test_portal_retry_watchdog_times_out_stuck_cycle, test_missing_folder_manual_review_is_recoverable, test_expired_portal_attachment_retry_is_not_pending, test_timezone_aware_portal_retry_expiry_is_comparable, test_portal_retry_expiry_accepts_mixed_legacy_and_aware_clock_shapes

- [`tests/test_laf_portal_retry_reconciliation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_laf_portal_retry_reconciliation.py)｜1220 行｜`4b6da28f05a0`｜_email_case, test_generic_portal_reference_is_not_a_download_instruction, test_explicit_portal_download_instruction_is_detected, test_report_transfer_subject_does_not_claim_download_before_body_evidence, test_transfer_confirmation_routes_without_portal_download, test_transfer_confirmation_with_explicit_download_routes_to_result_download, test_official_review_result_subject_without_download_instruction_is_notice_only, test_official_review_result_with_explicit_download_instruction_routes_to_download

- [`tests/test_legal_insight_provenance_gate_rc643.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_legal_insight_provenance_gate_rc643.py)｜263 行｜`8f0255edf1ad`｜_judgment_text, _web_row, _load_sync_module, _load_quarantine_module, test_official_judgment_url_is_exact, test_web_fetch_requires_official_case_matching_raw_text, test_known_router_contamination_is_rejected_even_with_fabricated_provenance, test_human_reviewed_and_native_rows_have_separate_explicit_contracts

- [`tests/test_legaltech_taiwan_law_mcp_rc512.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_legaltech_taiwan_law_mcp_rc512.py)｜356 行｜`3c06bfc2180b`｜_Response, _Response.__init__, _Response.__enter__, _Response.__exit__, _Response.geturl, _Response.read, _envelope, test_remote_judgment_client_binds_jid_official_url_and_remains_candidate

- [`tests/test_live_gate_deployment_paths.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_live_gate_deployment_paths.py)｜228 行｜`485706b88e16`｜test_commercial_schedule_fixture_branch_is_explicitly_host_independent, test_smoke_accepts_bound_runtime_and_environment_for_installed_release, test_smoke_loads_hash_bound_external_environment, test_smoke_rejects_drifted_external_environment, test_minimal_ready_payload_is_valid_but_explicit_false_is_not, test_production_backup_directory_uses_mutable_shared_state, test_production_backup_directory_uses_mutable_shared_state.Backup, test_production_backup_directory_uses_mutable_shared_state.Backup.run_list

- [`tests/test_local_deep_queue_worker_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_local_deep_queue_worker_rc568.py)｜73 行｜`dae51c9bccb9`｜_Controller, _Controller.__init__, _Controller.run, _receipts, test_missing_secure_reference_is_deferred_not_completed, test_worker_executes_one_secure_reference_through_controlled_controller, test_runtime_gate_deferral_never_executes_task, test_runtime_gate_deferral_never_executes_task._Blocked

- [`tests/test_local_model_champion_eval_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_local_model_champion_eval_rc568.py)｜29 行｜`8edbdd90d404`｜_rows, test_score_requires_all_contract_areas, test_runner_is_offline_and_requires_challenger_gate, test_latency_memory_and_crash_thresholds_block_promotion

- [`tests/test_lottery_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_lottery_blueprint.py)｜92 行｜`006384b5438e`｜_make_app, test_lottery_page_is_public, test_lottery_csv_draw_masks_sensitive_fields, test_lottery_xlsx_draw_count_can_be_specified, test_lottery_rejects_too_many_winners

- [`tests/test_magi_agent_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_magi_agent_gateway.py)｜255 行｜`bba4452609d2`｜_config, _Response, _Response.__init__, _Response.__enter__, _Response.__exit__, _Response.read, test_gateway_config_rejects_embedded_credentials_and_missing_key, test_published_tools_are_fixed_and_only_confirm_can_write

- [`tests/test_magi_encyclopedia_privacy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_magi_encyclopedia_privacy.py)｜84 行｜`bd24e707917c`｜test_pdf_builder_uses_reportlab_without_office_or_browser_processes, test_pdf_builder_recurses_through_pandoc_section_containers, test_source_index_signature_redacts_posix_workstation_default, test_source_index_metadata_redacts_user_and_windows_paths, test_source_index_redaction_keeps_non_path_contract_text, test_source_links_use_immutable_commit_not_mutable_release_branch

- [`tests/test_market_briefing_quality_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_market_briefing_quality_gate.py)｜99 行｜`e234f710591b`｜_row, test_quality_gate_rejects_model_that_does_not_beat_constant_baseline, test_quality_gate_accepts_out_of_sample_directional_edge, test_unverified_market_output_is_watch_not_directional_forecast

- [`tests/test_memory_grounding.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_memory_grounding.py)｜216 行｜`fc735dfcf41a`｜test_auto_remember_disabled_by_default, test_auto_remember_requires_explicit_mode, test_memory_ranking_prioritizes_trusted_sources_for_fact_queries, test_memory_ranking_keeps_chatlog_available_for_explicit_recall, test_expand_query_skips_memory_recall_queries, test_expand_query_skips_memory_recall_queries.FakeResponse, test_expand_query_skips_memory_recall_queries.FakeResponse.json, test_expand_query_skips_memory_recall_queries.FakeSession

- [`tests/test_memory_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_memory_policy.py)｜212 行｜`f38cf2e6dcdb`｜test_too_short_content_blocked, test_empty_content_blocked, test_normal_length_passes, test_timeout_fallback_blocked, test_degraded_response_blocked, test_synthetic_fallback_blocked, test_assistant_generated_blocked, test_assistant_chatlog_blocked_by_default

- [`tests/test_message_intent_boundaries.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_message_intent_boundaries.py)｜424 行｜`b0d22da2c90f`｜_BoundaryOrchestrator, _BoundaryOrchestrator.__init__, _BoundaryOrchestrator._append_route_trace, _BoundaryOrchestrator._load_skill_interview_pending, _BoundaryOrchestrator._pending_key, _BoundaryOrchestrator.get_active_heavy_tasks, _BoundaryOrchestrator._brain_runtime_banner, _BoundaryOrchestrator._try_conversational_intent

- [`tests/test_mobile_auth_routes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_mobile_auth_routes.py)｜370 行｜`bcfad938ede3`｜_fake_cursor, _fake_cursor._Cursor, _fake_cursor._Cursor.__init__, _fake_cursor._Cursor.execute, _fake_cursor._Cursor.fetchone, _fake_cursor._Conn, _fake_cursor._Conn.commit, _User

- [`tests/test_model_live_gate_degraded_profiles.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_model_live_gate_degraded_profiles.py)｜84 行｜`ff1030c88329`｜_probe, _night_probes, test_night_e4b_declared_last_resort_is_degraded_not_failed, test_night_e4b_without_declared_degraded_profile_still_fails, test_night_declared_fallback_does_not_hide_unreachable_8080, test_switch_applies_cooldown_to_night_e4b_last_resort, test_switch_preserves_healthy_e4b_under_resource_pressure

- [`tests/test_model_router_deep_queue_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_model_router_deep_queue_rc568.py)｜23 行｜`e90e0272603c`｜test_quality_task_without_heavy_opt_in_queues_local_deep_when_26b_not_live, test_quality_task_uses_live_26b_locally_without_heavy_opt_in

- [`tests/test_nas_pdf_ocr_worker_lock.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_nas_pdf_ocr_worker_lock.py)｜353 行｜`fa9b2cb8c677`｜test_explicit_existing_nas_root_bypasses_production_mount_probe, test_nas_ocr_uses_homebrew_libexec_python, test_nas_pdf_ocr_worker_skips_before_queue_when_lock_busy, test_nas_pdf_ocr_worker_skips_before_queue_when_lock_busy.HeldLock, test_nas_pdf_ocr_worker_skips_before_queue_when_lock_busy.HeldLock.as_dict, test_nas_pdf_ocr_worker_skips_before_queue_when_lock_busy.fail_if_called, test_nas_pdf_ocr_worker_retries_stale_processing_and_archives_without_overwrite, test_nas_pdf_ocr_worker_retries_stale_processing_and_archives_without_overwrite.fake_run

- [`tests/test_natural_language_agent_quality_rc549.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_natural_language_agent_quality_rc549.py)｜202 行｜`6014468e1d0c`｜test_calendar_mutation_is_not_stolen_by_weather_or_stock_routes, test_natural_office_language_reaches_work_or_agent_lanes, test_human_like_clarification_for_vague_or_multiple_targets, test_tool_policy_understands_natural_translation_and_business_status, test_controlled_evolution_questions_use_read_only_ledger_tool, test_evolution_grounding_requires_the_specific_ledger_tool, test_payment_slip_recovery_requires_explicit_action_and_reaches_tool_policy, test_declining_tools_never_downgrades_current_office_facts_to_chat

- [`tests/test_nightly_regression_production_suites.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_nightly_regression_production_suites.py)｜48 行｜`7ce4edd92ccb`｜test_production_default_omits_retired_mock_suite, test_production_default_omits_retired_mock_suite._suite, test_production_default_omits_retired_mock_suite._suite._run, test_missing_retired_mock_fixture_is_neutral

- [`tests/test_obsidian_ingest_checkpoint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_obsidian_ingest_checkpoint.py)｜76 行｜`2139bce312e7`｜test_ingest_state_match_supports_legacy_and_strong_metadata, test_save_ingest_state_merges_and_atomically_replaces, test_hydrate_ingest_state_recovers_durable_note_cursor

- [`tests/test_omlx_switch_exit_semantics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_omlx_switch_exit_semantics.py)｜59 行｜`70c6a47479d2`｜_run_gatekeeper_result, test_gatekeeper_pause_is_deferred_not_success, test_gatekeeper_launcher_failure_is_not_relabelled_as_pause

- [`tests/test_omlx_watchdog_switch_lock.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_omlx_watchdog_switch_lock.py)｜122 行｜`6f0f64cdd90a`｜test_watchdog_respects_switch_lock_and_profile_mtime, _run_watchdog_once, test_watchdog_accepts_night_12b_fallback_without_kickstart, test_watchdog_reports_profile_mismatch_without_kickstart, test_watchdog_reports_unconfigured_live_model_without_kickstart

- [`tests/test_optional_line_health.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_optional_line_health.py)｜60 行｜`b28e3f2e8077`｜_clear_line_environment, test_line_is_disabled_by_default_when_credentials_are_absent, test_line_is_required_only_after_explicit_enable, test_existing_credential_pair_enables_line_when_flag_is_absent, test_explicit_disable_wins_over_existing_credentials

- [`tests/test_osc_address_label.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_address_label.py)｜161 行｜`d686278fc954`｜app, client, _make_exec, _make_exec.fake_exec, test_address_label_route_registered, test_address_label_route_registered.fake_exec, test_address_label_404_when_case_missing, test_address_label_404_when_case_missing.fake_exec

- [`tests/test_osc_backup_endpoints.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_backup_endpoints.py)｜301 行｜`a1b744d3637d`｜app, client, _fake_osc_exec, _fake_osc_exec_with_rows, _fake_osc_exec_with_rows._exec, test_backup_routes_registered, test_backup_create_writes_file, test_backup_create_prunes_to_seven

- [`tests/test_osc_checklists_endpoints.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_checklists_endpoints.py)｜211 行｜`014ac636b423`｜app, client, test_laf_checklist_routes_registered, test_laf_checklist_get_requires_case_number, test_laf_checklist_post_creates_with_auto_key, test_laf_checklist_post_creates_with_auto_key.fake_exec, test_laf_checklist_seed_inserts_defaults, test_laf_checklist_seed_inserts_defaults.fake_exec

- [`tests/test_osc_closed_case_archive.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_closed_case_archive.py)｜794 行｜`de7a5d981a0b`｜test_closed_case_status_detection, test_auto_archive_closed_case_moves_folder, test_auto_archive_closed_case_moves_folder.fake_exec, test_closed_case_user_level_mount_is_allowed_for_browser, test_closed_case_user_level_mount_is_allowed_for_browser.fake_roots, test_user_level_magi_mount_paths_use_timeout_guard, test_auto_archive_closed_case_ignores_legacy_delete_guard, test_auto_archive_closed_case_ignores_legacy_delete_guard.fake_exec

- [`tests/test_osc_csv_import_export.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_csv_import_export.py)｜374 行｜`c031d0dd2dfa`｜app, client, _make_csv, test_cases_import_route_registered, test_cases_import_validates_no_file, test_cases_import_missing_required_header, test_cases_import_success, test_cases_import_success.fake_exec

- [`tests/test_osc_document_reuse_api.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_document_reuse_api.py)｜342 行｜`a323196579c5`｜_build_app, _build_app._User, _build_app._load_user, _make_source_docx, _doc_text, _doc_text.collect, test_reuse_document_api_creates_target_case_word_doc_and_logs, test_reuse_document_api_creates_target_case_word_doc_and_logs.fake_exec

- [`tests/test_osc_documents_stamp_endpoint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_documents_stamp_endpoint.py)｜342 行｜`e7bf530a5542`｜app, client, test_stamp_route_registered, test_stamp_validates_missing_file_path, test_stamp_validates_copy_type, test_stamp_rejects_nonexistent_file, test_stamp_rejects_unsupported_extension, test_stamp_rejects_unsupported_extension._safe

- [`tests/test_osc_events_refresh_outcome_semantics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_events_refresh_outcome_semantics.py)｜40 行｜`f150701e28c1`｜test_quality_warning_is_partial_but_successful, test_component_failure_remains_terminal_failure, test_warning_free_refresh_is_completed

- [`tests/test_osc_file_frontend_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_file_frontend_runtime.py)｜452 行｜`1fcac361e8f2`｜_find_node_runtime, _run_node, test_pdf_preview_uses_unified_route_and_authenticated_content_url, test_office_and_structured_previews_render_in_shared_modal, test_session_expiry_never_renders_a_fake_preview, test_download_probes_session_before_clicking_real_content_route, test_file_download_error_ui_does_not_render_server_trace_text, test_readonly_api_retries_transient_fetch_failure_without_raw_browser_error

- [`tests/test_osc_files_move.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_files_move.py)｜1583 行｜`1fa205e29241`｜_client, _client.TestUser, _client._load_user, _directory_route_app, _directory_route_app.TestUser, _directory_route_app._load_user, _client_with_role, _client_with_role.TestUser

- [`tests/test_osc_folder_rename.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_folder_rename.py)｜339 行｜`d4599cc38ea0`｜_login_app, _login_app.TestUser, _login_app._load_user, test_case_root_folder_rename_updates_case_path_and_references, test_case_root_folder_rename_updates_case_path_and_references.fake_exec, test_generic_folder_rename_updates_indexed_paths, test_generic_folder_rename_updates_indexed_paths.fake_exec, test_case_folder_reconcile_reports_repair_for_stale_db_path

- [`tests/test_osc_laf_debt_required_checklist.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_laf_debt_required_checklist.py)｜224 行｜`a92c3279fad9`｜_install_flask_login_stub, _build_app, test_debt_required_get_returns_osc_spec_and_laf_number_candidates, test_debt_required_get_returns_osc_spec_and_laf_number_candidates.fake_exec, test_debt_required_save_upserts_visible_items_and_prunes_inactive_debt_rows, test_debt_required_save_upserts_visible_items_and_prunes_inactive_debt_rows.fake_exec, test_laf_number_sync_uses_single_candidate_when_manual_number_is_empty, test_laf_number_sync_uses_single_candidate_when_manual_number_is_empty.fake_exec

- [`tests/test_osc_p2_discord_and_theme.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_p2_discord_and_theme.py)｜304 行｜`8805f6756508`｜app, client, test_discord_test_route_registered, test_discord_test_requires_url, test_discord_test_requires_url.fake_helpers, test_discord_test_requires_url.fake_helpers.fake_exec, test_discord_test_rejects_invalid_url, test_discord_test_success_with_valid_url

- [`tests/test_osc_pdf_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_pdf_blueprint.py)｜1310 行｜`0865532fae97`｜_calendar_source_root, app, client, sample_pdf, _assert_pdf, test_pdf_routes_registered, test_pdf_info, test_pdf_rejects_non_pdf

- [`tests/test_osc_saas_workbench.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_saas_workbench.py)｜918 行｜`5846ae6e3269`｜test_osc_refresh_hard_exit_flushes_before_native_teardown, test_osc_refresh_hard_exit_flushes_before_native_teardown.Stream, test_osc_refresh_hard_exit_flushes_before_native_teardown.Stream.__init__, test_osc_refresh_hard_exit_flushes_before_native_teardown.Stream.flush, test_quality_check_blocks_prompt_leak_and_internal_case_number, test_client_packet_uses_debt_checklist, test_client_packet_uses_debt_checklist.fake_exec, test_conflict_check_flags_opponent_records

- [`tests/test_osc_todos_bulk_complete.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_todos_bulk_complete.py)｜71 行｜`6b3d72badec2`｜_app, _app.User, _app._load, test_bulk_complete_before_updates_stale_open_todos, test_bulk_complete_before_updates_stale_open_todos.fake_exec, test_bulk_complete_before_dry_run_does_not_update, test_bulk_complete_before_dry_run_does_not_update.fake_exec

- [`tests/test_osc_web_draft_hotfix.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_web_draft_hotfix.py)｜192 行｜`23fe410575e2`｜_Operator, test_runtime_exports_are_authorized_without_widening_runtime_root, test_authenticated_web_download_reads_its_own_runtime_export, test_authenticated_web_download_reads_its_own_runtime_export._load_user, test_authenticated_web_download_reads_its_own_runtime_export._login, test_web_pleading_export_keeps_osc_generation_and_office_format_contract

- [`tests/test_osc_web_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_osc_web_smoke.py)｜2260 行｜`2ba1073dcefb`｜_build_app, _build_app._TestUser, _build_app._load_user, app, client, _make_fake_exec, _make_fake_exec._fake, test_dashboard_endpoint_reachable

- [`tests/test_overdue_confirmation_calendar_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_overdue_confirmation_calendar_policy.py)｜180 行｜`f044386b3224`｜_load, test_policy_is_narrow_and_preserves_real_deadlines, test_both_calendar_event_builders_fail_closed_for_osc_only_review, _DeleteCall, _DeleteCall.execute, _Events, _Events.__init__, _Events.delete

- [`tests/test_pdf_cross_case_identity_confirmation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_pdf_cross_case_identity_confirmation.py)｜139 行｜`9a22e2758014`｜_judgment, test_exact_cross_case_copy_is_verified_by_sha256, test_exact_cross_case_copy_is_verified_by_sha256.fake_exec, test_same_filename_with_different_bytes_is_not_a_conflict, test_scan_fails_closed_into_identity_confirmation, test_manual_confirmation_has_no_calendar_date_and_is_idempotent, test_manual_confirmation_has_no_calendar_date_and_is_idempotent.fake_exec

- [`tests/test_pdf_namer_nightly_process_isolation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_pdf_namer_nightly_process_isolation.py)｜180 行｜`c09dd619d278`｜_module, test_nightly_analyze_runs_each_sample_in_bounded_child, test_nightly_analyze_runs_each_sample_in_bounded_child.fake_run, test_nightly_analyze_fails_closed_without_leaking_worker_output, test_nightly_hashes_pdf_in_bounded_chunks, test_nightly_does_not_reopen_nas_pdf_when_analyzer_omits_hash, test_nightly_defers_transient_nas_scan_error, test_nightly_fixture_runs_real_pdf_child_and_writes_bound_manifest

- [`tests/test_pdf_namer_state_isolation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_pdf_namer_state_isolation.py)｜282 行｜`72177e202e82`｜_copy_candidate, _tree_digest, _candidate_env, test_candidate_import_is_side_effect_free_and_prefers_explicit_state, test_v2_without_state_environment_keeps_legacy_skill_directory, test_isolated_runtime_refuses_release_tree_write_target, test_isolated_runtime_rejects_nested_directory_and_file_symlinks, test_v3_writes_and_nightly_dry_run_leave_candidate_tree_immutable

- [`tests/test_process_monitor_unification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_process_monitor_unification.py)｜137 行｜`c199f459dcd6`｜_snapshot, test_rc600_shell_launcher_is_not_a_worker_but_its_unmanaged_child_is_orphaned, test_worker_below_canonical_supervisor_ancestry_is_not_an_orphan, test_direct_init_worker_and_exact_duplicate_group_are_reported_once, test_transient_zombie_requires_same_five_second_persistence_on_shared_contract, test_web_and_menubar_use_identical_snapshot_and_summary, test_web_and_menubar_use_identical_snapshot_and_summary.Done, test_process_monitor_read_failure_is_an_attention_state

- [`tests/test_rc241_runtime_and_batch_regressions.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_rc241_runtime_and_batch_regressions.py)｜278 行｜`e8753905104c`｜_load_transcript_action, test_tool_events_resolve_to_mutable_agent_dir, test_transcript_default_download_dir_is_outside_sealed_release, test_transcript_batch_deduplicates_repeated_download_paths, test_transcript_batch_deduplicates_repeated_download_paths.Downloader, test_transcript_batch_deduplicates_repeated_download_paths.Downloader.cleanup_download_folder, test_transcript_batch_deduplicates_repeated_download_paths.Downloader.login, test_transcript_batch_deduplicates_repeated_download_paths.Downloader.get_cases_from_db

- [`tests/test_rc600_runtime_bootstrap_and_reporting.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_rc600_runtime_bootstrap_and_reporting.py)｜65 行｜`8abe5f35163b`｜test_file_review_action_bootstraps_release_root_without_pythonpath, test_self_repair_reporter_maps_legacy_job_and_hides_trace_codes

- [`tests/test_reconcile_overdue_todos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_reconcile_overdue_todos.py)｜172 行｜`5a95416a2a66`｜_load_module, test_calendar_past_proceeding_is_archived_not_escalated, test_past_proceeding_type_is_completed_even_without_calendar_source, test_actionable_duty_is_not_hidden_by_calendar_hearing_wording, test_existing_overdue_confirmation_for_past_proceeding_is_reconciled, test_existing_document_overdue_confirmation_recovers_original_proceeding_type, test_real_pure_occurrence_with_google_event_keeps_calendar_history, test_archived_calendar_occurrence_keeps_google_event_as_history

- [`tests/test_repair_fileprovider_case_to_nas.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_repair_fileprovider_case_to_nas.py)｜192 行｜`1095c3416f2e`｜_fixture_tree, _identity, test_dry_run_seals_manifest_without_creating_destination, test_apply_copies_exact_tree_preserves_source_and_writes_pii_free_receipt, test_apply_rejects_manifest_drift_before_copy, test_apply_never_overwrites_existing_destination, test_source_change_during_copy_fails_and_cleans_owned_stage, test_source_change_during_copy_fails_and_cleans_owned_stage._copy_then_mutate

- [`tests/test_saas_commercial_foundations.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_saas_commercial_foundations.py)｜228 行｜`cfb65755bdd3`｜test_durable_rate_limit_is_shared_between_instances_and_resets, test_durable_rate_limit_stores_no_raw_client_identity, test_rate_limit_storage_failure_is_fail_closed_when_required, test_rate_limit_readiness_verifies_database_not_only_filename, test_audit_chain_seals_legacy_prefix_and_redacts_sensitive_values, test_audit_chain_concurrent_append_has_one_contiguous_sequence, test_audit_tampering_is_detected_and_future_append_fails_closed, test_protected_mutations_receive_start_and_completion_receipts

- [`tests/test_saas_readiness_migration.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_saas_readiness_migration.py)｜44 行｜`a892296bf327`｜test_formal_saas_release_contains_versioned_tenant_migration

- [`tests/test_safe_process.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_safe_process.py)｜896 行｜`5f232ee577cc`｜_trusted_test_venv, _reset_sem, test_argv_head_whitelisted_python3, test_versioned_python3_executable_is_whitelisted, test_windows_python_executable_is_whitelisted, test_absolute_python_alias_for_current_interpreter_is_whitelisted, test_relative_or_foreign_python_alias_is_rejected, test_current_python_alias_keeps_shell_metachar_guards

- [`tests/test_selfhost_portability.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_selfhost_portability.py)｜764 行｜`13c4af3e5623`｜test_windows_required_runtime_modules_have_portable_imports, test_portable_file_lock_is_exclusive_and_releasable, _config, _source, test_windows_layout_is_native_and_machine_independent, test_cross_platform_windows_plan_uses_windows_python_launcher, test_macos_layout_selects_apple_local_backends, test_render_environment_disables_apple_only_paths_on_windows

- [`tests/test_selfhost_release_smoke.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_selfhost_release_smoke.py)｜19 行｜`b5b37c039338`｜test_native_release_smoke_proves_package_upgrade_rollback_and_tamper_detection

- [`tests/test_sentencing_trend_chat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_sentencing_trend_chat.py)｜195 行｜`93b048e9ef86`｜_result, test_chat_parser_extracts_full_filters_and_roc_year_range, test_chat_parser_normalises_court_alias_and_keeps_long_offence, test_chat_parser_supports_simple_offence_and_clarifies_missing_filters, test_chat_parser_rejects_reversed_period_without_guessing, test_chat_formatter_reports_provenance_statistics_and_official_source, test_display_date_uses_full_taiwanese_roc_format_without_changing_invalid_values, test_chat_formatter_never_promotes_unverified_mcp_candidate

- [`tests/test_sentencing_trends.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_sentencing_trends.py)｜672 行｜`f7f2cea1e544`｜_row, test_parser_uses_signature_judge_and_separates_execution_sentence, test_parser_replaces_time_windowed_download_api_with_stable_official_page, test_official_page_url_rejects_untrusted_or_time_windowed_fallback_without_jid, test_panel_judges_keep_participants_and_highlight_last_listed_judge, test_judge_filter_defaults_to_last_listed_but_can_search_any_participant, test_date_filters_accept_picker_iso_and_reject_partial_or_reversed_values, test_parser_includes_appendix_sentences_but_excludes_missing_appendix

- [`tests/test_shared_party_case_identity_rc643.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_shared_party_case_identity_rc643.py)｜246 行｜`e2144e5ca868`｜_row, _case, _load_smart_filer, _index_entry, test_db_peer_probe_detects_same_visible_drive_namespace, test_same_drive_folder_cannot_match_multiple_nas_cases, test_single_case_direct_worker_blocks_when_db_peer_shares_namespace, test_existing_drive_app_property_cannot_be_stolen

- [`tests/test_startup_resource_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_startup_resource_policy.py)｜29 行｜`d63702177d52`｜test_startup_prefetch_is_lazy_by_default, test_startup_prefetch_accepts_explicit_opt_in, test_startup_prefetch_rejects_false_like_values, test_inprocess_laf_gmail_monitor_is_disabled_by_default, test_inprocess_laf_gmail_monitor_requires_explicit_opt_in

- [`tests/test_tailscale_funnel_healthcheck.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_tailscale_funnel_healthcheck.py)｜826 行｜`ca484519fbe0`｜test_app_binary_is_forced_into_documented_cli_mode, test_app_binary_is_forced_into_documented_cli_mode.fake_run, test_official_app_cli_is_preferred_when_capability_probe_passes, test_unusable_official_app_falls_back_to_homebrew, test_configured_audited_cli_has_priority_but_arbitrary_path_is_rejected, test_capability_probe_rejects_version_mismatch, test_local_dns_resolution_fails_closed_without_addresses, test_edge_coverage_requires_every_advertised_public_address

- [`tests/test_telegram_history.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_telegram_history.py)｜22 行｜`04af1db09d02`｜test_telegram_records_assistant_reply_with_orchestrator_history_signature, test_telegram_records_assistant_reply_with_orchestrator_history_signature.Orchestrator, test_telegram_records_assistant_reply_with_orchestrator_history_signature.Orchestrator.process_message, test_telegram_records_assistant_reply_with_orchestrator_history_signature.Orchestrator.record_assistant_reply

- [`tests/test_tool_registry_contracts.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_tool_registry_contracts.py)｜77 行｜`29d17995296b`｜test_schema_rejects_missing_wrong_and_unknown_arguments_before_executor, test_irreversible_tool_rejects_arbitrary_confirmation_token_before_executor, test_irreversible_tool_accepts_only_spec_bound_confirmation_token, test_read_tool_without_schema_or_confirmation_remains_compatible

- [`tests/test_tools_api_async_jobs.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_tools_api_async_jobs.py)｜501 行｜`16da2f5350d4`｜_ok_result, _fail_result, _tools_api_import_stubs, ctx, ctx._AuthClient, ctx._AuthClient.post, ctx._AuthClient.get, ctx._AuthClient.raw

- [`tests/test_tools_api_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_tools_api_runtime.py)｜729 行｜`6e43890db097`｜_read_jsonl, tools_api_runtime, test_search_emits_pre_and_post_events, test_skill_runtime_flags_default_to_non_mutating, test_tools_livez_is_process_only, test_tools_health_requires_reachable_model, test_tools_health_requires_reachable_model._Session, test_tools_health_requires_reachable_model._Session.get

- [`tests/test_tools_api_shortcut_endpoints.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_tools_api_shortcut_endpoints.py)｜193 行｜`7131f6ae9462`｜shortcut_client, test_shortcut_ocr_rejects_missing_api_key, test_shortcut_ocr_rejects_empty_body, test_shortcut_ocr_returns_plaintext_on_success, test_shortcut_ocr_returns_plaintext_on_success._FakeGateway, test_shortcut_ocr_returns_plaintext_on_success._FakeGateway.vision, test_shortcut_pdf_text_rejects_non_pdf, test_shortcut_pdf_text_returns_plaintext

- [`tests/test_transcribe_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_transcribe_runtime.py)｜97 行｜`44e2b26604de`｜test_transcribe_auto_prefers_balthasar_before_apple, test_transcribe_auto_uses_fast_cli_before_balthasar, test_transcript_postprocess_uses_taiwan_legal_term_for_evidence_motion, test_whisper_model_dir_defaults_to_local_magi_cache, test_whisper_model_dir_honors_explicit_local_override, test_whisper_cli_refuses_missing_local_model_before_subprocess

- [`tests/test_transcript_filename_repair.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_transcript_filename_repair.py)｜537 行｜`c70a8930dffb`｜_load_module, _load_repair_module, test_00000000_transcript_is_not_treated_as_final_name, test_transcript_filename_generation_rejects_unusable_parse_values, test_transcript_metadata_receipt_category_is_field_specific_and_privacy_safe, test_transcript_metadata_extracts_record_from_second_page_without_cross_page_mix, test_transcript_metadata_accepts_chinese_numeral_roc_date_on_proven_record_page, test_transcript_metadata_accepts_historical_roc_date_on_same_record_page

- [`tests/test_transcript_partial_retry_rc239.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_transcript_partial_retry_rc239.py)｜581 行｜`f39fb8759090`｜_load_action, _Downloader, _Downloader.__init__, _Downloader.cleanup_download_folder, _Downloader.login, _Downloader.get_cases_from_db, _Downloader.download_record, _case

- [`tests/test_transcript_portal_empty_failclosed_rc223.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_transcript_portal_empty_failclosed_rc223.py)｜391 行｜`6548101a5cc6`｜_Body, _Body.__init__, _Driver, _Driver.__init__, _Driver.find_element, _Driver.find_elements, _Driver.quit, _downloader

- [`tests/test_translation_strict_nim_provenance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_translation_strict_nim_provenance.py)｜67 行｜`ca982c32b84c`｜test_heavy_translation_never_uses_google_fallback, test_heavy_translation_never_uses_google_fallback.forbidden_google, test_heavy_translation_does_not_retry_terminal_provider_failure, test_heavy_translation_does_not_retry_terminal_provider_failure.exhausted

- [`tests/test_tw_output_guard_fidelity.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_tw_output_guard_fidelity.py)｜23 行｜`21dee71e5e92`｜test_tw_legal_review_module_import_has_default_model, test_tw_review_cannot_drop_legal_fact_anchors

- [`tests/test_v3_laf_dedup_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_v3_laf_dedup_compat.py)｜251 行｜`f9f495756246`｜_sha, _source, FakeStore, FakeStore.__init__, FakeStore.validate_schema, FakeStore.acquire_lock, FakeStore.release_lock, FakeStore.begin

- [`tests/test_video_studio_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_video_studio_blueprint.py)｜593 行｜`394fbda2ac9b`｜_app, _payload, test_public_tool_directory_and_video_page_require_no_login, test_public_video_navigation_uses_shared_magi_theme_contract, test_video_studio_defines_matching_day_and_night_palette, test_health_is_public_safe_and_exact, test_edit_command_is_interpreted_and_unknown_or_conflicting_meanings_fail_closed, test_request_contract_rejects_unknown_and_coerced_values

- [`tests/test_web_information_architecture.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_web_information_architecture.py)｜149 行｜`800b7b281690`｜_read, test_desktop_home_prioritises_workflows_over_engineering_controls, test_research_and_sentencing_share_the_same_primary_navigation, test_maintenance_manual_is_self_contained_and_uses_shared_theme_contract, test_maintenance_manual_tables_wrap_without_hiding_cells_on_narrow_screens, test_mobile_home_exposes_direct_work_routes_without_desktop_detour, test_paperclip_deep_links_are_allowlisted_and_shareable, test_mobile_and_desktop_navigation_remain_adaptive_and_keyboard_accessible

- [`tests/test_web_runtime_blueprint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_web_runtime_blueprint.py)｜548 行｜`7a25efce3f97`｜_User, _User.__init__, _Orchestrator, _Orchestrator.__init__, _Orchestrator.process_message, _make_app, _make_app._load_user, test_process_monitor_routes_render_and_toggle

- [`tests/test_weekend_resummary_budget_semantics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/test_weekend_resummary_budget_semantics.py)｜170 行｜`a4419b781d37`｜test_sealed_release_import_defers_mutable_judgment_binding, test_nim_daily_budget_stops_immediately_as_checkpointed_deferral, test_budget_deferral_does_not_hide_a_real_process_failure, test_quality_failure_cannot_be_published_as_batch_success, test_legacy_zero_exit_budget_marker_is_reconcilable_without_deleting_evidence, test_background_authorization_budget_marker_is_a_terminal_deferral, test_utf8_byte_large_but_character_short_sources_are_terminal, test_utf8_byte_large_but_character_short_sources_are_terminal.forbidden_provider

- [`tests/v3/test_a2a_adapter.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_a2a_adapter.py)｜48 行｜`ed319b070e1f`｜test_shipped_a2a_policy_is_disabled_proposal_only, test_a2a_never_allows_writer_federation_or_whale, test_enabled_future_adapter_can_only_create_non_dispatching_proposal

- [`tests/v3/test_active_release_input_method_watchdog.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_active_release_input_method_watchdog.py)｜64 行｜`5ed588a4ffe5`｜_fixture, test_active_watchdog_is_manifest_and_hash_bound, test_active_watchdog_rejects_script_drift, test_active_watchdog_rejects_release_outside_store

- [`tests/v3/test_active_release_service_launcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_active_release_service_launcher.py)｜112 行｜`3342610befc1`｜_sha256, _fixture, test_resolves_hash_bound_script_from_active_release, test_rejects_unknown_service_before_reading_marker, test_rejects_script_drift_after_sealing, test_rejects_marker_manifest_hash_drift

- [`tests/v3/test_actual_route_replay.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_actual_route_replay.py)｜1336 行｜`3a376c80fd75`｜test_installed_candidate_root_is_allowed_for_read_only_replay, test_only_direct_installed_release_root_qualifies, test_worker_uses_candidate_venv_with_site_processing_disabled, test_all_347_routes_receive_machine_readable_actual_replay_dispositions, test_actual_handlers_are_dispatched_with_bound_outcomes_not_auth_or_404_shortcuts, test_osc_transactional_crud_batch_dispatches_success_and_journals_exact_sql, test_secondary_osc_write_batch_binds_db_and_file_transcripts, test_next_osc_read_only_batch_uses_actual_handlers_and_select_only_fixture

- [`tests/v3/test_background_heavy_authorization_rc568.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_background_heavy_authorization_rc568.py)｜88 行｜`b99eff8a8dd7`｜_contract, _prepare, test_background_heavy_requires_complete_job_bound_contract, test_background_heavy_rejects_mismatched_or_expired_contract, test_background_heavy_uses_only_named_env_contract, test_credential_guard_blocks_cookie_bearer_and_oauth_without_blocking_case_number

- [`tests/v3/test_backup_prepare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_backup_prepare.py)｜215 行｜`5ef53f54c56f`｜_database, _website, test_online_backup_is_actually_restored_and_hash_bound, test_empty_escape_duplicate_and_existing_output_fail_closed, test_website_symlink_special_scope_and_source_drift_fail_without_success_metadata, test_website_symlink_special_scope_and_source_drift_fail_without_success_metadata.mutate_after_first_copy, test_verify_rejects_archive_tamper_extra_member_and_protected_restore_root

- [`tests/v3/test_business_events.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_business_events.py)｜72 行｜`f14bdd62af6a`｜test_event_is_idempotent_and_contains_no_document_body, test_claim_complete_and_crash_lease_recovery, test_invalid_case_number_fails_closed, test_claim_does_not_consume_observation_events

- [`tests/v3/test_business_outcome_regression.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_business_outcome_regression.py)｜50 行｜`be37782da286`｜_raw, test_manual_finding_is_deidentified_deduplicated_and_test_root_only, test_manual_finding_rejects_pii_before_persistence, test_outcome_slo_requires_receipts_and_handles_cross_case_notification_and_defer

- [`tests/v3/test_business_recovery.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_business_recovery.py)｜672 行｜`deb77c0f69d4`｜test_only_expected_schedule_waits_are_terminal_deferrals, test_legacy_candidate_rejection_is_exact_job_and_strong_evidence_only, test_transient_business_failure_is_bounded_retry, test_explicit_human_requirement_is_not_blindly_retried, test_zero_semantic_collision_counter_does_not_request_a_person, test_false_timeout_field_does_not_mislabel_business_failure, test_final_partial_success_receipt_outranks_caught_item_traceback, test_unhandled_traceback_without_final_success_receipt_still_fails

- [`tests/v3/test_campaign_offline_probes.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_campaign_offline_probes.py)｜336 行｜`c0696ef749ae`｜test_schedule_count_is_bound_to_dispatch_policy_not_stale_probe_literals, test_missing_duration_profile_uses_explicit_noncertifying_timeout_bound, test_timeout_fallback_can_never_clear_duration_replay, _emit, _assert_offline_attestation, test_seven_day_schedule_10x_arrival_2x_duration_replay_emits_measured_evidence, test_bounded_fault_matrix_emits_recovery_duplicate_and_loss_evidence, _fd_count

- [`tests/v3/test_campaign_runner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_campaign_runner.py)｜3460 行｜`0b735d4888d0`｜_schedule_fixture_ids, Clock, Clock.__init__, Clock.__call__, _json_bytes, _write, create_release, create_real_launcher_release

- [`tests/v3/test_candidate_no_bytecode.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_candidate_no_bytecode.py)｜125 行｜`22f7649d3d00`｜_clean_environment, test_clean_environment_cannot_import_from_parent_candidate, _copy_package, test_route_parity_imports_gateway_without_writing_candidate_bytecode, test_candidate_pytest_collection_disables_bytecode_without_launcher, test_isolated_candidate_import_probes_do_not_escape_pytest_bytecode_fuse

- [`tests/v3/test_change_scope.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_change_scope.py)｜85 行｜`9a9bc8cd9028`｜test_docs_css_and_tests_are_scoped_only_for_development, test_operational_boundaries_force_full_even_when_mixed_with_docs, test_operational_prefix_cannot_be_downgraded_by_css_suffix, test_keyword_boundaries_force_full, test_only_explicit_pure_source_is_scoped, test_marker_in_allowed_magi_pure_directory_is_scoped, test_marker_cannot_downgrade_operational_file, test_unknown_source_and_empty_diff_fail_closed

- [`tests/v3/test_compat_admin.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_admin.py)｜88 行｜`a2f964167bcf`｜Health, Health.response, FakeServer, FakeServer.__init__, _website, test_admin_factory_is_hash_bound_and_preserves_legacy_handler, test_admin_factory_rejects_hash_drift_external_bind_and_missing_source

- [`tests/v3/test_compat_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_gateway.py)｜342 行｜`ba15fb08a66c`｜test_import_is_lazy_and_cannot_open_socket_or_start_process, test_pinned_inventory_covers_both_apps_and_sensitive_http_shapes, test_factory_does_not_call_legacy_loader_until_first_request, test_factory_does_not_call_legacy_loader_until_first_request.loader, test_fixed_production_factories_bind_their_declared_service, _representative_app, _representative_app.callback, _representative_app.login

- [`tests/v3/test_compat_http_contract_runner.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_http_contract_runner.py)｜552 行｜`751e3666cf66`｜_write_fixture, _legacy_response, test_wsgi_test_client_executes_reviewed_json_contract_and_emits_bound_evidence, test_wsgi_test_client_executes_reviewed_json_contract_and_emits_bound_evidence.osc_chat, test_asgi_style_test_client_preserves_exact_sse_stream_and_request_contract, test_asgi_style_test_client_preserves_exact_sse_stream_and_request_contract.AsgiStyleClient, test_asgi_style_test_client_preserves_exact_sse_stream_and_request_contract.AsgiStyleClient.request, test_callable_receives_verified_multipart_bytes_and_preserves_plaintext_response

- [`tests/v3/test_compat_inventory.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_inventory.py)｜163 行｜`de1631e9ef9a`｜test_runtime_inventory_and_explicit_side_effect_reviews_cover_every_method, test_get_oauth_callback_is_explicitly_reviewed_as_external_commit, test_osc_file_routes_are_explicitly_reviewed_as_sandboxed_reversible_writes, test_service_5003_offline_review_partitions_every_pinned_route_method, test_every_review_row_has_current_handler_source_identity, test_inventory_change_fails_closed_even_when_declared_counts_are_adjusted, test_inventory_missing_route_fails_count_gate, test_inventory_rejects_malformed_route_rows_before_fingerprinting

- [`tests/v3/test_compat_live_validation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_live_validation.py)｜301 行｜`1fe35318d0a3`｜test_safe_plan_is_validated_but_not_executed, test_live_probe_with_write_side_effect_is_rejected, test_live_probe_cannot_relabel_reviewed_webhook_post_as_read_only, test_supplementally_reviewed_route_method_cannot_claim_read_only, test_plan_rejects_production_ports_and_external_write_flags, test_incomplete_report_explicitly_records_unproven_gaps, test_report_rejects_summary_mismatch, test_passed_report_cannot_hide_unproven_or_nonpassing_checks

- [`tests/v3/test_compat_osc_file_contract.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_osc_file_contract.py)｜221 行｜`3913e6e8ca9d`｜ContractUser, ContractUser.__init__, contract, contract.load_user, contract.contract_login_page, contract.contract_login, _logged_in_client, test_contract_routes_are_bound_inside_the_verified_347_route_inventory

- [`tests/v3/test_compat_replay.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_replay.py)｜313 行｜`ed33741a4450`｜test_osc_preview_range_download_golden_flow_has_bound_end_to_end_outcomes, test_osc_golden_flow_expected_outcome_drift_fails_closed, test_osc_golden_flow_expected_outcome_drift_fails_closed.changed_load, test_recorded_fixtures_are_schema_valid_and_anonymized, test_anonymizer_redacts_sensitive_keys_and_inline_identifiers, test_fixture_rejects_unredacted_taiwan_pii_and_channel_identifiers, test_fixture_rejects_sensitive_identity_keys_by_default, test_fixture_rejects_raw_sensitive_header_and_file_content

- [`tests/v3/test_compat_side_effects.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_compat_side_effects.py)｜97 行｜`22df3e9132a8`｜_job, test_offline_replay_never_executes, test_isolated_live_allows_only_read_only_classes_by_default, test_isolated_live_blocks_writes_by_default, test_explicit_sandbox_can_only_enable_local_or_reversible_writes, test_job_contract_requires_idempotency_and_confirmation_for_external_commit

- [`tests/v3/test_controlled_evolution.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_controlled_evolution.py)｜334 行｜`726133ea4f74`｜_signal, _proposal, _git, _source_repo, _FakeListener, _FakeListener.getsockname, _FakeListener.close, _allow_network_probe_fixture

- [`tests/v3/test_controlled_restart_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_controlled_restart_evidence.py)｜235 行｜`a294e834de53`｜_sha, _write, _release, _observation, FakeBackend, FakeBackend.__init__, FakeBackend.observe, test_host_process_probe_excludes_only_observer_tree

- [`tests/v3/test_conversation_reference_capture_rc261.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_conversation_reference_capture_rc261.py)｜31 行｜`fc256c6fd236`｜_HistoryOrchestrator, _HistoryOrchestrator.__init__, _HistoryOrchestrator._ensure_runtime_foundations, test_user_case_identifier_becomes_recent_reference, test_assistant_case_identifier_never_becomes_authoritative_reference

- [`tests/v3/test_core_config.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_config.py)｜57 行｜`fc824d4c3aaf`｜test_default_config_is_loopback_non_binding_and_policy_aligned, test_build_is_side_effect_free, test_binding_requires_explicit_port, test_phase_one_rejects_non_loopback_binding

- [`tests/v3/test_core_health.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_health.py)｜118 行｜`59e37ea3a9ef`｜test_liveness_is_ready_without_initializing_state, test_readiness_checks_only_initialized_local_core, test_importing_health_does_not_import_heavy_frameworks, test_cli_default_does_not_create_state_or_bind, test_cli_initialize_does_not_claim_production_readiness, test_single_active_guard_rejects_second_owner

- [`tests/v3/test_core_ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_ledger.py)｜1047 行｜`eec39e297c7c`｜ledger, test_initialize_is_idempotent_and_enables_wal, test_schema_v4_migrates_existing_v3_jobs_without_losing_payload, test_canonical_job_envelope_round_trips_and_validates_schema, test_verified_commit_persists_receipts_artifacts_and_metrics, test_canonical_job_fields_fail_closed_on_invalid_input_or_stored_data, test_job_past_latest_start_is_not_leased, test_write_classes_require_contract_idempotency_key

- [`tests/v3/test_core_mutable_state_isolation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_mutable_state_isolation.py)｜399 行｜`eebdbc7a5765`｜_snapshot, test_v3_env_file_cannot_replace_hash_bound_launch_environment, test_core_imports_and_first_writes_leave_candidate_tree_immutable, test_status_consumers_follow_mutable_static_and_agent_dirs, test_http_mutable_paths_preserve_v2_defaults_without_launch_bindings

- [`tests/v3/test_core_resource.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_resource.py)｜134 行｜`3c08a9e99e36`｜request, test_global_light_limit_is_two, test_all_heavy_classes_share_two_tokens, test_guarded_pressure_and_interactive_reserve_block_background, test_interactive_bypasses_soft_guards_but_not_hard_limits, test_critical_pressure_allows_only_interactive_light, test_interactive_activity_prevents_new_background_heavy, test_active_interactive_lease_prevents_new_background_heavy

- [`tests/v3/test_core_state.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_state.py)｜48 行｜`0c56e08f8d5d`｜test_success_requires_business_completion, test_terminal_status_cannot_transition, test_waiting_and_confirmation_paths_are_explicit, test_invalid_shortcut_is_rejected, test_running_job_cannot_be_manually_requeued

- [`tests/v3/test_core_supervisor.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_core_supervisor.py)｜106 行｜`3712c6cd8935`｜worker_spec, test_worker_runs_in_owned_process_group_and_releases_slot, test_duplicate_job_is_rejected_and_group_is_terminated, test_deadline_terminates_and_reaps_worker, test_default_worker_environment_does_not_copy_arbitrary_secret, test_leader_exit_does_not_leave_unaccounted_descendant

- [`tests/v3/test_cron_policy.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cron_policy.py)｜201 行｜`54983a9df9f1`｜_isolate_cron_snapshot_binding, _synthetic_cron_payload, _write_bound_policy, test_policy_is_hash_bound_and_preserves_global_resource_caps, test_policy_loads_hash_bound_external_snapshot_without_release_root_cron, test_policy_accepts_rebased_external_snapshot_with_distinct_trusted_source_hash, test_external_snapshot_binding_cannot_fall_back_or_bypass_policy, test_external_snapshot_rejects_relative_path_and_symlink

- [`tests/v3/test_cron_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cron_service.py)｜951 行｜`b1cc0c64d7e2`｜_bind_source_tree_python, _bind_source_tree_python.resolve, test_run_component_uses_hash_bound_worker_count_not_legacy_env, test_run_component_uses_hash_bound_worker_count_not_legacy_env.StubService, test_run_component_uses_hash_bound_worker_count_not_legacy_env.StubService.__init__, test_run_component_uses_hash_bound_worker_count_not_legacy_env.StubService.run, test_bound_cron_environment_loads_hash_verified_file, test_bound_cron_environment_rejects_hash_drift

- [`tests/v3/test_cron_snapshot.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cron_snapshot.py)｜227 行｜`25b8442d6e72`｜_release, _python, test_snapshot_rebases_code_python_and_mutable_outputs, test_snapshot_converts_legacy_cd_prefix_to_release_cwd, test_snapshot_blocks_v2_files_missing_from_release, test_snapshot_blocks_model_switch_schedule_policy_conflict, test_snapshot_blocks_source_path_replacement_after_descriptor_read, test_snapshot_blocks_source_path_replacement_after_descriptor_read.read_and_replace

- [`tests/v3/test_cutover_activation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_activation.py)｜777 行｜`d277a7503c84`｜_begin, test_activation_commit_is_atomic_phase_bound_and_resumable, test_marker_mismatch_interrupted_journal_and_invalid_transition_fail_closed, test_production_ownership_refuses_ready_or_write_before_exact_commit_marker, test_active_release_snapshot_binds_stable_identity_and_active_phase, _begin_v3_rotation, test_v3_rotation_commits_hash_bound_candidate_and_remains_restartable, test_v3_rotation_refuses_previous_state_drift

- [`tests/v3/test_cutover_cli.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_cli.py)｜261 行｜`3c53f8204d4f`｜test_plan_is_explicitly_non_mutating, test_simulate_clean_and_fault_exit_codes, test_cli_reports_are_json_serializable, test_preflight_uses_release_bound_conditional_daytime_window, test_preflight_uses_release_bound_conditional_daytime_window.absolute, test_boolean_evidence_report_shortcut_is_disabled, test_preflight_requires_explicit_expected_state_and_release_context, test_execute_is_explicit_and_requires_plan_hash_and_secure_token_file

- [`tests/v3/test_cutover_core.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_core.py)｜266 行｜`f06d5fa59b0e`｜snapshot, owner, test_repository_gate_file_contains_single_active_no_go_rules, test_cutover_window_is_a_hard_timezone_aware_gate, test_cutover_window_supports_overnight_and_rejects_invalid_clock, test_cutover_window_can_be_bound_to_explicit_local_dates, test_absolute_daytime_window_is_one_day_taipei_and_end_exclusive, test_absolute_daytime_window_rejects_any_non_daylight_or_noncanonical_policy

- [`tests/v3/test_cutover_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_evidence.py)｜564 行｜`fc332f5984a7`｜_reconciliation, _plan, _chain, _raw_report, test_raw_atomic_report_rejects_marker_journal_and_hash_faults, test_raw_rollback_report_rejects_rto_larger_than_observed_duration, _pairs, _restart_inputs

- [`tests/v3/test_cutover_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_execute.py)｜2014 行｜`885ebab6cd32`｜_sha256, _write, _write_json, _seal_test_release, _binding, _conditional_binding, test_final_executor_requires_conditional_authorization_when_policy_demands_it, test_final_executor_effective_daytime_window_is_end_exclusive

- [`tests/v3/test_cutover_mutation_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_mutation_guard.py)｜60 行｜`cbe958aa90f8`｜spec, test_missing_armed_token_cannot_reach_runner, test_missing_armed_token_cannot_reach_runner.fake_runner, test_even_matching_caller_supplied_tokens_cannot_reach_launchctl, test_even_matching_caller_supplied_tokens_cannot_reach_launchctl.fake_runner, test_constructor_bypass_still_cannot_reach_mutation_method

- [`tests/v3/test_cutover_planning.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_planning.py)｜253 行｜`bd41f6123fb6`｜_sha, _json, _legacy_gate, _v2_agent_bindings, _loaded_launchd_probe, test_prepared_plan_generator_hash_binds_every_input, test_plan_generator_rejects_insecure_token_and_mismatched_release

- [`tests/v3/test_cutover_probe.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_probe.py)｜417 行｜`2e9ac3c8a8d7`｜test_default_ports_cover_v2_v3_model_and_rpc_surfaces, test_listener_inventory_uses_one_bounded_nonblocking_lsof_pass, test_listener_inventory_uses_one_bounded_nonblocking_lsof_pass.Result, test_snapshot_excludes_only_observer_chain_and_children_not_siblings, test_collect_snapshot_attributes_pid_port_launchd_and_lock_without_mutation, test_unattributed_listener_is_no_go, test_relative_listener_inherits_release_from_absolute_root_ancestor, test_known_root_general_process_is_not_ambiguous

- [`tests/v3/test_cutover_workflow.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_cutover_workflow.py)｜67 行｜`514aa4ad57d1`｜test_every_start_is_preceded_by_verified_zero_owners, test_live_validation_has_required_stop_validate_stop_restore_sequence, test_clean_simulation_completes_with_one_active_release, test_v2_residual_owner_stops_validation_before_v3_start, test_v3_residual_owner_stops_rollback_before_v2_start, test_phase_one_mutation_is_disabled_for_every_token_combination

- [`tests/v3/test_deploy_prepare.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_deploy_prepare.py)｜2098 行｜`c94ded6acc4a`｜_probe_seatbelt_capability, seatbelt_capable, test_seatbelt_capability_probe_requires_allow_default_true_success, test_seatbelt_capability_probe_requires_allow_default_true_success.fake_run, _write, _remove_test_tree, _seal_test_release, _fake_venv

- [`tests/v3/test_dispatcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_dispatcher.py)｜1241 行｜`207b573c4c2b`｜ledger, create_read_job, worker_factory, worker_factory.factory, make_dispatcher, create_priority_job, wait_for_path, assert_pid_gone

- [`tests/v3/test_evidence_compiler.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_evidence_compiler.py)｜744 行｜`9aadf061c68f`｜test_checked_in_portable_inventory_matches_candidate_source_surfaces, test_runtime_inventory_ignores_diagnostic_line_drift_but_not_interface_drift, test_cutover_baseline_matches_generated_route_and_schedule_inventories, test_compile_evidence_routes_physical_fault_inputs_only_to_campaign_compiler, test_compile_evidence_routes_physical_fault_inputs_only_to_campaign_compiler.fake_release, test_compile_evidence_routes_physical_fault_inputs_only_to_campaign_compiler.fake_campaign, _write_json, _sha

- [`tests/v3/test_evidence_ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_evidence_ledger.py)｜189 行｜`3d5667008054`｜_envelope, test_predecessor_failure_remains_history_but_not_active_latest, test_legacy_latest_is_projection_of_active_release_only, test_business_attention_never_becomes_system_failure, test_old_release_failure_is_superseded_and_current_failure_is_failed, test_envelope_rejects_tampering_and_sensitive_receipt_fields, test_duplicate_append_is_idempotent, test_projection_refuses_symlink

- [`tests/v3/test_explicit_heavy_api_only.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_explicit_heavy_api_only.py)｜168 行｜`0cee5cae6938`｜_FakeResponse, _FakeResponse.json, test_explicit_heavy_never_falls_back_to_local, test_explicit_heavy_fails_closed_when_nim_disabled, test_explicit_heavy_daily_budget_exhaustion_stops_without_retry, test_explicit_heavy_authorizes_verbatim_personal_data_for_this_call, test_explicit_heavy_authorizes_verbatim_personal_data_for_this_call.fake_nim, test_unmarked_follow_up_never_inherits_heavy_authorization

- [`tests/v3/test_external_canary.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_external_canary.py)｜95 行｜`75eca7065812`｜_keys, _receipt, test_signed_fresh_offhost_receipt_verifies_all_required_layers, test_tamper_tailnet_or_missing_ipv6_fails_closed

- [`tests/v3/test_external_credential_contract.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_external_credential_contract.py)｜201 行｜`f014a89b2e1d`｜_sha, _write, _runtime_contract, test_sealed_runtime_verifies_static_hash_and_allows_atomic_token_refresh, test_sealed_runtime_rejects_static_drift_and_unsafe_mutable_mode, test_sealed_laf_reader_has_no_candidate_fallback_and_detects_hash_drift, _handoff_manifest, test_secret_handoff_materializes_refreshable_targets_without_content_in_receipt

- [`tests/v3/test_faiss_maintenance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_faiss_maintenance.py)｜974 行｜`2ba1a84568af`｜_bind_source_tree_python, _bind_source_tree_python.resolve, _cron_policy, _isolate_direct_worker_rss, test_streaming_rebuild_never_fetches_more_than_the_bound_and_is_consistent, test_streaming_rebuild_never_fetches_more_than_the_bound_and_is_consistent.CountCursor, test_streaming_rebuild_never_fetches_more_than_the_bound_and_is_consistent.CountCursor.execute, test_streaming_rebuild_never_fetches_more_than_the_bound_and_is_consistent.CountCursor.fetchone

- [`tests/v3/test_fault_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_fault_certification.py)｜572 行｜`d4a73992e6f9`｜_canonical, _write_json, test_controlled_restart_fault_layer_uses_apfs_wal_and_transaction_sigkill, test_fault_stimulus_plan_is_replayable_and_profile_unique, test_fault_certification_hash_fails_closed_after_tamper, test_fault_certification_rejects_live_source_and_nonempty_sandboxes, test_campaign_cli_emits_profile_bound_inner_report_and_cleans_owned_sandbox, test_fault_inner_reports_compile_as_controlled_restart_offline_pass

- [`tests/v3/test_fault_realism.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_fault_realism.py)｜227 行｜`c79babbb26ab`｜test_bounded_fault_matrix_with_realism_audit_emits_recovery_duplicate_and_loss_evidence, test_owned_sigkill_commit_window_sweep_emits_exact_partial_evidence, test_fault_evidence_retains_realism_blocker_and_names_unproven_claims, test_evidence_hash_fails_closed_after_tampering, test_owned_marker_timeout_path_sigkills_and_reaps_child, test_live_tree_and_source_tree_are_rejected_before_sandbox_creation, test_nonempty_or_symlink_sandbox_is_rejected, test_invalid_cycle_count_is_rejected

- [`tests/v3/test_function_health_entrypoint.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_function_health_entrypoint.py)｜53 行｜`94c4416d54fb`｜test_function_health_entrypoint_imports_release_from_foreign_cwd

- [`tests/v3/test_function_health_operational_semantics.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_function_health_operational_semantics.py)｜515 行｜`1c8bde0e4615`｜test_business_readiness_attention_is_business_state_not_system_failure, test_malformed_business_readiness_still_fails_closed, test_predecessor_release_health_receipt_is_archived_not_failed, test_current_release_health_failure_remains_failed, _evidence, test_evidence_v2_predecessor_failure_cannot_red_light_active_release, test_evidence_v2_business_backlog_is_not_live_failure, test_unbound_health_failure_still_fails_closed

- [`tests/v3/test_g8_campaign_binding.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_g8_campaign_binding.py)｜253 行｜`66db6c1da578`｜_g8_report, _runner, test_campaign_requires_path_hash_pair_and_rejects_tamper, test_campaign_postcheck_detects_g8_report_drift, test_ledger_refuses_old_context_without_g8_binding, test_real_runner_passes_g8_env_only_to_matched_performance, test_real_runner_passes_g8_env_only_to_matched_performance.capture, test_all_seven_matched_profiles_and_artifact_context_bind_same_g8_report

- [`tests/v3/test_g8_isolated_smb.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_g8_isolated_smb.py)｜772 行｜`3de4fddfb1cb`｜_receipt, FakeSMB, FakeSMB.__init__, FakeSMB.mount_receipt, FakeSMB.state_snapshot, FakeSMB.run_arm, FakeSMB.cleanup_owned, FakeSMB.entries

- [`tests/v3/test_g8_maintenance_safety.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_g8_maintenance_safety.py)｜64 行｜`98d4f453c98c`｜_rows, _policy, test_weekend_resummary_in_callers_session_is_never_selected, test_independent_verified_v2_group_is_selected_and_reverified, test_nonleader_or_unverified_group_is_not_selected, test_restore_contract_includes_share_gateway_5014

- [`tests/v3/test_gateway.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_gateway.py)｜746 行｜`83dff5d2e1af`｜FakeGuard, FakeGuard.__init__, FakeGuard.acquire, FakeGuard.release, FakeServer, FakeServer.__init__, FakeServer.run, FakeServer.close

- [`tests/v3/test_generated_implementation_status.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_generated_implementation_status.py)｜71 行｜`5baf5fd21770`｜test_generated_status_uses_active_v3_marker_and_manifest, test_rendered_status_cannot_claim_v2_is_production

- [`tests/v3/test_health_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_health_certification.py)｜431 行｜`0c3b9fa607cc`｜_canonical, _write_json, _inner_report, test_thousand_production_health_probes_are_model_free_and_read_only, test_health_certification_rejects_live_source_and_nonempty_roots, test_health_evidence_hash_rejects_tampering, test_health_certification_emits_structured_campaign_evidence, test_campaign_cli_runs_direct_certifier_with_profile_binding

- [`tests/v3/test_heavy_fallback_live_check_contract.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_heavy_fallback_live_check_contract.py)｜38 行｜`59fdfbe50f83`｜test_load_env_uses_hash_bound_external_file, test_load_env_rejects_hash_drift

- [`tests/v3/test_host_singleton_migration.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_host_singleton_migration.py)｜198 行｜`92abfa2ce912`｜_release, _plist, test_rendered_host_singleton_contains_no_v2_reference, test_staging_is_non_mutating_and_hash_bound, test_omlx_normal_runtime_drops_release_specific_python_override, test_omlx_unified_runtime_rebinds_python_override, test_renderer_rejects_unversioned_release

- [`tests/v3/test_human_approval.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_human_approval.py)｜498 行｜`c6312d19c6a5`｜_canonical, _write, _fixture, _approve, _bound_artifacts, test_all_machine_gates_merkle_request_interactive_receipt_and_human_normalizer, test_approval_refuses_noninteractive_or_nonallowlisted_actor, test_approval_compiler_rejects_post_request_machine_evidence_drift

- [`tests/v3/test_ime_candidate_native.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_ime_candidate_native.py)｜18 行｜`43623b29664f`｜test_real_candidate_window_survives_bounded_memory_pressure

- [`tests/v3/test_ime_candidate_probe.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_ime_candidate_probe.py)｜469 行｜`4ec7b8dd96f3`｜_FakeClock, _FakeClock.__init__, _FakeClock.monotonic, _FakeClock.sleep, test_textedit_readiness_waits_for_front_document_and_window, test_textedit_readiness_retries_transient_apple_event_error, test_textedit_readiness_retries_transient_apple_event_error.readiness_reader, test_frontmost_restore_waits_for_asynchronous_appkit_activation

- [`tests/v3/test_isolated_live_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_isolated_live_evidence.py)｜164 行｜`658321bc2086`｜_sha, _artifacts, test_three_independent_runs_emit_four_authoritatively_recomputed_envelopes, test_duplicate_run_and_tampered_trace_fail_closed

- [`tests/v3/test_isolated_live_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_isolated_live_execute.py)｜2193 行｜`fb7e1c840114`｜test_macos_v2_restore_uses_actual_production_readiness_routes, _digest, _write, _json, _probes, PreparedFixture, _prepared, test_live_plan_schema_matches_the_executor_contract

- [`tests/v3/test_isolated_live_plan_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_isolated_live_plan_builder.py)｜102 行｜`125fa6b5babf`｜_build, test_builder_publishes_only_a_statically_verified_plan, test_builder_fails_closed_before_publishing_on_non_go_offline_gate, test_builder_rejects_unsafe_token_and_existing_output

- [`tests/v3/test_isolated_resource_window.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_isolated_resource_window.py)｜584 行｜`846006b58175`｜_raw_source, _pow_text, passing_report, rehash, test_passing_fixture_has_complete_inert_production_external_contract, test_resource_receipt_rejects_rehashed_nonexact_named_state_binding, test_exclusive_stopped_window_can_certify_without_claiming_per_process_metal, test_window_fails_closed_on_owner_budget_model_or_attribution_drift

- [`tests/v3/test_isolated_resource_window_collector.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_isolated_resource_window_collector.py)｜595 行｜`66493127a8b5`｜file_sha, tree_sha, Process, Process.__init__, FakeBackend, FakeBackend.__init__, FakeBackend.configure_scope, FakeBackend.isolation_probe

- [`tests/v3/test_isolated_resource_window_plan_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_isolated_resource_window_plan_builder.py)｜387 行｜`05495a8ef624`｜sha, fixture, invoke, external_kwargs, test_builder_deep_verifies_and_writes_0400_plan_0600_secret_without_executing, test_builder_binds_verified_external_python_runtime, test_builder_accepts_sealed_candidate_without_bundled_website, test_builder_rejects_sealed_candidate_that_bundles_external_website

- [`tests/v3/test_judicial_summary_quality_audit.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_judicial_summary_quality_audit.py)｜92 行｜`9f3d7d70d228`｜_StartTransactionConnection, _StartTransactionConnection.__init__, _StartTransactionConnection.start_transaction, _ActiveTransactionConnection, _BeginConnection, _BeginConnection.__init__, _BeginConnection.begin, _Cursor

- [`tests/v3/test_legacy_background_service.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_legacy_background_service.py)｜295 行｜`eaa2c3370a2d`｜test_import_does_not_start_thread_socket_or_process, test_default_plan_owns_required_components_and_keeps_preloads_off, test_preload_components_are_strictly_opt_in, test_periodic_loop_runs_immediately_and_stop_event_interrupts_wait, test_periodic_loop_runs_immediately_and_stop_event_interrupts_wait.action, test_service_starts_one_shot_and_gracefully_stops_managed_loop, test_service_starts_one_shot_and_gracefully_stops_managed_loop.one_shot, test_service_starts_one_shot_and_gracefully_stops_managed_loop.loop

- [`tests/v3/test_legacy_mutable_state_routing.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_legacy_mutable_state_routing.py)｜131 行｜`542f7625de1c`｜_metadata_snapshot, test_v3_legacy_modules_write_only_external_runtime_state

- [`tests/v3/test_legal_research_quality.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_legal_research_quality.py)｜399 行｜`4989d6065ed9`｜test_external_query_privacy_gate_keeps_issue_but_removes_identifiers, test_private_narrative_without_legal_issue_fails_closed, test_unlabelled_party_name_and_internal_narrative_never_leave_for_mcp, test_explicit_public_judge_and_court_docket_remain_searchable, test_reasoning_spans_exclude_party_argument_and_map_exact_source, test_dual_axis_ranking_and_practice_card_are_explainable, test_grounded_summary_has_span_citations_and_no_freeform_hallucination, test_citation_lock_allows_only_verified_source_with_support_span

- [`tests/v3/test_live_validation_mode.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_live_validation_mode.py)｜87 行｜`80eab9c50049`｜_environment, _request, _request.start_response, test_validation_wsgi_exposes_only_read_only_fixed_fixture, test_validation_factory_fails_closed_if_any_safety_switch_drifts

- [`tests/v3/test_macos_resources.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_macos_resources.py)｜288 行｜`495469faa2b2`｜completed, fixture_runner, fixture_runner.run, test_parsers_preserve_units_and_derived_provenance, test_footprint_parser_uses_pid_bound_phys_footprint_not_rss_or_generic_summary, test_sampler_is_read_only_bounded_and_never_promotes_rss_to_footprint_or_metal, test_sampler_is_read_only_bounded_and_never_promotes_rss_to_footprint_or_metal.runner, test_explicit_ps_bound_pids_enable_authoritative_physical_footprint

- [`tests/v3/test_magi_acceptance_gate_runtime_binding.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_magi_acceptance_gate_runtime_binding.py)｜64 行｜`814093c5749f`｜test_doctor_environment_follows_active_release_and_shared_runtime, test_source_root_uses_local_runtime_state

- [`tests/v3/test_magi_doctor_v3_runtime.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_magi_doctor_v3_runtime.py)｜450 行｜`c481efd60570`｜test_menubar_ps_fallback_handles_release_path_with_spaces, test_menubar_ps_fallback_rejects_unrelated_command, test_project_python_accepts_hash_bound_v3_runtime, test_project_python_rejects_hash_drift, test_project_python_rejects_unbound_v3_runtime, test_v3_release_detection_requires_release_id_and_release_root, test_live_runtime_root_falls_back_to_active_v3_release, test_live_runtime_root_ignores_stale_legacy_override_for_active_release

- [`tests/v3/test_manual_skill_mutable_state_isolation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_manual_skill_mutable_state_isolation.py)｜962 行｜`e8e69df8b56a`｜_snapshot, _copy_candidate, _v3_environment, test_release_launcher_exports_manual_skill_state_bindings, test_night_talk_and_council_approval_share_external_state, test_named_manual_skills_import_and_first_write_leave_candidate_immutable, test_expanded_manual_state_writers_leave_candidate_immutable, test_manual_state_defaults_preserve_v2_paths

- [`tests/v3/test_mcp_conformance.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_mcp_conformance.py)｜259 行｜`2eae88426ded`｜FakeGateway, FakeGateway.call_tool, modern_message, test_modern_discover_is_stateless_and_advertises_only_modern_version, test_modern_request_requires_per_request_version_and_capabilities, test_modern_trace_context_propagates_and_removed_handshake_fails, test_legacy_initialize_remains_compatible, call_asgi

- [`tests/v3/test_memory_lifecycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_memory_lifecycle.py)｜127 行｜`3081267b5639`｜test_record_v2_stores_hashes_not_memory_content, test_exact_tombstone_stops_recall_and_never_uses_fuzzy_delete, test_formal_legal_records_and_legal_hold_cannot_be_deleted, test_correction_archives_old_record_and_links_replacement, test_correction_rejects_identical_content_without_archiving_original, test_deletion_requires_all_backends_and_receipts, test_deletion_requires_all_backends_and_receipts.adapter, test_derived_graph_and_obsidian_indexes_tombstone_without_deleting_files

- [`tests/v3/test_menubar_cron_lifecycle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_menubar_cron_lifecycle.py)｜139 行｜`45bf9d5185cf`｜_job, _details, test_historical_resource_deferral_is_not_current_pending, test_claimed_deferred_occurrence_is_current_pending, test_active_retry_is_current_pending_without_pending_occurrence, test_success_is_not_reclassified_by_historical_partial_stdout, test_review_required_candidate_remains_visible_without_retry, test_normal_queued_occurrence_is_in_pending_summary

- [`tests/v3/test_model_recovery.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_model_recovery.py)｜53 行｜`ca685c070a27`｜_write_gate, test_newer_declared_night_e4b_gate_proves_recovery, test_wrong_model_or_stale_gate_fails_closed, test_day_gate_cannot_recover_night_switch

- [`tests/v3/test_module_boundaries.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_module_boundaries.py)｜51 行｜`11f8ff17a7ee`｜test_module_boundaries_are_complete_and_source_backed, test_legacy_facades_remain_explicit_and_new_core_is_outside_them

- [`tests/v3/test_mutable_state_handoff.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_mutable_state_handoff.py)｜559 行｜`b9d43008a95c`｜_context, _payload, _seed_source, _run, test_allowlist_is_exact_and_covers_audited_p0_p1_state, test_dry_run_writes_only_private_receipt_and_lists_optional_degradation, test_prepare_atomically_copies_only_allowlist_and_is_idempotent, test_debt_address_handoff_preserves_existing_entries_and_v3_writes_only_shared

- [`tests/v3/test_mutable_state_handoff_cutover.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_mutable_state_handoff_cutover.py)｜439 行｜`5cbf7df92d8b`｜_plan, _payload, _executor, test_handoff_runs_after_zero_before_install_and_hashes_enter_evidence, test_handoff_runs_after_zero_before_install_and_hashes_enter_evidence.handoff, test_post_zero_source_drift_restores_v2_without_consuming_token, test_post_zero_source_drift_restores_v2_without_consuming_token.handoff, test_execute_replays_the_exact_pre_cutover_receipt_identity

- [`tests/v3/test_named_mutable_state_routing.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_named_mutable_state_routing.py)｜215 行｜`712924de42a6`｜test_payment_proof_queue_uses_existing_file_review_state_binding, _sealed_environment, test_named_mutable_files_preserve_v2_fallbacks, test_sealed_named_mutable_files_bind_exact_shared_targets, test_sealed_producers_write_only_shared_targets, test_sealed_producers_write_only_shared_targets.snapshot, test_sealed_named_mutable_files_fail_closed_when_binding_missing, test_sealed_named_mutable_files_reject_release_or_wrong_shared_target

- [`tests/v3/test_nas_ocr_queue_path.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_nas_ocr_queue_path.py)｜83 行｜`a9ec4504973c`｜test_producer_worker_and_status_share_explicit_queue_path, test_sealed_v3_without_queue_binding_fails_closed, test_v2_without_queue_binding_keeps_legacy_home_database, test_sealed_v3_queue_must_be_external_and_canonical

- [`tests/v3/test_native_case_filesystem.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_native_case_filesystem.py)｜274 行｜`b61275d24c31`｜connection, _effects, _service, test_folder_creation_and_archive_are_transactionally_reflected, test_pending_closure_archives_every_case_category_without_marking_final, test_folder_is_removed_and_database_rolled_back_if_later_hook_fails, test_folder_is_removed_and_database_rolled_back_if_later_hook_fails.fail_after_filesystem, test_archive_move_is_reversed_if_database_transaction_fails

- [`tests/v3/test_native_osc_cases.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_native_osc_cases.py)｜378 行｜`27224427d327`｜AllowCsrf, AllowCsrf.validate, AllowCsrf.safe_response_cookie, connection, service, service.lawyer, client, test_native_create_preserves_core_v2_alias_and_response_contract

- [`tests/v3/test_native_osc_production.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_native_osc_production.py)｜872 行｜`53e56d053659`｜test_path_canonicalizer_maps_nas_strings_without_probing_filesystem, test_path_canonicalizer_maps_nas_strings_without_probing_filesystem.forbidden_probe, ScriptedCursor, ScriptedCursor.__init__, ScriptedCursor.execute, ScriptedCursor.fetchone, ScriptedCursor.fetchall, ScriptedCursor.close

- [`tests/v3/test_nightly_runtime_bindings.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_nightly_runtime_bindings.py)｜46 行｜`6a7a9cdab2d2`｜test_system_test_report_uses_mutable_static, test_channel_smoke_source_loads_explicit_v3_env

- [`tests/v3/test_nim_budget_reserve.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_nim_budget_reserve.py)｜39 行｜`3003590b8bf6`｜_base_env, test_background_nim_stops_before_interactive_reserve, test_interactive_nim_can_use_reserved_capacity

- [`tests/v3/test_nim_heavy_sealed_release.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_nim_heavy_sealed_release.py)｜10 行｜`2b131d9fce81`｜test_nim_heavy_imports_without_legacy_providers_package

- [`tests/v3/test_observability.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_observability.py)｜121 行｜`73672b0ca511`｜_record, test_trace_is_deidentified_but_links_intent_model_effect_and_receipt, test_support_bundle_uses_ephemeral_pseudonyms_and_rejects_free_text_labels, test_slo_keeps_deferred_separate_and_support_bundle_redacts_errors, test_dr_verification_requires_real_restore_and_both_targets, test_recent_jobs_reader_does_not_request_wal_mutation, test_recent_jobs_reader_does_not_request_wal_mutation.forbid_writer

- [`tests/v3/test_office_cognition_rc261.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_office_cognition_rc261.py)｜72 行｜`80c5bfe6c8ea`｜test_human_assistant_stops_on_material_missing_fact, test_specific_office_requests_do_not_over_question, test_attachment_resolves_content_pronoun, test_contract_exposes_tool_and_side_effect_without_running_anything, test_office_domains_are_visible_for_cross_module_work, test_multi_domain_office_fact_query_requires_verified_tool

- [`tests/v3/test_office_deliverable_quality_rc261.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_office_deliverable_quality_rc261.py)｜155 行｜`5bc62b35f330`｜test_legal_summary_requires_identifiers_money_law_and_reasoning, test_translation_must_preserve_critical_anchors, test_translation_compares_english_and_roc_date_money_semantically, test_transcript_compares_spoken_chinese_date_and_article_semantically, test_repetition_is_not_shipped_as_human_quality_output, test_transcript_requested_timestamp_and_speaker_are_verified, test_transcript_polish_cannot_change_case_identifier, test_speaker_roles_are_not_invented_from_mentions

- [`tests/v3/test_offline_machine_gate_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_offline_machine_gate_builder.py)｜352 行｜`d35fbc46a7a5`｜_sha, _write, _json, BuilderFixture, _fixture, FakeCodeOwnedRunner, FakeCodeOwnedRunner.__init__, FakeCodeOwnedRunner._value

- [`tests/v3/test_omlx_profile_schedule_sync.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_omlx_profile_schedule_sync.py)｜83 行｜`bc2ee6d91b55`｜_cron_jobs, test_model_switch_schedule_matches_profile_policy, test_model_switch_binds_raw_runtime_to_inert_bytecode_cache, test_day_boundary_has_immediate_switch_and_bounded_grace, test_night_boundary_has_immediate_switch_and_bounded_grace, test_menubar_uses_waiting_state_only_during_bounded_transition, test_menubar_marks_profile_mismatch_red_after_transition_grace

- [`tests/v3/test_operational_attestation_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_operational_attestation_builder.py)｜78 行｜`e7bc0905f559`｜_ledger, test_builder_requires_real_records_receipts_and_restore, test_builder_refuses_green_for_empty_or_unreceipted_work

- [`tests/v3/test_operational_hardening_fixture_paths.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_operational_hardening_fixture_paths.py)｜100 行｜`5f076e4cfc53`｜test_audit_fixture_quotes_release_paths_with_spaces, test_hardening_fixture_provider_quotes_laf_scanner_path, test_current_critical_paths_have_no_pass_only_exception_handlers, _write_degraded_profile, test_hardening_accepts_fresh_bounded_night_e4b_fallback, test_hardening_rejects_stale_or_topology_mismatched_fallback

- [`tests/v3/test_ownership_stability_guard.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_ownership_stability_guard.py)｜181 行｜`17a1778267f8`｜SequenceProbe, SequenceProbe.__init__, SequenceProbe.assert_exclusive, test_periodic_guard_defers_one_listener_timeout_and_resets_on_success, test_periodic_guard_never_defers_confirmed_foreign_owner, test_periodic_guard_fails_closed_after_consecutive_timeout_limit, test_periodic_guard_fails_closed_after_grace_window, test_transient_classifier_is_narrow

- [`tests/v3/test_pdf_namer_handoff.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_pdf_namer_handoff.py)｜168 行｜`33dab93fc73e`｜_json, _paths, test_default_paths_bind_live_v2_and_the_deployed_v3_shared_state, test_precopy_is_allowlisted_private_and_public_evidence_has_no_case_payload_or_file_names, test_final_refresh_accepts_normal_post_precopy_v2_learning_update, test_final_refresh_rejects_destination_drift_without_touching_v2, test_direct_final_apply_and_repeat_are_idempotent, test_symlink_escape_hardlink_and_different_overwrite_are_rejected

- [`tests/v3/test_perf_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_perf_certification.py)｜202 行｜`c94850456766`｜_arm, test_request_plan_covers_session_mariadb_folder_and_archive, test_matched_comparator_clears_only_complete_equivalent_plan, test_missing_v3_folder_or_archive_fails_closed, test_runtime_or_request_plan_drift_is_rejected, test_folder_marker_creation_timestamp_is_the_only_normalized_filesystem_field, test_folder_marker_creation_timestamp_is_the_only_normalized_filesystem_field.marker_row, test_folder_marker_normalization_rejects_invalid_date_parent_extra_or_non_gitkeep

- [`tests/v3/test_perf_compat.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_perf_compat.py)｜399 行｜`7c430ec4f671`｜test_legacy_perf_envelope_cannot_impersonate_release_bound_partial_evidence, _inventory_evidence, test_fixture_preserves_the_pinned_route_identity_and_deterministic_response, test_direct_and_compat_arms_prove_identical_workload_and_responses, test_response_drift_is_a_hard_blocker_before_comparison, test_wrong_worker_assignment_is_rejected, test_network_guard_prevents_connections, test_actual_production_livez_blueprint_runs_without_service_or_external_dependencies

- [`tests/v3/test_physical_fault_drill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_physical_fault_drill.py)｜651 行｜`b5584555ca4a`｜_device, _raw_command, _power_measurement, _passing_report, _rehash, test_selected_device_rejects_nonphysical_or_system_targets, test_prepare_plan_requires_mounted_empty_root_and_writes_owner_only_artifacts, test_authorization_requires_allowlisted_interactive_tty

- [`tests/v3/test_pre_cutover.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_pre_cutover.py)｜1617 行｜`4acabe9866af`｜digest, write_json, test_pre_cutover_rejects_conditional_authorization_outside_approved_window, test_pre_cutover_rejects_conditional_authorization_outside_approved_window.Probe, test_pre_cutover_rejects_conditional_authorization_outside_approved_window.Probe.__init__, test_pre_cutover_rejects_conditional_authorization_outside_approved_window.Probe._check, test_pre_cutover_uses_daytime_window_with_end_exclusive, test_pre_cutover_uses_daytime_window_with_end_exclusive.Probe

- [`tests/v3/test_pre_cutover_readiness.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_pre_cutover_readiness.py)｜108 行｜`75889b8d4aa7`｜_load_manifest, _surfaces, test_readiness_manifest_has_complete_required_surface_gate, test_every_status_is_machine_readable_and_backed_by_precise_evidence, test_every_required_surface_is_implemented_tested_and_ready, test_surface_readiness_remains_explicitly_separate_from_live_validation

- [`tests/v3/test_privacy_boundary_rc263.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_privacy_boundary_rc263.py)｜346 行｜`39e726b389d8`｜_verified_scrubber, test_taiwan_law_office_identifiers_are_removed_and_restorable, test_legal_prose_is_not_misclassified_as_a_person, test_labelled_name_without_space_is_removed, test_office_profile_fails_closed_without_verified_name_inventory, test_public_judgment_profile_does_not_need_office_name_inventory, test_certificate_never_contains_original_values_or_mapping, test_second_pass_detects_unmasked_identifier

- [`tests/v3/test_provisional_resource_window_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_provisional_resource_window_execute.py)｜751 行｜`7f348e2d29d6`｜_write_json, _sha, _semantic, _receipt, _launch_status, _probe_receipt, FakeMachine, FakeMachine.__init__

- [`tests/v3/test_provisional_resource_window_macos.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_provisional_resource_window_macos.py)｜92 行｜`8565b68433cc`｜_sha, _sealed, test_cli_verifies_provisional_gate_before_building_real_machine, test_cli_verifies_provisional_gate_before_building_real_machine.fake_verify_plan, test_cli_verifies_provisional_gate_before_building_real_machine.fake_static, test_cli_verifies_provisional_gate_before_building_real_machine.FakeMachine, test_cli_verifies_provisional_gate_before_building_real_machine.FakeMachine.__init__, test_cli_verifies_provisional_gate_before_building_real_machine.FakeExecutor

- [`tests/v3/test_python_runtime_snapshot.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_python_runtime_snapshot.py)｜281 行｜`201dab31230b`｜test_runtime_snapshot_uses_cross_platform_lock_backend, _runtime, test_complete_runtime_tree_is_hash_bound_and_verifiable, test_default_bytecode_cache_growth_does_not_drift_bound_runtime, test_excluded_bytecode_cache_cannot_hide_non_bytecode_member, test_dependency_or_mode_drift_is_rejected, test_symlinked_runtime_directory_is_rejected, test_pth_path_must_not_escape_runtime

- [`tests/v3/test_quality_input_builder.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_quality_input_builder.py)｜156 行｜`794e3f5c2836`｜_write, _release, _runtime_manifest, _inputs, test_builds_code_only_release_bound_quality_inputs, test_fails_closed_for_existing_output_or_wrong_count, test_runtime_tree_tampering_fails_closed, test_website_path_replacement_during_descriptor_read_fails_closed

- [`tests/v3/test_quality_ledger.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_quality_ledger.py)｜88 行｜`24df2a7d43f4`｜_attestation, _signal, test_all_business_quality_kinds_are_canonical_and_deidentified, test_unknown_or_raw_data_is_rejected_and_never_persisted, test_release_attestation_is_bound_and_never_deploy_authority, test_ledger_rejects_a_forged_release_attestation, test_quality_outcome_feeds_controlled_evolution_without_raw_evidence, test_auto_retry_requires_retry_and_human_review_requires_human

- [`tests/v3/test_rc643_v3_only_promotion.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_rc643_v3_only_promotion.py)｜99 行｜`5fcab3fb57bd`｜test_rc643_promotion_is_single_pass_v3_only, test_rc643_gate_has_no_legacy_v2_or_retired_probe_requirement, test_release_quality_suite_excludes_retired_v2_campaigns_and_keeps_v3_gates, test_active_gate_baseline_matches_generated_route_and_schedule_inventory

- [`tests/v3/test_release_bundle.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_release_bundle.py)｜841 行｜`c973f7ea37b6`｜_unlock_sealed_release_directories_after_test, _write, _git, _head, _privacy_entry, test_public_audit_distinguishes_mysql_flag_check_from_inline_secret, test_public_audit_allows_only_official_document_number_context, test_office_validation_cli_survives_v3_python_safe_path

- [`tests/v3/test_release_gate.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_release_gate.py)｜780 行｜`ee4d493fb8a0`｜test_release_gate_recomputes_dynamic_route_seatbelt_contract, test_worker_soak_metrics_follow_targeted_campaign_pass_count, test_worker_soak_metrics_fail_closed_without_hiding_unreaped_workers, test_release_gate_route_formal_environment_allowlist_fails_closed_on_drift, test_release_gate_rejects_unbound_route_seatbelt_workspace, test_release_gate_conditional_g28_requires_exact_five_fixed_sources, test_release_gate_conditional_g28_requires_exact_five_fixed_sources.verifier, test_release_gate_conditional_g28_requires_exact_five_fixed_sources.artifact

- [`tests/v3/test_release_python_launcher.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_release_python_launcher.py)｜563 行｜`2fe1b12cfc8d`｜_unlock_synthetic_release_after_test, _sealed_release, _runtime, _cron_environment, _runtime_manifest_environment, _launcher_from_environment, test_launcher_hash_binds_external_runtime_and_redirects_mutable_paths, test_launcher_refuses_python_bytecode_cache_policy_override

- [`tests/v3/test_release_quality_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_release_quality_certification.py)｜769 行｜`163c05e3aeef`｜_canonical, _rehash_report, _quality_inputs, _bind_test_website_admin, test_v3_website_admin_is_hash_bound_and_staged_inside_workspace, test_v3_website_admin_staging_rejects_source_hash_drift, test_release_quality_transcript_tampering_fails_closed, test_truthful_pytest_skip_reaches_strict_no_skip_policy

- [`tests/v3/test_resource_performance_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_resource_performance_certification.py)｜403 行｜`77e3e0ffd58a`｜_canonical, _partial_inputs, test_partial_report_tampering_never_promotes_a_missing_capability, test_owned_resource_probe_and_real_automatic_preemption_benchmark, test_owned_worker_group_physical_footprint_returns_within_budget, test_hash_bound_v2_stopped_window_upgrades_only_g8_g9_g25_capabilities, test_campaign_entrypoint_reexecs_complete_partial_producer_under_seatbelt, test_campaign_entrypoint_reexecs_complete_partial_producer_under_seatbelt.capture

- [`tests/v3/test_route_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_route_certification.py)｜940 行｜`82561b6a7f18`｜test_actual_worker_protects_login_account_live_root_when_home_is_isolated, test_actual_worker_protects_login_account_live_root_when_home_is_isolated.run, _dispositions, _binding, _runtime_binding, _trace_safety, _base_safety, test_reviewed_passing_trace_promotes_real_handler_success_not_guard

- [`tests/v3/test_route_certification_gateway_success.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_route_certification_gateway_success.py)｜208 行｜`1b6f9863b520`｜test_line_root_and_alias_callbacks_dispatch_to_offline_handler, test_line_root_and_alias_callbacks_dispatch_to_offline_handler.OfflineLineHandler, test_line_root_and_alias_callbacks_dispatch_to_offline_handler.OfflineLineHandler.handle, test_line_rate_limit_returns_retry_after, test_line_rate_limit_returns_retry_after.reject, test_login_and_register_commit_only_to_in_memory_database, test_login_and_register_commit_only_to_in_memory_database.Cursor, test_login_and_register_commit_only_to_in_memory_database.Cursor.__init__

- [`tests/v3/test_route_certification_osc_crud_success.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_route_certification_osc_crud_success.py)｜456 行｜`da9ef77e64eb`｜_app, test_osc_crud_success_paths_use_transaction_journal, test_osc_crud_success_paths_use_transaction_journal.execute, test_remaining_osc_projection_and_sandbox_text_success, test_remaining_osc_projection_and_sandbox_text_success.execute, test_remaining_osc_files_documents_and_laf_success, test_remaining_osc_files_documents_and_laf_success.execute, test_remaining_osc_files_documents_and_laf_success.create_structure

- [`tests/v3/test_route_certification_tools_success.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_route_certification_tools_success.py)｜294 行｜`535ad936f9b4`｜_OfflineAutoSkill, _OfflineAutoSkill._ok, _OfflineAutoSkill.teach, _OfflineAutoSkill.learn, _OfflineAutoSkill.learn_from_file, _OfflineAutoSkill.internalize_as_skill, _OfflineAutoSkill.internalize_codebase_as_skills, _OfflineAutoSkill.import_toolsai_auto_skill

- [`tests/v3/test_route_parity.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_route_parity.py)｜94 行｜`4e2475bf2d4b`｜FakeMap, FakeMap.__init__, FakeMap.iter_rules, FakeApp, FakeApp.__init__, _write_inputs, test_exact_factory_route_surface_passes_without_requests, test_missing_and_extra_routes_fail_closed

- [`tests/v3/test_runtime_isolation_regressions.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_runtime_isolation_regressions.py)｜482 行｜`88d4cc5fa4bd`｜_unlock_read_only_candidate_directories_after_test, _app_with_blueprint, test_launcher_deploy_mutable_env_matrix_and_release_test_contract_are_explicit, test_golem_v3_stages_pending_secret_without_mutating_active_env, test_golem_v2_keeps_existing_direct_env_edit_contract, test_osc_share_and_forms_routes_first_write_only_external_state, test_osc_share_and_forms_routes_first_write_only_external_state._Document, test_osc_share_and_forms_routes_first_write_only_external_state._Document.save

- [`tests/v3/test_safe_mlx_preflight.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_safe_mlx_preflight.py)｜25 行｜`e5144bd9e322`｜test_default_preflight_does_not_import_mlx_core

- [`tests/v3/test_schedule_baseline_capture.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_schedule_baseline_capture.py)｜422 行｜`4dd555d926d9`｜test_capture_baseline_redacts_state_and_keeps_only_successful_duration, test_capture_baseline_accumulates_unique_successes_and_computes_p95, test_capture_baseline_normalizes_offsets_before_deduplication, test_capture_baseline_rejects_conflicting_duration_for_same_instant, test_capture_baseline_interprets_legacy_naive_scheduler_time_as_taipei, test_capture_baseline_invalidates_observation_after_command_change, test_capture_baseline_accepts_only_runtime_sample_bound_to_current_command

- [`tests/v3/test_schedule_body_registry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_schedule_body_registry.py)｜1425 行｜`e1b9d1554a1f`｜_inputs, _bound_cron_snapshot_bytes, _system_diagnostic_adapter, test_registry_resolves_every_enabled_job_exactly_once, test_osc_events_fixture_expectation_rolls_forward_across_month_end, test_osc_events_contract_uses_bound_future_date_not_a_fixed_calendar_day, test_osc_events_database_fixture_covers_document_index_fast_path, test_system_diagnostic_terminal_accepts_only_resource_warnings_and_preserves_them

- [`tests/v3/test_schedule_capacity_certification.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_schedule_capacity_certification.py)｜685 行｜`01c23d188302`｜_occurrence, test_coalescing_safety_allows_only_declared_durable_checkpoint_backlogs, test_campaign_schedule_body_cache_is_release_bound_and_reused, test_campaign_schedule_body_cache_is_release_bound_and_reused.fake_registry, test_certified_campaign_workdir_cleanup_removes_only_owned_tmpdir, test_certified_campaign_workdir_cleanup_rejects_unowned_directory, test_same_job_coalescer_distinguishes_exact_dedup_from_latest_pending, test_layered_capacity_accounts_for_10x_delivery_and_never_runs_same_job_twice

- [`tests/v3/test_schedule_evidence.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_schedule_evidence.py)｜780 行｜`152ed486dc65`｜_digest, _sign, _raw_passes, test_one_bound_raw_pass_recomputes_the_seven_day_g11_metrics, test_duplicate_or_missing_profiles_and_bodies_fail_closed, test_cron_or_release_source_drift_fails_closed, test_enabled_job_set_is_derived_from_cron_and_must_match_raw_evidence, test_invalid_cron_snapshot_fails_closed

- [`tests/v3/test_schedule_realism.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_schedule_realism.py)｜497 行｜`f75ec3d13edb`｜test_command_identity_treats_release_root_rebase_and_quoting_as_equivalent, test_command_identity_treats_v3_checkout_and_legacy_root_as_equivalent, test_command_identity_treats_complete_mutable_checkout_pair_as_rebased, test_command_identity_treats_manifest_bound_candidate_rebase_as_equivalent, test_command_identity_treats_bound_external_runtime_shared_paths_as_equivalent, test_command_identity_does_not_collapse_unbound_shared_runtime_path, test_command_identity_detects_argument_value_drift, test_command_identity_does_not_rebase_unrelated_absolute_code_anchor

- [`tests/v3/test_scheduled_mutable_state_routing.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_scheduled_mutable_state_routing.py)｜417 行｜`aa70473cc9bd`｜_release_metadata, test_scheduled_modules_keep_v3_release_immutable, test_scheduled_module_defaults_preserve_v2_paths

- [`tests/v3/test_sealed_candidate_paths.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_sealed_candidate_paths.py)｜361 行｜`bab5d8b09b8a`｜_bind_candidate_cron, test_candidate_diagnostics_use_hash_bound_cron_without_release_copy, test_candidate_cron_binding_rejects_hash_drift, test_sealed_candidate_without_external_cron_binding_fails_closed, test_candidate_cron_source_must_match_release_policy, test_candidate_child_processes_select_verified_launcher_without_venv, test_candidate_mutable_preflight_and_laf_state_stay_outside_release, test_candidate_mutable_preflight_and_laf_state_stay_outside_release.ExitedProcess

- [`tests/v3/test_service_entrypoints.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_service_entrypoints.py)｜1033 行｜`df6af4c673d5`｜identity, test_importing_production_entrypoints_has_no_runtime_side_effects, test_default_ownership_probe_rejects_v2_launchd_process_and_foreign_ports, test_command_root_matching_never_resolves_process_tokens, test_command_root_matching_never_resolves_process_tokens.reject_resolve, test_default_ownership_probe_accepts_release_managed_listener_process_group, test_default_ownership_probe_accepts_its_own_bound_listener, test_default_ownership_probe_accepts_verified_same_release_role_listener

- [`tests/v3/test_service_manifest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_service_manifest.py)｜135 行｜`988e791c2abf`｜test_production_service_manifest_has_exact_single_owner_topology, test_isolated_live_validation_manifest_has_no_production_background_children, test_runtime_manifest_selection_is_release_hash_and_safety_bound, test_manifest_ownership_and_process_escape_fail_closed

- [`tests/v3/test_skill_manifest_sandbox.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_skill_manifest_sandbox.py)｜132 行｜`1c804578ced2`｜_skill, test_candidate_manifest_binds_action_and_defaults_to_no_authority, test_live_skill_requires_exact_release_catalog_digest, test_manifest_rejects_undeclared_dependency_hash, test_whole_process_seatbelt_denies_network_and_external_write

- [`tests/v3/test_skill_overlay_isolation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_skill_overlay_isolation.py)｜496 行｜`64c37aba9c8c`｜_write_skill, _tree_digest, test_overlay_precedence_and_copy_on_write_keep_release_immutable, test_docs_only_overlay_rebases_release_a_to_b_but_preserves_user_code, test_nerv_edit_snapshot_rollback_and_definitions_are_external, test_full_tree_snapshot_rollback_deletes_extras_and_reloads_registry, test_runtime_mutation_paths_live_under_overlay, test_generate_install_and_autoskill_write_only_overlay

- [`tests/v3/test_span_evaluation.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_span_evaluation.py)｜45 行｜`d0b8196aa7f6`｜_span, test_behavior_evaluation_proves_calls_absence_retries_receipt_and_terminal, test_behavior_evaluation_reports_every_policy_failure

- [`tests/v3/test_static_external_staging.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_static_external_staging.py)｜490 行｜`93fe5a44b071`｜_sha, _write, _write_json, _fixture, _source_kwargs, _stage, test_stage_has_no_deploy_prerequisite_and_reverifies_exact_five_inputs, test_source_snapshot_must_be_prebound_and_source_drift_is_detected

- [`tests/v3/test_supply_chain.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_supply_chain.py)｜248 行｜`2d10450340d4`｜_components, test_runtime_lock_and_sbom_are_deterministic, test_wheelhouse_manifest_detects_drift, test_secret_scan_rejects_credentials_but_not_normal_source, test_secret_scan_allows_explicit_offline_fixtures_but_rejects_real_looking_values, test_secret_scan_never_allows_fixture_prefixes_in_production_source, test_runtime_install_policy_has_only_guarded_call_sites, test_vulnerability_receipt_is_bound_and_rejects_high_findings

- [`tests/v3/test_telemetry.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_telemetry.py)｜52 行｜`add74dcc1aed`｜test_w3c_trace_context_round_trip_and_child_linkage, test_span_rejects_content_and_paths_as_attributes, test_invalid_traceparent_is_not_accepted, test_otlp_exporter_is_loopback_only

- [`tests/v3/test_transcription_backend_manifest.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_transcription_backend_manifest.py)｜146 行｜`fe568834a335`｜_backend_map, test_transcription_manifest_matches_schema_and_has_unique_backends, test_mlx_whisper_and_whisper_cli_form_the_only_enabled_dual_pair, test_forensic_dual_pair_requires_absolute_offline_content_bound_artifacts, test_ownscribe_is_fail_closed_until_legal_chinese_benchmark_passes, test_manifest_requires_a_versioned_provenance_preserving_adapter, test_evaluation_is_source_based_and_makes_no_verified_quality_claim, test_candidate_suite_result_is_explicitly_partial

- [`tests/v3/test_transcription_quality_benchmark.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_transcription_quality_benchmark.py)｜278 行｜`4184223a86be`｜_seal, _manifest, _backend, _report, test_authorized_hash_only_corpus_is_ready_without_opening_media, test_existing_one_second_generic_fixture_cannot_satisfy_corpus_gate, test_corpus_rejects_confidential_or_uploadable_material, test_portable_evidence_rejects_raw_transcript_or_local_path

- [`tests/v3/test_v3_rotation_drill.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_v3_rotation_drill.py)｜316 行｜`13a7bc35adeb`｜_release, _marker, _sentinel, _report, _write_report, test_v3_rotation_report_derives_atomic_restart_and_rollback_metrics, test_v3_rotation_report_fails_closed_on_semantic_tamper, test_v3_only_release_gate_accepts_only_the_single_rotation_report_role

- [`tests/v3/test_v3_rotation_execute.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_v3_rotation_execute.py)｜652 行｜`bdd01dd8998f`｜_sha, _write, _release, _deployment, FakeMachine, FakeMachine.__init__, FakeMachine.run, FakeMachine.observe

- [`tests/v3/test_validation_campaign.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_validation_campaign.py)｜174 行｜`7c643e09079d`｜test_campaign_is_armed_for_certifying_offline_only_and_single_active, test_all_offline_workloads_are_certified_and_historical_blockers_remain_auditable

- [`tests/v3/test_validation_manifest_integrity.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_validation_manifest_integrity.py)｜129 行｜`805614101a91`｜_load, _strings, _declared_test_paths, test_active_test_matrix_never_references_missing_tests, test_v3_quality_manifest_never_references_missing_or_v2_tests, test_py_compile_gate_never_references_missing_source_files, test_pre_cutover_suites_never_mirror_evidence_into_live_runtime, test_user_reported_regression_manifest_is_complete_and_resolvable

- [`tests/v3/test_validation_router.py`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/test_validation_router.py)｜500 行｜`11c5be7531fc`｜_manifest, _nodes, _materialize_core_workspace, _write_cli_sources, _formal_binding, test_pure_change_routes_one_measured_node_without_promotion_downgrade, test_operational_change_routes_full_core_and_unknown_inventory_does_not_scope, test_formal_modes_require_release_binding_and_keep_all_core_sections

<a id="appD"></a>
# 附錄 D. 設定、Schema、前端與腳本索引

| 副檔名 | 檔案數 |
| --- | --- |
| .py | 1394 |
| .xsd | 117 |
| .md | 94 |
| .json | 87 |
| .pdf | 80 |
| .html | 53 |
| .png | 34 |
| .js | 31 |
| .sh | 19 |
| .xml | 15 |
| .txt | 13 |
| [none] | 11 |
| .css | 9 |
| .docx | 9 |
| .gradle | 6 |
| .plist | 6 |
| .java | 3 |
| .csv | 2 |
| .properties | 2 |
| .sql | 2 |
| .toml | 2 |
| .yaml | 2 |
| .yml | 2 |
| .bat | 1 |
| .cmd | 1 |
| .command | 1 |
| .example | 1 |
| .ipynb | 1 |
| .jar | 1 |
| .pro | 1 |
| .ps1 | 1 |
| .sb | 1 |
| .svg | 1 |
| .swift | 1 |

- [`.env.example`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/.env.example)｜782 bytes｜`632e1358740d`

- [`.github/workflows/build-installers.yml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/.github/workflows/build-installers.yml)｜1,321 bytes｜`622b389b7e70`

- [`.github/workflows/ci.yml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/.github/workflows/ci.yml)｜1,190 bytes｜`86ff523e4337`

- [`.gitignore`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/.gitignore)｜7,566 bytes｜`8d17420006c4`

- [`CONSTITUTION.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/CONSTITUTION.md)｜2,986 bytes｜`308c8411e86f`

- [`LICENSE`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/LICENSE)｜1,133 bytes｜`1b0bdc4ae6f7`

- [`MAGI_SAAS_ROADMAP.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/MAGI_SAAS_ROADMAP.md)｜3,035 bytes｜`8ae2a9d57d25`

- [`PUBLIC_RELEASE.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/PUBLIC_RELEASE.json)｜510 bytes｜`c1169e1ec0b9`

- [`README.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/README.md)｜20,783 bytes｜`73bfc26f5247`

- [`README.zh-TW.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/README.zh-TW.md)｜18,989 bytes｜`35bc1cc34ef4`

- [`SECURITY.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/SECURITY.md)｜3,297 bytes｜`d6d4ad7ab369`

- [`SUPPORT.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/SUPPORT.md)｜1,664 bytes｜`e388a35e984d`

- [`api/pipelines/typo_map.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/pipelines/typo_map.json)｜2,325 bytes｜`1a0e5ac6668e`

- [`bin/magi-v3-python`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/bin/magi-v3-python)｜22,304 bytes｜`2dd2efd98b4f`

- [`config/a2a/adapter.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/a2a/adapter.json)｜172 bytes｜`09611397e13f`

- [`config/agent_capabilities.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/agent_capabilities.json)｜18,097 bytes｜`d2d67e646a66`

- [`config/bin/omlx_switch_model.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/bin/omlx_switch_model.sh)｜43,792 bytes｜`cb059ce19f63`

- [`config/business_recovery_contracts.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/business_recovery_contracts.json)｜5,855 bytes｜`db049a691030`

- [`config/exam_tutor_trend_sources.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/exam_tutor_trend_sources.json)｜7,141 bytes｜`d65aafa4a422`

- [`config/git-hooks/pre-commit`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/git-hooks/pre-commit)｜142 bytes｜`b3ed7365d18d`

- [`config/laf_branch_profiles.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/laf_branch_profiles.json)｜4,231 bytes｜`a0e0977a42c2`

- [`config/launchagents/com.magi.memory-watchdog.plist`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/launchagents/com.magi.memory-watchdog.plist)｜1,619 bytes｜`41c6d3359df9`

- [`config/launchagents/com.magi.mlx-mtp.plist`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/launchagents/com.magi.mlx-mtp.plist)｜1,747 bytes｜`34557e874739`

- [`config/launchagents/com.magi.omlx-restore.plist`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/launchagents/com.magi.omlx-restore.plist)｜887 bytes｜`bbf13eea1bd6`

- [`config/launchagents/com.magi.paperclip-share-gateway.plist`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/launchagents/com.magi.paperclip-share-gateway.plist)｜1,413 bytes｜`e7abefb05943`

- [`config/launchagents/com.magi.paperclip-share-tunnel.plist`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/launchagents/com.magi.paperclip-share-tunnel.plist)｜2,012 bytes｜`c5e77109aa95`

- [`config/launchdaemons/com.magi.nas-mountpoints.plist`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/launchdaemons/com.magi.nas-mountpoints.plist)｜791 bytes｜`95141b875300`

- [`config/mcp/approved_servers.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/mcp/approved_servers.json)｜200 bytes｜`d6583718a671`

- [`config/model_registry.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/model_registry.json)｜4,860 bytes｜`96b3c36a78d9`

- [`config/observability/otel-collector.yaml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/observability/otel-collector.yaml)｜575 bytes｜`7c2ad5af4657`

- [`config/selfhost.example.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/selfhost.example.json)｜2,939 bytes｜`9166a73b62f0`

- [`config/selfhost.schema.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/selfhost.schema.json)｜4,846 bytes｜`8d1ddc437f66`

- [`config/single_source_of_truth.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/single_source_of_truth.json)｜1,400 bytes｜`1ca95c2d8237`

- [`config/skills/approved_skill_catalog.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/skills/approved_skill_catalog.json)｜112 bytes｜`f1eb84c39f81`

- [`config/supply-chain/rc643-r64/python-runtime-lock.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/supply-chain/rc643-r64/python-runtime-lock.json)｜72,892 bytes｜`96d85ea1b9ef`

- [`config/supply-chain/rc643-r64/sbom.cdx.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/supply-chain/rc643-r64/sbom.cdx.json)｜436,631 bytes｜`39ce8c06ee60`

- [`config/supply-chain/rc643-r64/vulnerability-receipt.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/supply-chain/rc643-r64/vulnerability-receipt.json)｜337 bytes｜`992d55411636`

- [`config/supply-chain/rc643-r64/wheelhouse-manifest.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/supply-chain/rc643-r64/wheelhouse-manifest.json)｜4,532 bytes｜`4404c9080227`

- [`config/supply-chain/rc643-runtime-security-overlay.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/supply-chain/rc643-runtime-security-overlay.txt)｜973 bytes｜`0a67a969a059`

- [`config/test_matrix.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/test_matrix.json)｜19,493 bytes｜`f88cc72142a4`

- [`config/v3_capability_manifest.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_capability_manifest.json)｜7,758 bytes｜`866a4106998c`

- [`config/v3_cutover_gates.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_cutover_gates.json)｜4,813 bytes｜`4451c502d72e`

- [`config/v3_launchagent_roles.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_launchagent_roles.json)｜822 bytes｜`83701ea2872c`

- [`config/v3_live_validation_service_manifest.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_live_validation_service_manifest.json)｜1,170 bytes｜`2cfabc03b1c4`

- [`config/v3_module_boundaries.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_module_boundaries.json)｜2,189 bytes｜`d379e3fca0f5`

- [`config/v3_pre_cutover_readiness.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_pre_cutover_readiness.json)｜7,823 bytes｜`9efc7d75081d`

- [`config/v3_regression_scenarios.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_regression_scenarios.json)｜3,265 bytes｜`06e42096c21a`

- [`config/v3_release_quality_suites.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_release_quality_suites.json)｜4,533 bytes｜`c522fb0b8c82`

- [`config/v3_resource_policy.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_resource_policy.json)｜8,825 bytes｜`fd7a637c08bd`

- [`config/v3_resource_window.sb`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_resource_window.sb)｜870 bytes｜`3162bd3600b2`

- [`config/v3_schedule_body_adapter_registry.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_schedule_body_adapter_registry.json)｜151,873 bytes｜`98b2021abee8`

- [`config/v3_schedule_dispatch_policy.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_schedule_dispatch_policy.json)｜2,131 bytes｜`83d3e9cc8a3e`

- [`config/v3_schedule_realism_baseline.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_schedule_realism_baseline.json)｜146,265 bytes｜`27a86f71c427`

- [`config/v3_service_manifest.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_service_manifest.json)｜2,019 bytes｜`eba82bbe24a2`

- [`config/v3_supply_chain_binding.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_supply_chain_binding.json)｜1,390 bytes｜`4bcef06bf566`

- [`config/v3_transcription_backends.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_transcription_backends.json)｜6,976 bytes｜`cf4e7235021e`

- [`config/v3_validation_campaign.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/config/v3_validation_campaign.json)｜15,820 bytes｜`8e10298230cf`

- [`data/templates/D_supplement.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/data/templates/D_supplement.docx)｜24,227 bytes｜`4d0f7147ecec`

- [`install-magi.cmd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/install-magi.cmd)｜123 bytes｜`3f76952efdda`

- [`install-magi.command`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/install-magi.command)｜278 bytes｜`681ebe3c0556`

- [`install-magi.ps1`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/install-magi.ps1)｜1,161 bytes｜`3b23c70704d4`

- [`integrations/debt_robot/document/A.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/document/A.docx)｜19,455 bytes｜`e9d4cdafc573`

- [`integrations/debt_robot/document/B.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/document/B.docx)｜20,143 bytes｜`a960b5986eb4`

- [`integrations/debt_robot/document/C.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/document/C.docx)｜14,263 bytes｜`676e272005ba`

- [`integrations/debt_robot/document/D.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/integrations/debt_robot/document/D.docx)｜24,227 bytes｜`29f777ae54e2`

- [`json/datastores.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/json/datastores.json)｜1,354 bytes｜`e1bd2a858e09`

- [`json/holidays_config.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/json/holidays_config.json)｜149 bytes｜`3b4763e9f238`

- [`json/models.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/json/models.json)｜2,644 bytes｜`02d9d4c376b8`

- [`json/nodes.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/json/nodes.json)｜1,337 bytes｜`534ad20a6059`

- [`json/services.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/json/services.json)｜1,692 bytes｜`77a54fb386fb`

- [`migrations/README.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/migrations/README.md)｜978 bytes｜`a6ce58c9fc86`

- [`migrations/versions/003_add_tenant_scope.sql`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/migrations/versions/003_add_tenant_scope.sql)｜5,310 bytes｜`e67a861072ea`

- [`mobile_app/README.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/README.md)｜1,273 bytes｜`0b4456987c02`

- [`mobile_app/android/.gitignore`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/.gitignore)｜1,867 bytes｜`bec7785deef7`

- [`mobile_app/android/app/.gitignore`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/.gitignore)｜26 bytes｜`99c54e51ee60`

- [`mobile_app/android/app/build.gradle`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/build.gradle)｜2,132 bytes｜`9da4bec3a141`

- [`mobile_app/android/app/capacitor.build.gradle`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/capacitor.build.gradle)｜370 bytes｜`7051cadfdbc5`

- [`mobile_app/android/app/proguard-rules.pro`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/proguard-rules.pro)｜751 bytes｜`1cf8c57e8f79`

- [`mobile_app/android/app/src/androidTest/java/com/getcapacitor/myapp/ExampleInstrumentedTest.java`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/androidTest/java/com/getcapacitor/myapp/ExampleInstrumentedTest.java)｜774 bytes｜`ff50b4c110a7`

- [`mobile_app/android/app/src/main/AndroidManifest.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/AndroidManifest.xml)｜1,529 bytes｜`a65ff5c0812a`

- [`mobile_app/android/app/src/main/assets/capacitor.config.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/assets/capacitor.config.json)｜235 bytes｜`67bb17b95f77`

- [`mobile_app/android/app/src/main/java/tw/local/magi/mobile/MainActivity.java`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/java/tw/local/magi/mobile/MainActivity.java)｜124 bytes｜`f4950b948e9c`

- [`mobile_app/android/app/src/main/res/drawable-land-hdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-land-hdpi/splash.png)｜7,705 bytes｜`08cc34ad7713`

- [`mobile_app/android/app/src/main/res/drawable-land-mdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-land-mdpi/splash.png)｜4,040 bytes｜`5cf98b4451bd`

- [`mobile_app/android/app/src/main/res/drawable-land-xhdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-land-xhdpi/splash.png)｜9,251 bytes｜`22f87e1e3bc8`

- [`mobile_app/android/app/src/main/res/drawable-land-xxhdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-land-xxhdpi/splash.png)｜13,984 bytes｜`42aa26392546`

- [`mobile_app/android/app/src/main/res/drawable-land-xxxhdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-land-xxxhdpi/splash.png)｜17,683 bytes｜`60393ce8636f`

- [`mobile_app/android/app/src/main/res/drawable-port-hdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-port-hdpi/splash.png)｜7,934 bytes｜`c5015f4ba362`

- [`mobile_app/android/app/src/main/res/drawable-port-mdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-port-mdpi/splash.png)｜4,096 bytes｜`07fa579e1c83`

- [`mobile_app/android/app/src/main/res/drawable-port-xhdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-port-xhdpi/splash.png)｜9,875 bytes｜`b73049cb37fe`

- [`mobile_app/android/app/src/main/res/drawable-port-xxhdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-port-xxhdpi/splash.png)｜13,346 bytes｜`0c7f1212f25b`

- [`mobile_app/android/app/src/main/res/drawable-port-xxxhdpi/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-port-xxxhdpi/splash.png)｜17,489 bytes｜`3db071a03b2f`

- [`mobile_app/android/app/src/main/res/drawable-v24/ic_launcher_foreground.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable-v24/ic_launcher_foreground.xml)｜1,880 bytes｜`a8514094f754`

- [`mobile_app/android/app/src/main/res/drawable/ic_launcher_background.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable/ic_launcher_background.xml)｜5,606 bytes｜`718ba51adf16`

- [`mobile_app/android/app/src/main/res/drawable/splash.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/drawable/splash.png)｜4,040 bytes｜`5cf98b4451bd`

- [`mobile_app/android/app/src/main/res/layout/activity_main.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/layout/activity_main.xml)｜535 bytes｜`5d770feb7913`

- [`mobile_app/android/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml)｜265 bytes｜`9c3a7e0a6515`

- [`mobile_app/android/app/src/main/res/mipmap-anydpi-v26/ic_launcher_round.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-anydpi-v26/ic_launcher_round.xml)｜265 bytes｜`9c3a7e0a6515`

- [`mobile_app/android/app/src/main/res/mipmap-hdpi/ic_launcher.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-hdpi/ic_launcher.png)｜2,786 bytes｜`72b71c3581ca`

- [`mobile_app/android/app/src/main/res/mipmap-hdpi/ic_launcher_foreground.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-hdpi/ic_launcher_foreground.png)｜3,450 bytes｜`32baa10d2632`

- [`mobile_app/android/app/src/main/res/mipmap-hdpi/ic_launcher_round.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-hdpi/ic_launcher_round.png)｜4,341 bytes｜`bfcc1b0fa931`

- [`mobile_app/android/app/src/main/res/mipmap-mdpi/ic_launcher.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-mdpi/ic_launcher.png)｜1,869 bytes｜`27ed3603010e`

- [`mobile_app/android/app/src/main/res/mipmap-mdpi/ic_launcher_foreground.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-mdpi/ic_launcher_foreground.png)｜2,110 bytes｜`58e78a618778`

- [`mobile_app/android/app/src/main/res/mipmap-mdpi/ic_launcher_round.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-mdpi/ic_launcher_round.png)｜2,725 bytes｜`0166fc333074`

- [`mobile_app/android/app/src/main/res/mipmap-xhdpi/ic_launcher.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xhdpi/ic_launcher.png)｜3,981 bytes｜`d35dbfff175b`

- [`mobile_app/android/app/src/main/res/mipmap-xhdpi/ic_launcher_foreground.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xhdpi/ic_launcher_foreground.png)｜5,036 bytes｜`6f88083b8166`

- [`mobile_app/android/app/src/main/res/mipmap-xhdpi/ic_launcher_round.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xhdpi/ic_launcher_round.png)｜6,593 bytes｜`40911a009228`

- [`mobile_app/android/app/src/main/res/mipmap-xxhdpi/ic_launcher.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xxhdpi/ic_launcher.png)｜6,644 bytes｜`ed346eb1e3f0`

- [`mobile_app/android/app/src/main/res/mipmap-xxhdpi/ic_launcher_foreground.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xxhdpi/ic_launcher_foreground.png)｜9,793 bytes｜`4a82bc1e9923`

- [`mobile_app/android/app/src/main/res/mipmap-xxhdpi/ic_launcher_round.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xxhdpi/ic_launcher_round.png)｜10,455 bytes｜`1ee4cd9ff371`

- [`mobile_app/android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png)｜9,441 bytes｜`87cb2f2ffe99`

- [`mobile_app/android/app/src/main/res/mipmap-xxxhdpi/ic_launcher_foreground.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xxxhdpi/ic_launcher_foreground.png)｜15,529 bytes｜`bd24fd383253`

- [`mobile_app/android/app/src/main/res/mipmap-xxxhdpi/ic_launcher_round.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/mipmap-xxxhdpi/ic_launcher_round.png)｜15,916 bytes｜`ab93096331e7`

- [`mobile_app/android/app/src/main/res/values/ic_launcher_background.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/values/ic_launcher_background.xml)｜120 bytes｜`cd40bafe618f`

- [`mobile_app/android/app/src/main/res/values/strings.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/values/strings.xml)｜302 bytes｜`c7e398085174`

- [`mobile_app/android/app/src/main/res/values/styles.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/values/styles.xml)｜823 bytes｜`18ebba36575e`

- [`mobile_app/android/app/src/main/res/xml/file_paths.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/main/res/xml/file_paths.xml)｜213 bytes｜`a46ed43ef65c`

- [`mobile_app/android/app/src/test/java/com/getcapacitor/myapp/ExampleUnitTest.java`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/app/src/test/java/com/getcapacitor/myapp/ExampleUnitTest.java)｜402 bytes｜`d4045ae8fac1`

- [`mobile_app/android/build.gradle`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/build.gradle)｜632 bytes｜`2b8ea5d22103`

- [`mobile_app/android/capacitor.settings.gradle`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/capacitor.settings.gradle)｜207 bytes｜`b488e162f552`

- [`mobile_app/android/gradle.properties`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/gradle.properties)｜987 bytes｜`3bab15c5b8bc`

- [`mobile_app/android/gradle/wrapper/gradle-wrapper.jar`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/gradle/wrapper/gradle-wrapper.jar)｜43,583 bytes｜`2db75c40782f`

- [`mobile_app/android/gradle/wrapper/gradle-wrapper.properties`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/gradle/wrapper/gradle-wrapper.properties)｜253 bytes｜`f7fafb1ddd0a`

- [`mobile_app/android/gradlew`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/gradlew)｜8,762 bytes｜`a3648413b47e`

- [`mobile_app/android/gradlew.bat`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/gradlew.bat)｜2,872 bytes｜`2209f919a225`

- [`mobile_app/android/settings.gradle`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/settings.gradle)｜208 bytes｜`6ce098d15ebd`

- [`mobile_app/android/variables.gradle`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/android/variables.gradle)｜497 bytes｜`6b4439bc3517`

- [`mobile_app/capacitor.config.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/capacitor.config.json)｜253 bytes｜`106449851dec`

- [`mobile_app/package-lock.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/package-lock.json)｜40,194 bytes｜`062d525feffb`

- [`mobile_app/package.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/package.json)｜630 bytes｜`c284232ec71a`

- [`mobile_app/www/index.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/mobile_app/www/index.html)｜1,018 bytes｜`e7a4d7098804`

- [`pyproject.toml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/pyproject.toml)｜3,193 bytes｜`c478b9684992`

- [`requirements-optional.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/requirements-optional.txt)｜1,906 bytes｜`1aaf30be9817`

- [`requirements-selfhost.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/requirements-selfhost.txt)｜263 bytes｜`5f1d2f446b05`

- [`requirements.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/requirements.txt)｜688 bytes｜`532b4da0e145`

- [`resources/osc/photo/lawyer_stamp.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/resources/osc/photo/lawyer_stamp.png)｜4,874 bytes｜`a0e604b5d69b`

- [`resources/osc/photo/logo.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/resources/osc/photo/logo.png)｜8,206 bytes｜`d95b616e980d`

- [`resources/osc/photo/namecard.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/resources/osc/photo/namecard.png)｜43,555 bytes｜`db985fab48c5`

- [`scripts/CRON_SETUP.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/CRON_SETUP.md)｜260 bytes｜`f0e28110a870`

- [`scripts/FIX_SILENT_EXCEPT_README.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/FIX_SILENT_EXCEPT_README.txt)｜2,266 bytes｜`c46e0860d514`

- [`scripts/ci/shell_true_grandfather.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ci/shell_true_grandfather.txt)｜354 bytes｜`37a3807c110d`

- [`scripts/cloudflare_tunnel.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/cloudflare_tunnel.sh)｜2,547 bytes｜`eb099ff5def1`

- [`scripts/fix_all_services.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/fix_all_services.sh)｜5,800 bytes｜`e0d8016d5def`

- [`scripts/fix_omlx_watchdog.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/fix_omlx_watchdog.sh)｜3,643 bytes｜`ee21d4dbd34b`

- [`scripts/magi_cli.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/magi_cli.sh)｜23,497 bytes｜`a59b94fdc318`

- [`scripts/omlx_patch_and_start.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/omlx_patch_and_start.sh)｜8,332 bytes｜`d0387db975a8`

- [`scripts/omlx_watchdog.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/omlx_watchdog.sh)｜16,034 bytes｜`094a27dab937`

- [`scripts/ops/cleanup_sensitive_data.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/cleanup_sensitive_data.sh)｜3,504 bytes｜`b29e31236513`

- [`scripts/ops/install_quickpiper.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/install_quickpiper.sh)｜822 bytes｜`79574ef096a6`

- [`scripts/ops/migrate_laf_status_20260426_schema.sql`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/migrate_laf_status_20260426_schema.sql)｜703 bytes｜`e72c6f9b8635`

- [`scripts/ops/official_api_night_once.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/official_api_night_once.sh)｜3,216 bytes｜`5fea75640ff4`

- [`scripts/ops/start_magi_daemon_launchd.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/ops/start_magi_daemon_launchd.sh)｜619 bytes｜`3f27ad747a2b`

- [`scripts/run_crawler.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/run_crawler.sh)｜977 bytes｜`aa6c1acf0210`

- [`scripts/run_db_sync.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/run_db_sync.sh)｜415 bytes｜`88cb3253aedc`

- [`scripts/run_judicial_pull.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/run_judicial_pull.sh)｜986 bytes｜`184737058fe6`

- [`scripts/run_nightly_guardian.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/run_nightly_guardian.sh)｜9,479 bytes｜`4b7398ab1684`

- [`scripts/run_reprocess_insights.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/run_reprocess_insights.sh)｜1,316 bytes｜`78433fd33899`

- [`scripts/run_resummary.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/run_resummary.sh)｜481 bytes｜`75f1911accea`

- [`scripts/start_paperclip_share_tunnel.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/start_paperclip_share_tunnel.sh)｜2,630 bytes｜`ea411364ee65`

- [`scripts/v3_validation/route-method-review-supplement.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/route-method-review-supplement.json)｜62,178 bytes｜`83283ed28d9e`

- [`scripts/v3_validation/route-method-review.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/route-method-review.json)｜149,232 bytes｜`c3b86ea84785`

- [`scripts/v3_validation/route-success-proof-review.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/route-success-proof-review.json)｜49,563 bytes｜`3ea31653896d`

- [`scripts/v3_validation/schemas/isolated-live-execution-plan.schema.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schemas/isolated-live-execution-plan.schema.json)｜3,465 bytes｜`4023cdefb081`

- [`scripts/v3_validation/schemas/live-validation-plan.schema.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schemas/live-validation-plan.schema.json)｜3,526 bytes｜`490ca78a6153`

- [`scripts/v3_validation/schemas/live-validation-report.schema.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schemas/live-validation-report.schema.json)｜3,858 bytes｜`f2088edeb29c`

- [`scripts/v3_validation/schemas/replay-fixture.schema.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/scripts/v3_validation/schemas/replay-fixture.schema.json)｜4,404 bytes｜`b196f8057423`

- [`skills/apple/SETUP_SHORTCUTS.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/apple/SETUP_SHORTCUTS.md)｜6,799 bytes｜`b241f1f75cfb`

- [`skills/auto-magi-skill/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/auto-magi-skill/SKILL.md)｜454 bytes｜`b573461e2ea9`

- [`skills/autoresearch/.gitignore`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/.gitignore)｜279 bytes｜`ab8c4b08f855`

- [`skills/autoresearch/.python-version`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/.python-version)｜5 bytes｜`7a41a41354ab`

- [`skills/autoresearch/README.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/README.md)｜8,039 bytes｜`3958fd4195ac`

- [`skills/autoresearch/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/SKILL.md)｜1,279 bytes｜`57aa2f60bcdc`

- [`skills/autoresearch/analysis.ipynb`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/analysis.ipynb)｜8,208 bytes｜`7c31705130d3`

- [`skills/autoresearch/doc.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/doc.md)｜1,059 bytes｜`643fa74ae75f`

- [`skills/autoresearch/program.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/program.md)｜7,039 bytes｜`86cf987a5c38`

- [`skills/autoresearch/progress.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/progress.png)｜252,961 bytes｜`a6be2ad8dece`

- [`skills/autoresearch/pyproject.toml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/autoresearch/pyproject.toml)｜543 bytes｜`675c150a9e07`

- [`skills/bilingual-docx/references/translation_style_examples.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bilingual-docx/references/translation_style_examples.md)｜3,166 bytes｜`6edcacba6487`

- [`skills/bilingual-docx/scripts/audit.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bilingual-docx/scripts/audit.js)｜8,937 bytes｜`a8462ab2bdc6`

- [`skills/bilingual-docx/scripts/build_template.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bilingual-docx/scripts/build_template.js)｜8,688 bytes｜`a32759e04e91`

- [`skills/bilingual-docx/scripts/normalize.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bilingual-docx/scripts/normalize.js)｜6,144 bytes｜`d2eee6228492`

- [`skills/bilingual-docx/scripts/parse_reference.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bilingual-docx/scripts/parse_reference.js)｜2,454 bytes｜`40ca4eaf2f36`

- [`skills/brain_manager/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/brain_manager/SKILL.md)｜3,082 bytes｜`e6043667c62a`

- [`skills/brief-gen/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/brief-gen/SKILL.md)｜2,657 bytes｜`59b490fcdd9f`

- [`skills/browser/WEB_AUTOMATION_GUIDE.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/browser/WEB_AUTOMATION_GUIDE.md)｜33,505 bytes｜`a1477f9aafaf`

- [`skills/casper-autofix-knowledge/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper-autofix-knowledge/SKILL.md)｜603 bytes｜`018765049fed`

- [`skills/casper-autofix-knowledge/knowledge_seed.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/casper-autofix-knowledge/knowledge_seed.json)｜548 bytes｜`8cd3cc08cacb`

- [`skills/contract-review/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/contract-review/SKILL.md)｜3,017 bytes｜`42cdc6f23965`

- [`skills/contract-review/evals/evals.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/contract-review/evals/evals.json)｜2,454 bytes｜`5d2d6ba9d2ae`

- [`skills/contract-review/references/vendor_standard.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/contract-review/references/vendor_standard.txt)｜2,589 bytes｜`74713555f5b6`

- [`skills/court-hearing-reminder/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/court-hearing-reminder/SKILL.md)｜1,854 bytes｜`a7600b11d16a`

- [`skills/crawler-targets/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/crawler-targets/SKILL.md)｜1,157 bytes｜`0cf682c4b08f`

- [`skills/db-dual-sync/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/db-dual-sync/SKILL.md)｜854 bytes｜`0b55985e7a3d`

- [`skills/definitions.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/definitions.json)｜162,034 bytes｜`680c3ac3c2c8`

- [`skills/doc-producer/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/doc-producer/SKILL.md)｜740 bytes｜`e02b70882ac8`

- [`skills/docx-editor/README.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/README.md)｜5,225 bytes｜`60b72eb42349`

- [`skills/docx-editor/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/SKILL.md)｜1,458 bytes｜`a052dd0defae`

- [`skills/docx-editor/SUMMARY_phases.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/SUMMARY_phases.md)｜615 bytes｜`d31c83d29981`

- [`skills/docx-editor/manifest.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx-editor/manifest.json)｜378 bytes｜`9363acaf8cd3`

- [`skills/docx/LICENSE.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/LICENSE.txt)｜1,467 bytes｜`79f6d8f5b427`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd)｜74,984 bytes｜`41b93bd8857c`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chartDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chartDrawing.xsd)｜6,956 bytes｜`3fd0586f2637`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-diagram.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-diagram.xsd)｜51,302 bytes｜`29b254ee0d10`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-lockedCanvas.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-lockedCanvas.xsd)｜624 bytes｜`5cb76dabd8b9`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-main.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-main.xsd)｜152,039 bytes｜`5375417f0f53`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-picture.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-picture.xsd)｜1,231 bytes｜`5d389d42befb`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-spreadsheetDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-spreadsheetDrawing.xsd)｜8,862 bytes｜`b4532b6d2588`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-wordprocessingDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-wordprocessingDrawing.xsd)｜14,795 bytes｜`bdad416b096b`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/pml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/pml.xsd)｜83,612 bytes｜`d173c3e5d61e`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-additionalCharacteristics.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-additionalCharacteristics.xsd)｜1,269 bytes｜`3c6709101c6a`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-bibliography.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-bibliography.xsd)｜7,328 bytes｜`0b364451dc36`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-commonSimpleTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-commonSimpleTypes.xsd)｜6,382 bytes｜`e2abacbb9a55`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlDataProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlDataProperties.xsd)｜1,248 bytes｜`0ef4bb354ff4`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlSchemaProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlSchemaProperties.xsd)｜880 bytes｜`0d103b99a4a8`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesCustom.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesCustom.xsd)｜2,608 bytes｜`9c085407751b`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesExtended.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesExtended.xsd)｜3,507 bytes｜`bc92e36ccd23`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesVariantTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesVariantTypes.xsd)｜7,507 bytes｜`7b5b7413e2c8`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-math.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-math.xsd)｜23,313 bytes｜`3213ef163160`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-relationshipReference.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-relationshipReference.xsd)｜1,367 bytes｜`12264f3c03d7`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd)｜242,277 bytes｜`beffeed56945`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-main.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-main.xsd)｜26,148 bytes｜`f5ee623b08b6`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-officeDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-officeDrawing.xsd)｜25,279 bytes｜`585bedc1313b`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-presentationDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-presentationDrawing.xsd)｜535 bytes｜`133c9f64a5c5`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-spreadsheetDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-spreadsheetDrawing.xsd)｜5,712 bytes｜`6bdeb169c371`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-wordprocessingDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-wordprocessingDrawing.xsd)｜4,010 bytes｜`475dcae1e7d1`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd)｜171,367 bytes｜`c2dd9f61f892`

- [`skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd)｜4,646 bytes｜`a539aa2fb154`

- [`skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-contentTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-contentTypes.xsd)｜1,963 bytes｜`9e0b7209fc69`

- [`skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-coreProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-coreProperties.xsd)｜2,515 bytes｜`451958454e85`

- [`skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-digSig.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-digSig.xsd)｜2,856 bytes｜`6de111e11403`

- [`skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd)｜1,344 bytes｜`f565adfef5a5`

- [`skills/docx/scripts/office/schemas/mce/mc.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/mce/mc.xsd)｜3,127 bytes｜`3a37e461ecf5`

- [`skills/docx/scripts/office/schemas/microsoft/wml-2010.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-2010.xsd)｜26,549 bytes｜`568b26ee156c`

- [`skills/docx/scripts/office/schemas/microsoft/wml-2012.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-2012.xsd)｜3,745 bytes｜`0fa75578a000`

- [`skills/docx/scripts/office/schemas/microsoft/wml-2018.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-2018.xsd)｜901 bytes｜`be0ff793a22d`

- [`skills/docx/scripts/office/schemas/microsoft/wml-cex-2018.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-cex-2018.xsd)｜1,778 bytes｜`fddc2b880cab`

- [`skills/docx/scripts/office/schemas/microsoft/wml-cid-2016.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-cid-2016.xsd)｜1,002 bytes｜`127ca209fa73`

- [`skills/docx/scripts/office/schemas/microsoft/wml-sdtdatahash-2020.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-sdtdatahash-2020.xsd)｜600 bytes｜`842e7163409c`

- [`skills/docx/scripts/office/schemas/microsoft/wml-symex-2015.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/office/schemas/microsoft/wml-symex-2015.xsd)｜745 bytes｜`16f6f8072249`

- [`skills/docx/scripts/templates/comments.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/templates/comments.xml)｜2,603 bytes｜`a08ba83ee879`

- [`skills/docx/scripts/templates/commentsExtended.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/templates/commentsExtended.xml)｜2,611 bytes｜`544eeecfecee`

- [`skills/docx/scripts/templates/commentsExtensible.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/templates/commentsExtensible.xml)｜2,707 bytes｜`bad10b3283e6`

- [`skills/docx/scripts/templates/commentsIds.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/templates/commentsIds.xml)｜2,619 bytes｜`db20f9616e00`

- [`skills/docx/scripts/templates/people.xml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/docx/scripts/templates/people.xml)｜115 bytes｜`056f63aa1197`

- [`skills/engine/apple_translation/_sidecar/build.sh`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/apple_translation/_sidecar/build.sh)｜429 bytes｜`1b35102f3c59`

- [`skills/engine/apple_translation/_sidecar/magi_translator_sidecar`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/apple_translation/_sidecar/magi_translator_sidecar)｜100,424 bytes｜`110f5a50a38b`

- [`skills/engine/apple_translation/_sidecar/main.swift`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/apple_translation/_sidecar/main.swift)｜3,749 bytes｜`5e72ce89021b`

- [`skills/engine/legal_dict.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/legal_dict.txt)｜557 bytes｜`f67c43ed905b`

- [`skills/engine/stopwords_zh.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/engine/stopwords_zh.txt)｜302 bytes｜`e90b85ff5581`

- [`skills/evidence-admissibility/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evidence-admissibility/SKILL.md)｜7,626 bytes｜`386d1b2d7996`

- [`skills/evidence-admissibility/references/admissibility_rules.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evidence-admissibility/references/admissibility_rules.md)｜5,440 bytes｜`3744eeb8e647`

- [`skills/evolution/iron_dome_dynamic_rules.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/evolution/iron_dome_dynamic_rules.json)｜1,288 bytes｜`7a4fac2e4a26`

- [`skills/file-review-orchestrator/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/file-review-orchestrator/SKILL.md)｜5,236 bytes｜`e963553513a5`

- [`skills/forensic-transcript-verifier/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/SKILL.md)｜9,067 bytes｜`939051405db8`

- [`skills/forensic-transcript-verifier/agents/openai.yaml`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/agents/openai.yaml)｜348 bytes｜`6db51a217b4a`

- [`skills/forensic-transcript-verifier/references/verification-protocol.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/references/verification-protocol.md)｜7,095 bytes｜`9641b31d8727`

- [`skills/forensic-transcript-verifier/scripts/write_transcript_docx.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/forensic-transcript-verifier/scripts/write_transcript_docx.js)｜5,420 bytes｜`2396e472618e`

- [`skills/gmail-drafts/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/gmail-drafts/SKILL.md)｜1,012 bytes｜`ff113fb1db73`

- [`skills/insight-refine/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/insight-refine/SKILL.md)｜514 bytes｜`90ad9091c47d`

- [`skills/insight-refine/meta.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/insight-refine/meta.json)｜193 bytes｜`18e43c3bcd47`

- [`skills/interpreter-empirical-classifier/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/interpreter-empirical-classifier/SKILL.md)｜3,239 bytes｜`91b523cba6d4`

- [`skills/iron-dome/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron-dome/SKILL.md)｜1,005 bytes｜`8f01ac550b3d`

- [`skills/iron_dome/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/iron_dome/SKILL.md)｜709 bytes｜`e0293f70832e`

- [`skills/judgment-collector/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judgment-collector/SKILL.md)｜10,136 bytes｜`209a6221d4f7`

- [`skills/judicial-flow-search-archive/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-flow-search-archive/SKILL.md)｜2,239 bytes｜`d935c9ad8cb5`

- [`skills/judicial-tools/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-tools/SKILL.md)｜3,813 bytes｜`c18ffacd5811`

- [`skills/judicial-web-search/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/judicial-web-search/SKILL.md)｜1,936 bytes｜`ab92d8c09e7a`

- [`skills/labor-law-calculator/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/labor-law-calculator/SKILL.md)｜3,178 bytes｜`82ad0f0fb25c`

- [`skills/laf-orchestrator/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-orchestrator/SKILL.md)｜23,706 bytes｜`4d1028330905`

- [`skills/laf-portal-automation/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-portal-automation/SKILL.md)｜13,625 bytes｜`dc33a56a669f`

- [`skills/laf-portal-automation/references/snapshot_training.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-portal-automation/references/snapshot_training.json)｜180,416 bytes｜`c71ab494b4b5`

- [`skills/laf-refine-case/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-refine-case/SKILL.md)｜427 bytes｜`7edeac3bc7ba`

- [`skills/laf-withdrawal-report/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/laf-withdrawal-report/SKILL.md)｜757 bytes｜`bdf8060eb751`

- [`skills/legal_attest/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/legal_attest/SKILL.md)｜1,969 bytes｜`f34545df8ccc`

- [`skills/magi-autopilot/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi-autopilot/SKILL.md)｜1,857 bytes｜`26b0d39d3722`

- [`skills/magi-doctor/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi-doctor/SKILL.md)｜1,082 bytes｜`7acecee324ed`

- [`skills/magi-self-repair/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/magi-self-repair/SKILL.md)｜823 bytes｜`9db4876b06ef`

- [`skills/market-briefing/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/market-briefing/SKILL.md)｜4,975 bytes｜`f4979938332f`

- [`skills/mock-test/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/mock-test/SKILL.md)｜1,420 bytes｜`91bda329318b`

- [`skills/obsidian/OBSIDIAN_FORMAT.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/obsidian/OBSIDIAN_FORMAT.md)｜1,952 bytes｜`6b4b5c445b54`

- [`skills/obsidian/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/obsidian/SKILL.md)｜2,929 bytes｜`7086486651e9`

- [`skills/ops/_docx_table_gen.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/_docx_table_gen.js)｜16,675 bytes｜`91e04d4b421b`

- [`skills/ops/database/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/database/SKILL.md)｜1,371 bytes｜`aa0ce976892d`

- [`skills/ops/sunrise_protocol/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/sunrise_protocol/SKILL.md)｜971 bytes｜`7ffe4f5ba195`

- [`skills/osc-orchestrator/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-orchestrator/SKILL.md)｜2,145 bytes｜`6806f9959a71`

- [`skills/osc-scan-folder/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/osc-scan-folder/SKILL.md)｜464 bytes｜`1cb97eb0c844`

- [`skills/pdf-annotator/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-annotator/SKILL.md)｜1,615 bytes｜`6a079e9cef94`

- [`skills/pdf-bookmarker/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-bookmarker/SKILL.md)｜5,408 bytes｜`ee7e2f67a5da`

- [`skills/pdf-namer/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/SKILL.md)｜3,182 bytes｜`a49bda01fd99`

- [`skills/pdf-namer/few_shot_prompt.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf-namer/few_shot_prompt.md)｜3,682 bytes｜`4e6efe9a3461`

- [`skills/pdf/LICENSE.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/LICENSE.txt)｜1,467 bytes｜`79f6d8f5b427`

- [`skills/pdf/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/SKILL.md)｜8,311 bytes｜`a0373416c6b2`

- [`skills/pdf/forms.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/forms.md)｜11,854 bytes｜`9530b3f57034`

- [`skills/pdf/reference.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pdf/reference.md)｜16,692 bytes｜`03a5f964f8ab`

- [`skills/pptx/LICENSE.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/LICENSE.txt)｜1,467 bytes｜`79f6d8f5b427`

- [`skills/pptx/editing.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/editing.md)｜6,885 bytes｜`6cb47c3ab17e`

- [`skills/pptx/pptxgenjs.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/pptxgenjs.md)｜12,819 bytes｜`9539534d92b7`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd)｜74,984 bytes｜`41b93bd8857c`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chartDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chartDrawing.xsd)｜6,956 bytes｜`3fd0586f2637`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-diagram.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-diagram.xsd)｜51,302 bytes｜`29b254ee0d10`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-lockedCanvas.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-lockedCanvas.xsd)｜624 bytes｜`5cb76dabd8b9`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-main.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-main.xsd)｜152,039 bytes｜`5375417f0f53`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-picture.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-picture.xsd)｜1,231 bytes｜`5d389d42befb`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-spreadsheetDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-spreadsheetDrawing.xsd)｜8,862 bytes｜`b4532b6d2588`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-wordprocessingDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-wordprocessingDrawing.xsd)｜14,795 bytes｜`bdad416b096b`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/pml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/pml.xsd)｜83,612 bytes｜`d173c3e5d61e`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-additionalCharacteristics.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-additionalCharacteristics.xsd)｜1,269 bytes｜`3c6709101c6a`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-bibliography.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-bibliography.xsd)｜7,328 bytes｜`0b364451dc36`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-commonSimpleTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-commonSimpleTypes.xsd)｜6,382 bytes｜`e2abacbb9a55`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlDataProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlDataProperties.xsd)｜1,248 bytes｜`0ef4bb354ff4`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlSchemaProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlSchemaProperties.xsd)｜880 bytes｜`0d103b99a4a8`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesCustom.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesCustom.xsd)｜2,608 bytes｜`9c085407751b`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesExtended.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesExtended.xsd)｜3,507 bytes｜`bc92e36ccd23`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesVariantTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesVariantTypes.xsd)｜7,507 bytes｜`7b5b7413e2c8`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-math.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-math.xsd)｜23,313 bytes｜`3213ef163160`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-relationshipReference.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-relationshipReference.xsd)｜1,367 bytes｜`12264f3c03d7`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd)｜242,277 bytes｜`beffeed56945`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-main.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-main.xsd)｜26,148 bytes｜`f5ee623b08b6`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-officeDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-officeDrawing.xsd)｜25,279 bytes｜`585bedc1313b`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-presentationDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-presentationDrawing.xsd)｜535 bytes｜`133c9f64a5c5`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-spreadsheetDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-spreadsheetDrawing.xsd)｜5,712 bytes｜`6bdeb169c371`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-wordprocessingDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-wordprocessingDrawing.xsd)｜4,010 bytes｜`475dcae1e7d1`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd)｜171,367 bytes｜`c2dd9f61f892`

- [`skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd)｜4,646 bytes｜`a539aa2fb154`

- [`skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-contentTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-contentTypes.xsd)｜1,963 bytes｜`9e0b7209fc69`

- [`skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-coreProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-coreProperties.xsd)｜2,515 bytes｜`451958454e85`

- [`skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-digSig.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-digSig.xsd)｜2,856 bytes｜`6de111e11403`

- [`skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd)｜1,344 bytes｜`f565adfef5a5`

- [`skills/pptx/scripts/office/schemas/mce/mc.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/mce/mc.xsd)｜3,127 bytes｜`3a37e461ecf5`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-2010.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-2010.xsd)｜26,549 bytes｜`568b26ee156c`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-2012.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-2012.xsd)｜3,745 bytes｜`0fa75578a000`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-2018.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-2018.xsd)｜901 bytes｜`be0ff793a22d`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-cex-2018.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-cex-2018.xsd)｜1,778 bytes｜`fddc2b880cab`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-cid-2016.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-cid-2016.xsd)｜1,002 bytes｜`127ca209fa73`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-sdtdatahash-2020.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-sdtdatahash-2020.xsd)｜600 bytes｜`842e7163409c`

- [`skills/pptx/scripts/office/schemas/microsoft/wml-symex-2015.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/pptx/scripts/office/schemas/microsoft/wml-symex-2015.xsd)｜745 bytes｜`16f6f8072249`

- [`skills/process-hygiene/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/process-hygiene/SKILL.md)｜1,226 bytes｜`c23186c026cb`

- [`skills/research-brief/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/SKILL.md)｜4,354 bytes｜`2468c5893504`

- [`skills/research-brief/seeds/east_asia.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/seeds/east_asia.json)｜4,753 bytes｜`7d1e48fc364b`

- [`skills/research-brief/seeds/ethnicity.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/seeds/ethnicity.json)｜4,979 bytes｜`a4232c05d15e`

- [`skills/research-brief/seeds/human_rights.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/seeds/human_rights.json)｜3,871 bytes｜`63d16dddaa93`

- [`skills/research-brief/seeds/interpretation.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/seeds/interpretation.json)｜3,381 bytes｜`72b2a46b299e`

- [`skills/research-brief/seeds/language_policy.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/research-brief/seeds/language_policy.json)｜3,971 bytes｜`24f5474b3e4a`

- [`skills/screenshot-sorter-tw/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/screenshot-sorter-tw/SKILL.md)｜9,941 bytes｜`6eb8855ba04d`

- [`skills/statutes-vdb/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/statutes-vdb/SKILL.md)｜1,813 bytes｜`cc61d04ad630`

- [`skills/transcript-downloader/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/transcript-downloader/SKILL.md)｜2,208 bytes｜`fd63394c9367`

- [`skills/transcript-indexer/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/transcript-indexer/SKILL.md)｜2,557 bytes｜`af0e08b3ab82`

- [`skills/transcript-todo-extractor/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/transcript-todo-extractor/SKILL.md)｜1,690 bytes｜`0e5c2f61e9e0`

- [`skills/translator/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/translator/SKILL.md)｜5,028 bytes｜`4a2ac462ecc3`

- [`skills/trial-prep/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/trial-prep/SKILL.md)｜1,801 bytes｜`30e91d59b4b7`

- [`skills/worldmonitor-intel/SKILL.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/worldmonitor-intel/SKILL.md)｜1,507 bytes｜`7b7a025c8361`

- [`skills/xlsx/LICENSE.txt`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/LICENSE.txt)｜1,467 bytes｜`79f6d8f5b427`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd)｜74,984 bytes｜`41b93bd8857c`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chartDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chartDrawing.xsd)｜6,956 bytes｜`3fd0586f2637`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-diagram.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-diagram.xsd)｜51,302 bytes｜`29b254ee0d10`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-lockedCanvas.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-lockedCanvas.xsd)｜624 bytes｜`5cb76dabd8b9`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-main.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-main.xsd)｜152,039 bytes｜`5375417f0f53`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-picture.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-picture.xsd)｜1,231 bytes｜`5d389d42befb`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-spreadsheetDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-spreadsheetDrawing.xsd)｜8,862 bytes｜`b4532b6d2588`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-wordprocessingDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-wordprocessingDrawing.xsd)｜14,795 bytes｜`bdad416b096b`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/pml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/pml.xsd)｜83,612 bytes｜`d173c3e5d61e`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-additionalCharacteristics.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-additionalCharacteristics.xsd)｜1,269 bytes｜`3c6709101c6a`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-bibliography.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-bibliography.xsd)｜7,328 bytes｜`0b364451dc36`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-commonSimpleTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-commonSimpleTypes.xsd)｜6,382 bytes｜`e2abacbb9a55`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlDataProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlDataProperties.xsd)｜1,248 bytes｜`0ef4bb354ff4`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlSchemaProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-customXmlSchemaProperties.xsd)｜880 bytes｜`0d103b99a4a8`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesCustom.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesCustom.xsd)｜2,608 bytes｜`9c085407751b`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesExtended.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesExtended.xsd)｜3,507 bytes｜`bc92e36ccd23`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesVariantTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-documentPropertiesVariantTypes.xsd)｜7,507 bytes｜`7b5b7413e2c8`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-math.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-math.xsd)｜23,313 bytes｜`3213ef163160`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-relationshipReference.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/shared-relationshipReference.xsd)｜1,367 bytes｜`12264f3c03d7`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd)｜242,277 bytes｜`beffeed56945`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-main.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-main.xsd)｜26,148 bytes｜`f5ee623b08b6`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-officeDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-officeDrawing.xsd)｜25,279 bytes｜`585bedc1313b`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-presentationDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-presentationDrawing.xsd)｜535 bytes｜`133c9f64a5c5`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-spreadsheetDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-spreadsheetDrawing.xsd)｜5,712 bytes｜`6bdeb169c371`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-wordprocessingDrawing.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/vml-wordprocessingDrawing.xsd)｜4,010 bytes｜`475dcae1e7d1`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd)｜171,367 bytes｜`c2dd9f61f892`

- [`skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd)｜4,646 bytes｜`a539aa2fb154`

- [`skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-contentTypes.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-contentTypes.xsd)｜1,963 bytes｜`9e0b7209fc69`

- [`skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-coreProperties.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-coreProperties.xsd)｜2,515 bytes｜`451958454e85`

- [`skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-digSig.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-digSig.xsd)｜2,856 bytes｜`6de111e11403`

- [`skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/ecma/fouth-edition/opc-relationships.xsd)｜1,344 bytes｜`f565adfef5a5`

- [`skills/xlsx/scripts/office/schemas/mce/mc.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/mce/mc.xsd)｜3,127 bytes｜`3a37e461ecf5`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-2010.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-2010.xsd)｜26,549 bytes｜`568b26ee156c`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-2012.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-2012.xsd)｜3,745 bytes｜`0fa75578a000`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-2018.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-2018.xsd)｜901 bytes｜`be0ff793a22d`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-cex-2018.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-cex-2018.xsd)｜1,778 bytes｜`fddc2b880cab`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-cid-2016.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-cid-2016.xsd)｜1,002 bytes｜`127ca209fa73`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-sdtdatahash-2020.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-sdtdatahash-2020.xsd)｜600 bytes｜`842e7163409c`

- [`skills/xlsx/scripts/office/schemas/microsoft/wml-symex-2015.xsd`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/xlsx/scripts/office/schemas/microsoft/wml-symex-2015.xsd)｜745 bytes｜`16f6f8072249`

- [`static/exam_tutor/choice_bank.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/choice_bank.json)｜3,123,238 bytes｜`53a47488aabe`

- [`static/exam_tutor/curated_practice_weights.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/curated_practice_weights.json)｜350,911 bytes｜`b93f69de1806`

- [`static/exam_tutor/essay-source-pdfs/essay-111-business-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-business-official-rubric.pdf)｜424,150 bytes｜`5c55924fdeba`

- [`static/exam_tutor/essay-source-pdfs/essay-111-business-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-business-question.pdf)｜182,312 bytes｜`80bfd9ae04e7`

- [`static/exam_tutor/essay-source-pdfs/essay-111-civil_1-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-civil_1-official-rubric.pdf)｜396,160 bytes｜`28fb6a3f7b2c`

- [`static/exam_tutor/essay-source-pdfs/essay-111-civil_1-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-civil_1-question.pdf)｜144,016 bytes｜`58a9f9816404`

- [`static/exam_tutor/essay-source-pdfs/essay-111-civil_2-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-civil_2-official-rubric.pdf)｜367,814 bytes｜`5557028506a2`

- [`static/exam_tutor/essay-source-pdfs/essay-111-civil_2-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-civil_2-question.pdf)｜135,113 bytes｜`fc746f1be170`

- [`static/exam_tutor/essay-source-pdfs/essay-111-constitutional-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-constitutional-official-rubric.pdf)｜521,089 bytes｜`9c456bd9881b`

- [`static/exam_tutor/essay-source-pdfs/essay-111-constitutional-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-constitutional-question.pdf)｜192,808 bytes｜`d7aa2ced545d`

- [`static/exam_tutor/essay-source-pdfs/essay-111-criminal-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-criminal-official-rubric.pdf)｜509,743 bytes｜`96650e5f5363`

- [`static/exam_tutor/essay-source-pdfs/essay-111-criminal-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-criminal-question.pdf)｜154,118 bytes｜`4ec148b4bf21`

- [`static/exam_tutor/essay-source-pdfs/essay-111-ip-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-ip-official-rubric.pdf)｜413,282 bytes｜`9847b0c677e5`

- [`static/exam_tutor/essay-source-pdfs/essay-111-ip-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-ip-question.pdf)｜129,669 bytes｜`10d93381354a`

- [`static/exam_tutor/essay-source-pdfs/essay-111-labor-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-labor-official-rubric.pdf)｜382,126 bytes｜`0c2e64c2e1f6`

- [`static/exam_tutor/essay-source-pdfs/essay-111-labor-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-labor-question.pdf)｜162,871 bytes｜`fe5e5568e357`

- [`static/exam_tutor/essay-source-pdfs/essay-111-maritime-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-maritime-official-rubric.pdf)｜494,396 bytes｜`38e482facacf`

- [`static/exam_tutor/essay-source-pdfs/essay-111-maritime-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-maritime-question.pdf)｜136,947 bytes｜`f3dbdbf7a1a6`

- [`static/exam_tutor/essay-source-pdfs/essay-111-tax-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-tax-official-rubric.pdf)｜350,596 bytes｜`7bf1ac373809`

- [`static/exam_tutor/essay-source-pdfs/essay-111-tax-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-111-tax-question.pdf)｜151,487 bytes｜`8ff0bd2e839d`

- [`static/exam_tutor/essay-source-pdfs/essay-112-business-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-business-official-rubric.pdf)｜269,631 bytes｜`b41e484b4e90`

- [`static/exam_tutor/essay-source-pdfs/essay-112-business-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-business-question.pdf)｜142,916 bytes｜`89b65c903d20`

- [`static/exam_tutor/essay-source-pdfs/essay-112-civil_1-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-civil_1-official-rubric.pdf)｜218,401 bytes｜`f8b88f0c8595`

- [`static/exam_tutor/essay-source-pdfs/essay-112-civil_1-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-civil_1-question.pdf)｜149,884 bytes｜`6f45ca0424c1`

- [`static/exam_tutor/essay-source-pdfs/essay-112-civil_2-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-civil_2-official-rubric.pdf)｜191,209 bytes｜`d9576b516cea`

- [`static/exam_tutor/essay-source-pdfs/essay-112-civil_2-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-civil_2-question.pdf)｜140,393 bytes｜`0fb1a961eb42`

- [`static/exam_tutor/essay-source-pdfs/essay-112-constitutional-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-constitutional-official-rubric.pdf)｜245,331 bytes｜`ad9cea6d8851`

- [`static/exam_tutor/essay-source-pdfs/essay-112-constitutional-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-constitutional-question.pdf)｜187,103 bytes｜`9f1093400f60`

- [`static/exam_tutor/essay-source-pdfs/essay-112-criminal-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-criminal-official-rubric.pdf)｜262,896 bytes｜`af8d00b56594`

- [`static/exam_tutor/essay-source-pdfs/essay-112-criminal-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-criminal-question.pdf)｜153,453 bytes｜`fe66ab833fb2`

- [`static/exam_tutor/essay-source-pdfs/essay-112-ip-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-ip-official-rubric.pdf)｜225,984 bytes｜`64661c8a9f87`

- [`static/exam_tutor/essay-source-pdfs/essay-112-ip-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-ip-question.pdf)｜147,091 bytes｜`4f853af3d7cd`

- [`static/exam_tutor/essay-source-pdfs/essay-112-labor-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-labor-official-rubric.pdf)｜259,486 bytes｜`ce2d17031c17`

- [`static/exam_tutor/essay-source-pdfs/essay-112-labor-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-labor-question.pdf)｜138,441 bytes｜`30b961548664`

- [`static/exam_tutor/essay-source-pdfs/essay-112-maritime-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-maritime-official-rubric.pdf)｜186,768 bytes｜`a4030bcbe328`

- [`static/exam_tutor/essay-source-pdfs/essay-112-maritime-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-maritime-question.pdf)｜135,633 bytes｜`448a682672f6`

- [`static/exam_tutor/essay-source-pdfs/essay-112-tax-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-tax-official-rubric.pdf)｜136,862 bytes｜`35f33c519fbf`

- [`static/exam_tutor/essay-source-pdfs/essay-112-tax-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-112-tax-question.pdf)｜126,702 bytes｜`c61c2bcf97b1`

- [`static/exam_tutor/essay-source-pdfs/essay-113-business-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-business-official-rubric.pdf)｜214,005 bytes｜`f94876645b08`

- [`static/exam_tutor/essay-source-pdfs/essay-113-business-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-business-question.pdf)｜137,491 bytes｜`74456afd3f9b`

- [`static/exam_tutor/essay-source-pdfs/essay-113-civil_1-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-civil_1-official-rubric.pdf)｜212,501 bytes｜`b7584f6d453e`

- [`static/exam_tutor/essay-source-pdfs/essay-113-civil_1-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-civil_1-question.pdf)｜137,517 bytes｜`b2abb049ad65`

- [`static/exam_tutor/essay-source-pdfs/essay-113-civil_2-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-civil_2-official-rubric.pdf)｜168,047 bytes｜`2b20219f8ec8`

- [`static/exam_tutor/essay-source-pdfs/essay-113-civil_2-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-civil_2-question.pdf)｜134,575 bytes｜`5b680b098d80`

- [`static/exam_tutor/essay-source-pdfs/essay-113-constitutional-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-constitutional-official-rubric.pdf)｜295,216 bytes｜`417d83bc7d5a`

- [`static/exam_tutor/essay-source-pdfs/essay-113-constitutional-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-constitutional-question.pdf)｜274,316 bytes｜`6e5b10081885`

- [`static/exam_tutor/essay-source-pdfs/essay-113-criminal-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-criminal-official-rubric.pdf)｜240,502 bytes｜`18e19cbfcf92`

- [`static/exam_tutor/essay-source-pdfs/essay-113-criminal-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-criminal-question.pdf)｜149,296 bytes｜`9233b216a23d`

- [`static/exam_tutor/essay-source-pdfs/essay-113-ip-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-ip-official-rubric.pdf)｜238,329 bytes｜`0af4b2175327`

- [`static/exam_tutor/essay-source-pdfs/essay-113-ip-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-ip-question.pdf)｜150,067 bytes｜`d8c8bcc0ddd9`

- [`static/exam_tutor/essay-source-pdfs/essay-113-labor-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-labor-official-rubric.pdf)｜259,231 bytes｜`c9fee79a8f7c`

- [`static/exam_tutor/essay-source-pdfs/essay-113-labor-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-labor-question.pdf)｜152,570 bytes｜`dd8b4875989c`

- [`static/exam_tutor/essay-source-pdfs/essay-113-maritime-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-maritime-official-rubric.pdf)｜302,559 bytes｜`0a7b69a504d2`

- [`static/exam_tutor/essay-source-pdfs/essay-113-maritime-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-maritime-question.pdf)｜165,800 bytes｜`3920737e3398`

- [`static/exam_tutor/essay-source-pdfs/essay-113-tax-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-tax-official-rubric.pdf)｜147,463 bytes｜`37bf97de67a6`

- [`static/exam_tutor/essay-source-pdfs/essay-113-tax-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-113-tax-question.pdf)｜129,126 bytes｜`51cba79d99b7`

- [`static/exam_tutor/essay-source-pdfs/essay-114-business-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-business-official-rubric.pdf)｜232,463 bytes｜`f4e25030fc61`

- [`static/exam_tutor/essay-source-pdfs/essay-114-business-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-business-question.pdf)｜126,742 bytes｜`b42c6f813bb4`

- [`static/exam_tutor/essay-source-pdfs/essay-114-civil_1-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-civil_1-official-rubric.pdf)｜216,133 bytes｜`367ebd677b0a`

- [`static/exam_tutor/essay-source-pdfs/essay-114-civil_1-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-civil_1-question.pdf)｜131,382 bytes｜`55c6ad1b063d`

- [`static/exam_tutor/essay-source-pdfs/essay-114-civil_2-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-civil_2-official-rubric.pdf)｜169,606 bytes｜`19c59c5f9f8c`

- [`static/exam_tutor/essay-source-pdfs/essay-114-civil_2-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-civil_2-question.pdf)｜122,789 bytes｜`c842f33a2b1c`

- [`static/exam_tutor/essay-source-pdfs/essay-114-constitutional-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-constitutional-official-rubric.pdf)｜326,666 bytes｜`639411eda0e7`

- [`static/exam_tutor/essay-source-pdfs/essay-114-constitutional-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-constitutional-question.pdf)｜147,791 bytes｜`4a1546c04d66`

- [`static/exam_tutor/essay-source-pdfs/essay-114-criminal-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-criminal-official-rubric.pdf)｜296,145 bytes｜`cc284a3fd255`

- [`static/exam_tutor/essay-source-pdfs/essay-114-criminal-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-criminal-question.pdf)｜147,324 bytes｜`5a15cb9ade33`

- [`static/exam_tutor/essay-source-pdfs/essay-114-ip-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-ip-official-rubric.pdf)｜273,282 bytes｜`35eaebebee84`

- [`static/exam_tutor/essay-source-pdfs/essay-114-ip-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-ip-question.pdf)｜175,906 bytes｜`66fa08a3decd`

- [`static/exam_tutor/essay-source-pdfs/essay-114-labor-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-labor-official-rubric.pdf)｜212,461 bytes｜`553bd5f1fdcb`

- [`static/exam_tutor/essay-source-pdfs/essay-114-labor-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-labor-question.pdf)｜184,690 bytes｜`e4a0ee585a9f`

- [`static/exam_tutor/essay-source-pdfs/essay-114-maritime-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-maritime-official-rubric.pdf)｜241,072 bytes｜`5cc85fcf496e`

- [`static/exam_tutor/essay-source-pdfs/essay-114-maritime-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-maritime-question.pdf)｜170,452 bytes｜`c088e3bb2f10`

- [`static/exam_tutor/essay-source-pdfs/essay-114-tax-official-rubric.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-tax-official-rubric.pdf)｜178,652 bytes｜`866c21c4897b`

- [`static/exam_tutor/essay-source-pdfs/essay-114-tax-question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/essay-source-pdfs/essay-114-tax-question.pdf)｜149,535 bytes｜`eb3a9e83d1f6`

- [`static/exam_tutor/extended_source_catalog.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/extended_source_catalog.json)｜4,383,272 bytes｜`4ab98287fec6`

- [`static/exam_tutor/source-pdfs/115110_0101_answer.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0101_answer.pdf)｜41,709 bytes｜`9ccfba639ce2`

- [`static/exam_tutor/source-pdfs/115110_0101_question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0101_question.pdf)｜525,636 bytes｜`9e3bf63b0266`

- [`static/exam_tutor/source-pdfs/115110_0201_answer.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0201_answer.pdf)｜40,997 bytes｜`b7d1a537424a`

- [`static/exam_tutor/source-pdfs/115110_0201_question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0201_question.pdf)｜459,855 bytes｜`371145d65bd6`

- [`static/exam_tutor/source-pdfs/115110_0202_answer.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0202_answer.pdf)｜43,009 bytes｜`4515b188b311`

- [`static/exam_tutor/source-pdfs/115110_0202_question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0202_question.pdf)｜485,349 bytes｜`18c1b2da978d`

- [`static/exam_tutor/source-pdfs/115110_0301_answer.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0301_answer.pdf)｜41,161 bytes｜`7d80229d5cb5`

- [`static/exam_tutor/source-pdfs/115110_0301_question.pdf`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/source-pdfs/115110_0301_question.pdf)｜581,466 bytes｜`77546ef8219c`

- [`static/exam_tutor/trend_analysis.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/exam_tutor/trend_analysis.json)｜19,683 bytes｜`1e88b761926e`

- [`static/golem/assets/alex.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/golem/assets/alex.png)｜4,570,811 bytes｜`f8126e6094c7`

- [`static/golem/assets/office_bg.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/golem/assets/office_bg.png)｜74,730 bytes｜`4f89f1cf0302`

- [`static/golem/assets/pixel_bg_tech.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/golem/assets/pixel_bg_tech.png)｜8,985,774 bytes｜`5811e196810f`

- [`static/golem/assets/server_rack.png`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/golem/assets/server_rack.png)｜6,010,033 bytes｜`7be5c6e2b573`

- [`static/golem/golem-console.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/golem/golem-console.css)｜24,025 bytes｜`a0cffd48fd31`

- [`static/golem/golem-console.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/golem/golem-console.js)｜31,894 bytes｜`ee02f6b7149d`

- [`static/iron_dome_patterns.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/iron_dome_patterns.json)｜1,628 bytes｜`b02cd5115635`

- [`static/magi-csrf.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/magi-csrf.js)｜1,991 bytes｜`488c9c5e1060`

- [`static/magi-site.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/magi-site.css)｜28,207 bytes｜`144eebc69a9b`

- [`static/magi-theme.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/magi-theme.css)｜16,592 bytes｜`371c4a9aae3b`

- [`static/magi-theme.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/magi-theme.js)｜3,185 bytes｜`76ce02a2ded2`

- [`static/mobile/magi-mobile.svg`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/mobile/magi-mobile.svg)｜602 bytes｜`68533fb85316`

- [`static/mobile/mobile.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/mobile/mobile.css)｜13,723 bytes｜`0cec60e67c51`

- [`static/mobile/mobile.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/mobile/mobile.js)｜9,999 bytes｜`74f085266767`

- [`static/mobile/sw.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/mobile/sw.js)｜2,239 bytes｜`d53693ac2df2`

- [`static/osc/file-manager.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/file-manager.css)｜40,861 bytes｜`88442bf6df60`

- [`static/osc/osc-components.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-components.css)｜56,509 bytes｜`52e843d1163c`

- [`static/osc/osc-events.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-events.js)｜63,626 bytes｜`9e92886efb29`

- [`static/osc/osc-grouping.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-grouping.js)｜3,490 bytes｜`3fd3b476b1bd`

- [`static/osc/osc-mobile.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-mobile.js)｜1,325 bytes｜`992743113fec`

- [`static/osc/osc-polish.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-polish.css)｜10,884 bytes｜`ff32058d3673`

- [`static/osc/osc-responsive.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-responsive.css)｜12,749 bytes｜`e63b20a20ba9`

- [`static/osc/osc-state.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-state.js)｜2,207 bytes｜`0153b552b259`

- [`static/osc/osc-theme.css`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-theme.css)｜13,106 bytes｜`4855d7cf4dd3`

- [`static/osc/osc-ui.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-ui.js)｜28,664 bytes｜`9fc4f463ab7b`

- [`static/osc/osc-utils.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/osc-utils.js)｜30,705 bytes｜`96ffb51b9d53`

- [`static/osc/tabs/accounting.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/accounting.js)｜39,441 bytes｜`db0159009ffc`

- [`static/osc/tabs/admin.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/admin.js)｜33,246 bytes｜`7c97f61336b4`

- [`static/osc/tabs/calendar.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/calendar.js)｜13,045 bytes｜`622031431cdd`

- [`static/osc/tabs/cases.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/cases.js)｜118,893 bytes｜`c7dcbd448723`

- [`static/osc/tabs/checklists.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/checklists.js)｜11,067 bytes｜`2916cdabf6d0`

- [`static/osc/tabs/dashboard.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/dashboard.js)｜6,012 bytes｜`002a1ae02105`

- [`static/osc/tabs/documents.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/documents.js)｜92,983 bytes｜`facee072b0e4`

- [`static/osc/tabs/drafts.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/drafts.js)｜39,525 bytes｜`b7a3ca031d0a`

- [`static/osc/tabs/file_manager.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/file_manager.js)｜104,446 bytes｜`75b65176ad39`

- [`static/osc/tabs/hearing_conflict.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/hearing_conflict.js)｜11,051 bytes｜`0c3223b66b8d`

- [`static/osc/tabs/insights.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/insights.js)｜7,991 bytes｜`cd127bc97acd`

- [`static/osc/tabs/raziel.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/raziel.js)｜13,311 bytes｜`338e62c4bb43`

- [`static/osc/tabs/saas.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/saas.js)｜26,961 bytes｜`df9c894c89f8`

- [`static/osc/tabs/todos.js`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/static/osc/tabs/todos.js)｜10,986 bytes｜`ba3ab0702caf`

- [`templates/cookie_cutter.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/cookie_cutter.html)｜13,917 bytes｜`07d59cf2367f`

- [`templates/dashboard.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/dashboard.html)｜113,753 bytes｜`d81c1bf6b6e5`

- [`templates/dashboard_beginner.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/dashboard_beginner.html)｜14,431 bytes｜`c75a9f39d9f1`

- [`templates/dashboard_nerv.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/dashboard_nerv.html)｜124,789 bytes｜`b670a6877859`

- [`templates/dashboard_website.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/dashboard_website.html)｜4,174 bytes｜`984ef8ad6b5a`

- [`templates/debt_templates/A.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/debt_templates/A.docx)｜26,687 bytes｜`ab5bb690a50d`

- [`templates/debt_templates/B.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/debt_templates/B.docx)｜23,659 bytes｜`d018e780da9e`

- [`templates/debt_templates/C.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/debt_templates/C.docx)｜18,650 bytes｜`0753260b27f5`

- [`templates/debt_templates/D.docx`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/debt_templates/D.docx)｜29,112 bytes｜`36f159c54dab`

- [`templates/debt_templates/all adress - bank.csv`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/debt_templates/all adress - bank.csv)｜1,037 bytes｜`a28129032654`

- [`templates/debt_templates/all adress - company.csv`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/debt_templates/all adress - company.csv)｜1,107 bytes｜`2bb78d4308c1`

- [`templates/exam_tutor.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/exam_tutor.html)｜161,738 bytes｜`468fcbfc03e7`

- [`templates/golem_console.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/golem_console.html)｜12,441 bytes｜`c4cf3fc8e0c5`

- [`templates/intel.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/intel.html)｜12,784 bytes｜`30dfc139177a`

- [`templates/login.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/login.html)｜5,596 bytes｜`376b7a773e4d`

- [`templates/lottery.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/lottery.html)｜11,497 bytes｜`f852b3bb58d8`

- [`templates/mobile_admin.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/mobile_admin.html)｜3,059 bytes｜`26557fe3cba1`

- [`templates/mobile_home.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/mobile_home.html)｜8,049 bytes｜`6fbacad394e4`

- [`templates/osc.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/osc.html)｜12,112 bytes｜`749ccf82cd26`

- [`templates/osc_debt.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/osc_debt.html)｜81,928 bytes｜`2a34af60d625`

- [`templates/partials/osc/accounting.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/accounting.html)｜22,518 bytes｜`71ac2ecad99a`

- [`templates/partials/osc/admin.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/admin.html)｜31,984 bytes｜`055dc803ed48`

- [`templates/partials/osc/archiveWizard.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/archiveWizard.html)｜2,152 bytes｜`621cf5961881`

- [`templates/partials/osc/calendar.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/calendar.html)｜7,436 bytes｜`9566e688ee74`

- [`templates/partials/osc/cases.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/cases.html)｜17,922 bytes｜`ebf576be3a4a`

- [`templates/partials/osc/clients.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/clients.html)｜5,212 bytes｜`db92bac23751`

- [`templates/partials/osc/dashboard.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/dashboard.html)｜4,907 bytes｜`fe1018f2a571`

- [`templates/partials/osc/documentReuse.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/documentReuse.html)｜6,725 bytes｜`151a51309d9c`

- [`templates/partials/osc/documents.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/documents.html)｜15,422 bytes｜`e6db719ac074`

- [`templates/partials/osc/drafts.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/drafts.html)｜16,440 bytes｜`ecaa66525f82`

- [`templates/partials/osc/fileManager.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/fileManager.html)｜10,022 bytes｜`0e14b0a327cb`

- [`templates/partials/osc/forms.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/forms.html)｜10,075 bytes｜`fdb2ae905aca`

- [`templates/partials/osc/hearingConflict.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/hearingConflict.html)｜6,016 bytes｜`12ca0004966f`

- [`templates/partials/osc/insights.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/insights.html)｜4,727 bytes｜`5bcc56f163d1`

- [`templates/partials/osc/laf.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/laf.html)｜5,899 bytes｜`ddeca28c3bf3`

- [`templates/partials/osc/lafWizard.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/lafWizard.html)｜4,257 bytes｜`d45a770681d6`

- [`templates/partials/osc/meetings.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/meetings.html)｜5,437 bytes｜`03a34a850a3b`

- [`templates/partials/osc/pdfTools.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/pdfTools.html)｜4,707 bytes｜`aa163ec9463c`

- [`templates/partials/osc/quotations.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/quotations.html)｜14,244 bytes｜`1a5b938302a3`

- [`templates/partials/osc/raziel.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/raziel.html)｜6,731 bytes｜`ede73f7fc356`

- [`templates/partials/osc/saasWorkbench.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/saasWorkbench.html)｜19,335 bytes｜`293b57ac24c1`

- [`templates/partials/osc/templateFolder.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/templateFolder.html)｜2,483 bytes｜`ebe449514788`

- [`templates/partials/osc/todos.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/partials/osc/todos.html)｜5,512 bytes｜`93a98a4a8569`

- [`templates/process_monitor.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/process_monitor.html)｜9,169 bytes｜`b5892720691b`

- [`templates/public_tools.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/public_tools.html)｜5,977 bytes｜`df60cb208e05`

- [`templates/register.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/register.html)｜4,266 bytes｜`2a455eadd4ef`

- [`templates/research.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/research.html)｜7,062 bytes｜`bceb7651afe0`

- [`templates/research_judgment_classifier.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/research_judgment_classifier.html)｜3,888 bytes｜`aff04105aeee`

- [`templates/rss_preview.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/rss_preview.html)｜3,534 bytes｜`b5fd2a17e7fc`

- [`templates/sentencing_trends.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/sentencing_trends.html)｜17,105 bytes｜`8eb77b5058f1`

- [`templates/video_studio.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/video_studio.html)｜13,830 bytes｜`bfccc2e816e4`

- [`templates/wizard/base.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/base.html)｜11,373 bytes｜`7a4d15034774`

- [`templates/wizard/complete.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/complete.html)｜3,118 bytes｜`1427d9ff065c`

- [`templates/wizard/config.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/config.html)｜9,222 bytes｜`5482b85da42c`

- [`templates/wizard/hardware.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/hardware.html)｜7,780 bytes｜`04f0c69049d5`

- [`templates/wizard/models.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/models.html)｜6,132 bytes｜`49cd44c92216`

- [`templates/wizard/review.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/review.html)｜4,043 bytes｜`6c87f7c8c24e`

- [`templates/wizard/welcome.html`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/templates/wizard/welcome.html)｜3,938 bytes｜`377193cbd212`

- [`tests/README.md`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/README.md)｜1,192 bytes｜`b7bb9e3a9b9f`

- [`tests/v3/compat/behavior_fixtures/osc-file-content.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/behavior_fixtures/osc-file-content.json)｜1,272 bytes｜`fa25c5127abf`

- [`tests/v3/compat/fixtures/external-sse.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/fixtures/external-sse.json)｜1,110 bytes｜`dc5034d478ca`

- [`tests/v3/compat/fixtures/reply-duplicate-headers.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/fixtures/reply-duplicate-headers.json)｜1,452 bytes｜`c9140bdb7500`

- [`tests/v3/compat/fixtures/reply-success.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/fixtures/reply-success.json)｜1,242 bytes｜`387bb1b73d3d`

- [`tests/v3/compat/fixtures/shortcut-error.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/fixtures/shortcut-error.json)｜1,449 bytes｜`8df952905d65`

- [`tests/v3/compat/live/isolated-plan.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/live/isolated-plan.json)｜1,366 bytes｜`268b6436217b`

- [`tests/v3/compat/live/isolated-report.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/compat/live/isolated-report.json)｜1,496 bytes｜`901031d82e09`

- [`tests/v3/evals/business_outcome_regression_corpus.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/tests/v3/evals/business_outcome_regression_corpus.json)｜69 bytes｜`abdc3a3746d8`

- [`third_party/video_autopilot_kit/LICENSE`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/third_party/video_autopilot_kit/LICENSE)｜1,071 bytes｜`7f29ea8ecc94`

- [`third_party/video_autopilot_kit/MAGI_INTEGRATION.json`](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/third_party/video_autopilot_kit/MAGI_INTEGRATION.json)｜768 bytes｜`45bc364045b0`

<a id="appE"></a>
# 附錄 E. API 路由索引

靜態掃描取得 **361** 條 decorator routes；動態 add_url_rule 或 runtime-generated route 請另以 `function_health_index.discover_api_routes()` 的 LIVE report 為準。

| Methods | Route | Handler | Source |
| --- | --- | --- | --- |
| GET,POST | / | index | [api/server.py:821](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L821) |
| POST | /alert | api_alert | [api/tools_api.py:3661](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3661) |
| GET | /api/admin/observability/support-bundle | api_admin_observability_support_bundle | [api/blueprints/admin_runtime.py:1815](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1815) |
| GET | /api/audit_log | api_list_audit_log | [api/tools_api.py:3750](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3750) |
| POST | /api/audit_log/restore/<int:log_id> | api_restore_from_audit | [api/tools_api.py:3804](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3804) |
| GET | /api/codex-distributed/status | api_codex_distributed_status | [api/blueprints/admin_runtime.py:2308](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2308) |
| POST | /api/codex-distributed/toggle | api_codex_distributed_toggle | [api/blueprints/admin_runtime.py:2321](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2321) |
| GET | /api/drive-case-exclusions | api_drive_case_exclusions_list | [api/blueprints/admin_runtime.py:3074](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3074) |
| POST | /api/drive-case-exclusions | api_drive_case_exclusions_add | [api/blueprints/admin_runtime.py:3085](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3085) |
| DELETE | /api/drive-case-exclusions | api_drive_case_exclusions_remove | [api/blueprints/admin_runtime.py:3108](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3108) |
| GET,POST | /api/golem/api-keys | golem_api_keys_api | [api/blueprints/golem_console.py:282](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/golem_console.py#L282) |
| POST | /api/golem/command | golem_command_api | [api/blueprints/golem_console.py:362](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/golem_console.py#L362) |
| GET | /api/golem/logs | golem_logs_api | [api/blueprints/golem_console.py:346](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/golem_console.py#L346) |
| GET | /api/golem/skills | golem_skills_api | [api/blueprints/golem_console.py:337](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/golem_console.py#L337) |
| GET | /api/golem/status | golem_status_api | [api/blueprints/golem_console.py:255](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/golem_console.py#L255) |
| POST | /api/intel/refresh | intel_refresh | [api/blueprints/dashboard_pages.py:569](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L569) |
| POST | /api/iron_dome/broadcast | iron_dome_broadcast | [skills/ops/iron_dome_sync.py:350](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/iron_dome_sync.py#L350) |
| GET | /api/iron_dome/hash | iron_dome_hash | [skills/ops/iron_dome_sync.py:330](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/iron_dome_sync.py#L330) |
| POST | /api/iron_dome/notify | iron_dome_notify | [skills/ops/iron_dome_sync.py:338](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/iron_dome_sync.py#L338) |
| GET | /api/iron_dome/patterns | iron_dome_patterns | [skills/ops/iron_dome_sync.py:334](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/iron_dome_sync.py#L334) |
| GET | /api/iron_dome/status | iron_dome_status | [skills/ops/iron_dome_sync.py:346](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/ops/iron_dome_sync.py#L346) |
| GET | /api/live-log | api_live_log | [api/blueprints/admin_runtime.py:3010](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3010) |
| GET | /api/live-validation | api_live_validation | [api/blueprints/admin_runtime.py:3026](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3026) |
| POST | /api/memory/obsidian-sync | api_memory_obsidian_sync | [api/blueprints/web_runtime.py:960](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L960) |
| POST | /api/memory/recall | api_memory_recall | [api/blueprints/web_runtime.py:920](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L920) |
| POST | /api/memory/remember | api_memory_remember | [api/blueprints/web_runtime.py:941](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L941) |
| GET | /api/memory/stats | api_memory_stats | [api/blueprints/web_runtime.py:841](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L841) |
| GET,POST | /api/nerv/heavy-runtime | api_nerv_heavy_runtime | [api/blueprints/admin_runtime.py:2165](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2165) |
| GET,POST | /api/nerv/product-runtime | api_nerv_product_runtime | [api/blueprints/admin_runtime.py:2125](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2125) |
| GET | /api/nerv/remote-access | api_nerv_remote_access | [api/blueprints/admin_runtime.py:1804](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1804) |
| POST | /api/nerv/remote-access/action | api_nerv_remote_access_action | [api/blueprints/admin_runtime.py:1874](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1874) |
| GET | /api/nerv/skill-interview | api_nerv_skill_interview_status | [api/blueprints/admin_runtime.py:1961](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1961) |
| POST | /api/nerv/skill-interview/reply | api_nerv_skill_interview_reply | [api/blueprints/admin_runtime.py:2002](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2002) |
| POST | /api/nerv/skill-interview/start | api_nerv_skill_interview_start | [api/blueprints/admin_runtime.py:1979](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1979) |
| GET | /api/nerv/skills | api_nerv_skills | [api/blueprints/admin_runtime.py:2114](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2114) |
| GET,POST | /api/nerv/skills/<skill_name> | api_nerv_skill_detail | [api/blueprints/admin_runtime.py:2215](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2215) |
| POST | /api/ops/process-guardian/toggle | process_guardian_toggle_api | [api/blueprints/web_runtime.py:991](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L991) |
| GET | /api/ops/process-monitor | process_monitor_api | [api/blueprints/web_runtime.py:975](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L975) |
| GET,POST | /api/osc/accounting/defaults | osc_accounting_defaults_api | [api/blueprints/osc_accounting.py:401](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L401) |
| GET,PUT,DELETE | /api/osc/accounting/defaults/<int:row_id> | osc_accounting_default_detail_api | [api/blueprints/osc_accounting.py:438](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L438) |
| GET,POST | /api/osc/accounting/import/google-sheet | osc_accounting_google_sheet_import_api | [api/blueprints/osc_accounting.py:302](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L302) |
| GET,POST | /api/osc/accounting/monthly-bonus | osc_accounting_monthly_bonus_api | [api/blueprints/osc_accounting.py:338](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L338) |
| GET | /api/osc/accounting/monthly-bonus/xlsx | osc_accounting_monthly_bonus_xlsx_api | [api/blueprints/osc_accounting.py:375](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L375) |
| GET,POST | /api/osc/accounting/recurring | osc_accounting_recurring_api | [api/blueprints/osc_accounting.py:476](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L476) |
| GET,PUT,DELETE | /api/osc/accounting/recurring/<int:row_id> | osc_accounting_recurring_detail_api | [api/blueprints/osc_accounting.py:535](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L535) |
| POST | /api/osc/accounting/recurring/<int:row_id>/sync-generated | osc_accounting_recurring_sync_generated_api | [api/blueprints/osc_accounting.py:574](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L574) |
| GET | /api/osc/accounting/summary | osc_accounting_summary_api | [api/blueprints/osc_accounting.py:286](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L286) |
| GET,POST | /api/osc/accounting/transactions | osc_accounting_transactions_api | [api/blueprints/osc_accounting.py:93](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L93) |
| GET,PUT,DELETE | /api/osc/accounting/transactions/<int:row_id> | osc_accounting_transaction_detail_api | [api/blueprints/osc_accounting.py:240](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L240) |
| GET | /api/osc/accounting/transactions/xlsx | osc_accounting_transactions_xlsx_api | [api/blueprints/osc_accounting.py:136](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_accounting.py#L136) |
| GET,POST | /api/osc/activity-logs | osc_activity_logs_api | [api/blueprints/osc_cases.py:4751](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4751) |
| GET,DELETE | /api/osc/activity-logs/<int:row_id> | osc_activity_log_detail_api | [api/blueprints/osc_cases.py:4793](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4793) |
| GET | /api/osc/archive-jobs/<job_id> | osc_archive_job_status_api | [api/blueprints/osc_cases.py:3450](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3450) |
| POST | /api/osc/archive-wizard/execute | osc_archive_wizard_execute_api | [api/blueprints/osc_cases.py:9743](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9743) |
| GET | /api/osc/archive-wizard/preview | osc_archive_wizard_preview_api | [api/blueprints/osc_cases.py:9732](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9732) |
| GET | /api/osc/backups | osc_backup_list | [api/blueprints/osc_cases.py:10645](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10645) |
| POST | /api/osc/backups | osc_backup_create | [api/blueprints/osc_cases.py:10655](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10655) |
| DELETE | /api/osc/backups/<filename> | osc_backup_delete | [api/blueprints/osc_cases.py:10773](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10773) |
| POST | /api/osc/backups/<filename>/restore | osc_backup_restore | [api/blueprints/osc_cases.py:10669](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10669) |
| GET,POST | /api/osc/calendar/events | osc_calendar_events_api | [api/blueprints/osc_cases.py:7720](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7720) |
| GET,PUT,DELETE | /api/osc/calendar/events/<int:row_id> | osc_calendar_event_detail_api | [api/blueprints/osc_cases.py:7845](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7845) |
| GET | /api/osc/case-intelligence | osc_case_intelligence_api | [api/blueprints/osc_cases.py:4354](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4354) |
| GET,POST | /api/osc/case-reason-templates | osc_case_reason_templates_api | [api/blueprints/osc_cases.py:4665](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4665) |
| GET,PUT,DELETE | /api/osc/case-reason-templates/<int:row_id> | osc_case_reason_template_detail_api | [api/blueprints/osc_cases.py:4716](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4716) |
| GET,POST | /api/osc/cases | osc_cases_api | [api/blueprints/osc_cases.py:1549](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L1549) |
| GET,PUT,DELETE | /api/osc/cases/<row_id> | osc_case_detail_api | [api/blueprints/osc_cases.py:1851](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L1851) |
| GET | /api/osc/cases/<row_id>/address-label | osc_case_address_label | [api/blueprints/osc_cases.py:11238](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L11238) |
| POST | /api/osc/cases/<row_id>/close | osc_case_close_api | [api/blueprints/osc_cases.py:1964](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L1964) |
| POST | /api/osc/cases/<row_id>/create-folder | osc_case_create_folder_api | [api/blueprints/osc_cases.py:3600](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3600) |
| GET | /api/osc/cases/<row_id>/file-search | osc_case_file_search_api | [api/blueprints/osc_cases.py:3837](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3837) |
| GET | /api/osc/cases/<row_id>/folder-browser | osc_case_folder_browser_api | [api/blueprints/osc_cases.py:3772](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3772) |
| GET | /api/osc/cases/<row_id>/folder-path | osc_case_folder_path_api | [api/blueprints/osc_cases.py:3545](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3545) |
| GET | /api/osc/cases/<row_id>/intelligence-snapshot | osc_case_intelligence_for_case_api | [api/blueprints/osc_cases.py:4370](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4370) |
| POST | /api/osc/cases/<row_id>/laf-number/sync | osc_case_laf_number_sync | [api/blueprints/osc_cases.py:10268](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10268) |
| POST | /api/osc/cases/<row_id>/laf-status | osc_laf_case_status_api | [api/blueprints/osc_cases.py:7143](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7143) |
| POST | /api/osc/cases/<row_id>/open-folder | osc_case_open_folder_api | [api/blueprints/osc_cases.py:3478](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3478) |
| POST | /api/osc/cases/<row_id>/quick-action | osc_case_quick_action_api | [api/blueprints/osc_cases.py:3948](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3948) |
| POST | /api/osc/cases/<row_id>/rename-folder | osc_case_rename_folder_api | [api/blueprints/osc_cases.py:3693](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L3693) |
| GET | /api/osc/cases/<row_id>/workbench | osc_case_workbench_api | [api/blueprints/osc_cases.py:4173](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4173) |
| GET | /api/osc/cases/export-csv | osc_cases_export_csv_api | [api/blueprints/osc_cases.py:9463](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9463) |
| GET | /api/osc/cases/export-xlsx | osc_cases_export_xlsx_api | [api/blueprints/osc_cases.py:9484](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9484) |
| POST | /api/osc/cases/import-csv | osc_cases_import_csv_api | [api/blueprints/osc_cases.py:9364](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9364) |
| POST | /api/osc/chat | osc_chat_api | [api/blueprints/web_runtime.py:1004](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L1004) |
| POST | /api/osc/chat/upload | osc_chat_upload_api | [api/blueprints/web_runtime.py:1103](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L1103) |
| GET | /api/osc/checklists/case | osc_case_checklist_get | [api/blueprints/osc_cases.py:10405](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10405) |
| POST | /api/osc/checklists/case | osc_case_checklist_post | [api/blueprints/osc_cases.py:10429](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10429) |
| PUT | /api/osc/checklists/case/<int:row_id> | osc_case_checklist_put | [api/blueprints/osc_cases.py:10453](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10453) |
| DELETE | /api/osc/checklists/case/<int:row_id> | osc_case_checklist_delete | [api/blueprints/osc_cases.py:10472](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10472) |
| GET | /api/osc/checklists/debt-required | osc_laf_debt_required_get | [api/blueprints/osc_cases.py:10197](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10197) |
| POST | /api/osc/checklists/debt-required/save | osc_laf_debt_required_save | [api/blueprints/osc_cases.py:10219](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10219) |
| GET | /api/osc/checklists/legal-aid | osc_laf_checklist_get | [api/blueprints/osc_cases.py:10295](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10295) |
| POST | /api/osc/checklists/legal-aid | osc_laf_checklist_post | [api/blueprints/osc_cases.py:10322](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10322) |
| PUT | /api/osc/checklists/legal-aid/<int:row_id> | osc_laf_checklist_put | [api/blueprints/osc_cases.py:10349](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10349) |
| DELETE | /api/osc/checklists/legal-aid/<int:row_id> | osc_laf_checklist_delete | [api/blueprints/osc_cases.py:10369](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10369) |
| POST | /api/osc/checklists/legal-aid/seed | osc_laf_checklist_seed | [api/blueprints/osc_cases.py:10376](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L10376) |
| GET,POST | /api/osc/clients | osc_clients_api | [api/blueprints/osc_cases.py:7906](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7906) |
| GET,PUT,DELETE | /api/osc/clients/<row_id> | osc_client_detail_api | [api/blueprints/osc_cases.py:7963](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7963) |
| GET | /api/osc/clients/<row_id>/workbench | osc_client_workbench_api | [api/blueprints/osc_cases.py:4044](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4044) |
| GET | /api/osc/clients/export-csv | osc_clients_export_csv_api | [api/blueprints/osc_cases.py:9616](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9616) |
| POST | /api/osc/clients/import-csv | osc_clients_import_csv_api | [api/blueprints/osc_cases.py:9529](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9529) |
| GET,POST | /api/osc/courts | osc_courts_api | [api/blueprints/osc_settings.py:92](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L92) |
| GET,PUT,DELETE | /api/osc/courts/<int:row_id> | osc_court_detail_api | [api/blueprints/osc_settings.py:131](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L131) |
| GET | /api/osc/dashboard | osc_dashboard_api | [api/blueprints/osc_cases.py:4390](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4390) |
| GET,POST | /api/osc/debt/address-data | debt_address_data | [api/blueprints/osc_debt.py:602](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L602) |
| POST | /api/osc/debt/auto-import | debt_auto_import | [api/blueprints/osc_debt.py:797](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L797) |
| POST | /api/osc/debt/batch-generate | debt_batch_generate | [api/blueprints/osc_debt.py:724](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L724) |
| GET | /api/osc/debt/cases | debt_cases_list | [api/blueprints/osc_debt.py:571](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L571) |
| GET | /api/osc/debt/courts | debt_courts_list | [api/blueprints/osc_debt.py:458](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L458) |
| GET | /api/osc/debt/expense-reference | debt_expense_reference | [api/blueprints/osc_debt.py:464](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L464) |
| GET | /api/osc/debt/forms | debt_forms_list | [api/blueprints/osc_debt.py:443](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L443) |
| POST | /api/osc/debt/generate | debt_generate_document | [api/blueprints/osc_debt.py:633](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L633) |
| GET | /api/osc/debt/import-candidates | debt_import_candidates | [api/blueprints/osc_debt.py:486](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L486) |
| POST | /api/osc/debt/merge-pdf | debt_merge_pdf | [api/blueprints/osc_debt.py:936](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L936) |
| GET | /api/osc/debt/scan-evidence/<case_id> | debt_scan_evidence | [api/blueprints/osc_debt.py:497](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L497) |
| GET | /api/osc/debt/schema/<form_type> | debt_form_schema | [api/blueprints/osc_debt.py:449](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L449) |
| GET | /api/osc/debt/source-status | debt_source_status | [api/blueprints/osc_debt.py:477](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L477) |
| POST | /api/osc/debt/supplement-checklist | debt_supplement_checklist | [api/blueprints/osc_debt.py:703](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L703) |
| POST | /api/osc/debt/validate | debt_validate | [api/blueprints/osc_debt.py:892](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_debt.py#L892) |
| POST | /api/osc/discord/test | osc_discord_test_api | [api/blueprints/osc_settings.py:228](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L228) |
| GET,POST | /api/osc/document-keywords | osc_document_keywords_api | [api/blueprints/osc_cases.py:6818](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6818) |
| GET,PUT,DELETE | /api/osc/document-keywords/<int:row_id> | osc_document_keyword_detail_api | [api/blueprints/osc_cases.py:6879](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6879) |
| GET,POST | /api/osc/document-replacements | osc_document_replacements_api | [api/blueprints/osc_cases.py:6917](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6917) |
| GET,DELETE | /api/osc/document-replacements/<int:row_id> | osc_document_replacement_detail_api | [api/blueprints/osc_cases.py:6960](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6960) |
| GET,POST | /api/osc/document-templates | osc_document_templates_api | [api/blueprints/osc_cases.py:6729](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6729) |
| GET,PUT,DELETE | /api/osc/document-templates/<int:row_id> | osc_document_template_detail_api | [api/blueprints/osc_cases.py:6783](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6783) |
| GET | /api/osc/documents | osc_documents_api | [api/blueprints/osc_cases.py:5705](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5705) |
| POST | /api/osc/documents/finalize | osc_documents_finalize_api | [api/blueprints/osc_cases.py:9116](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9116) |
| POST | /api/osc/documents/open | osc_documents_open_api | [api/blueprints/osc_cases.py:5930](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5930) |
| POST | /api/osc/documents/stamp | osc_documents_stamp_api | [api/blueprints/osc_cases.py:8886](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8886) |
| POST | /api/osc/documents/stamp-preview | osc_documents_stamp_preview_api | [api/blueprints/osc_cases.py:8831](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8831) |
| POST | /api/osc/drafts/export | osc_drafts_export_api | [api/blueprints/osc_cases.py:5333](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5333) |
| GET | /api/osc/drafts/feedback | osc_drafts_feedback_recent_api | [api/blueprints/osc_cases.py:5300](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5300) |
| POST | /api/osc/drafts/feedback | osc_drafts_feedback_post_api | [api/blueprints/osc_cases.py:5307](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5307) |
| POST | /api/osc/drafts/generate | osc_drafts_generate_api | [api/blueprints/osc_cases.py:5114](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5114) |
| GET | /api/osc/drafts/meta | osc_drafts_meta_api | [api/blueprints/osc_cases.py:5076](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5076) |
| POST | /api/osc/drafts/reuse-document | osc_drafts_reuse_document_api | [api/blueprints/osc_cases.py:5574](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5574) |
| GET | /api/osc/files/content | osc_file_content_api | [api/blueprints/osc_cases.py:6443](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6443) |
| GET | /api/osc/files/info | osc_files_info_api | [api/blueprints/osc_files.py:2124](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L2124) |
| GET | /api/osc/files/preview | osc_files_preview_api | [api/blueprints/osc_files.py:2025](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L2025) |
| POST | /api/osc/files/share | osc_files_share_create_api | [api/blueprints/osc_files.py:2156](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L2156) |
| GET,PUT | /api/osc/files/text | osc_file_text_api | [api/blueprints/osc_cases.py:6583](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6583) |
| POST | /api/osc/files/upload | osc_file_upload_api | [api/blueprints/osc_cases.py:6638](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6638) |
| POST | /api/osc/files/upload-chunked | osc_files_upload_chunked_api | [api/blueprints/osc_files.py:1605](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1605) |
| POST | /api/osc/files/upload-multi | osc_files_upload_multi_api | [api/blueprints/osc_files.py:1488](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1488) |
| GET | /api/osc/folders/browse | osc_folders_browse_api | [api/blueprints/osc_files.py:2278](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L2278) |
| POST | /api/osc/folders/mkdir | osc_folders_mkdir_api | [api/blueprints/osc_files.py:1772](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1772) |
| POST | /api/osc/folders/move | osc_folders_move_api | [api/blueprints/osc_files.py:1851](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1851) |
| POST | /api/osc/folders/rename | osc_folders_rename_api | [api/blueprints/osc_files.py:1810](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1810) |
| GET | /api/osc/folders/roots | osc_folder_roots_api | [api/blueprints/osc_files.py:1341](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1341) |
| GET | /api/osc/folders/tree | osc_folders_tree_api | [api/blueprints/osc_files.py:1929](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L1929) |
| POST | /api/osc/forms/export | osc_forms_export_api | [api/blueprints/osc_cases.py:8615](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8615) |
| POST | /api/osc/forms/preview | osc_forms_preview_api | [api/blueprints/osc_cases.py:8549](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8549) |
| GET | /api/osc/gcal/auth/callback | gcal_auth_callback | [api/blueprints/osc_gcal.py:253](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_gcal.py#L253) |
| POST | /api/osc/gcal/auth/start | gcal_auth_start | [api/blueprints/osc_gcal.py:213](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_gcal.py#L213) |
| POST | /api/osc/gcal/disconnect | gcal_disconnect | [api/blueprints/osc_gcal.py:318](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_gcal.py#L318) |
| GET | /api/osc/gcal/status | gcal_status | [api/blueprints/osc_gcal.py:174](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_gcal.py#L174) |
| POST | /api/osc/gcal/sync | gcal_sync | [api/blueprints/osc_gcal.py:335](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_gcal.py#L335) |
| GET | /api/osc/hearing-conflicts | osc_hearing_conflicts_api | [api/blueprints/osc_cases.py:7519](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7519) |
| POST | /api/osc/hearing-conflicts/check | osc_hearing_conflicts_check_api | [api/blueprints/osc_cases.py:7572](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7572) |
| GET | /api/osc/hearing-conflicts/download | osc_hearing_conflicts_download_api | [api/blueprints/osc_cases.py:7696](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7696) |
| POST | /api/osc/hearing-conflicts/generate | osc_hearing_conflicts_generate_api | [api/blueprints/osc_cases.py:7603](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7603) |
| GET,POST | /api/osc/insights | osc_insights_api | [api/blueprints/osc_cases.py:8338](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8338) |
| GET | /api/osc/insights/<insight_id> | osc_insight_detail_api | [api/blueprints/osc_cases.py:8411](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8411) |
| POST | /api/osc/insights/fetch-full | osc_insights_fetch_full_api | [api/blueprints/osc_cases.py:8443](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8443) |
| GET | /api/osc/judgments | osc_judgments_compat_api | [api/blueprints/osc_cases.py:8531](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8531) |
| GET | /api/osc/judgments_legacy | osc_judgments_api | [api/blueprints/web_runtime.py:1294](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L1294) |
| POST | /api/osc/labor-law/calc | osc_labor_law_calc | [api/blueprints/osc_cases.py:9862](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9862) |
| POST | /api/osc/labor-law/parse-files | osc_labor_law_parse_files | [api/blueprints/osc_cases.py:9957](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9957) |
| GET | /api/osc/laf | osc_laf_api | [api/blueprints/osc_cases.py:6977](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L6977) |
| POST | /api/osc/laf-backfill | osc_laf_backfill_api | [api/blueprints/osc_cases.py:9714](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9714) |
| POST | /api/osc/laf-wizard/run | osc_laf_wizard_run_api | [api/blueprints/osc_cases.py:9657](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L9657) |
| POST | /api/osc/laf/batch-status | osc_laf_batch_status_api | [api/blueprints/osc_cases.py:7112](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7112) |
| GET | /api/osc/laf/cases | osc_laf_cases_api | [api/blueprints/osc_cases.py:7043](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7043) |
| GET,POST | /api/osc/legal-aid-branches | osc_legal_aid_branches_api | [api/blueprints/osc_settings.py:161](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L161) |
| GET,PUT,DELETE | /api/osc/legal-aid-branches/<int:row_id> | osc_legal_aid_branch_detail_api | [api/blueprints/osc_settings.py:196](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L196) |
| POST | /api/osc/magi-modules/run | osc_magi_modules_run_api | [api/blueprints/web_runtime.py:1033](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L1033) |
| GET,POST | /api/osc/meetings | osc_meetings_api | [api/blueprints/osc_cases.py:7996](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7996) |
| GET,PUT,DELETE | /api/osc/meetings/<int:row_id> | osc_meeting_detail_api | [api/blueprints/osc_cases.py:8067](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8067) |
| GET,POST | /api/osc/memory-keywords | osc_memory_keywords_api | [api/blueprints/osc_cases.py:4880](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4880) |
| GET,PUT,DELETE | /api/osc/memory-keywords/<path:case_number>/<path:hotkey> | osc_memory_keyword_detail_api | [api/blueprints/osc_cases.py:4918](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4918) |
| GET | /api/osc/meta | osc_meta_api | [api/blueprints/osc_cases.py:1265](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L1265) |
| GET,POST | /api/osc/opponents | osc_opponents_api | [api/blueprints/osc_cases.py:4958](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4958) |
| GET,PUT,DELETE | /api/osc/opponents/<int:row_id> | osc_opponent_detail_api | [api/blueprints/osc_cases.py:4998](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4998) |
| GET | /api/osc/pdf-generation-log | osc_pdf_generation_log_api | [api/blueprints/osc_cases.py:5033](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5033) |
| GET,DELETE | /api/osc/pdf-generation-log/<int:row_id> | osc_pdf_generation_log_detail_api | [api/blueprints/osc_cases.py:5058](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5058) |
| POST | /api/osc/pdf/action | osc_pdf_action_api | [api/blueprints/osc_pdf.py:2357](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_pdf.py#L2357) |
| POST | /api/osc/pdf/calendar-scan | osc_pdf_calendar_scan_api | [api/blueprints/osc_pdf.py:2502](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_pdf.py#L2502) |
| GET | /api/osc/pdf/info | osc_pdf_info_api | [api/blueprints/osc_pdf.py:2328](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_pdf.py#L2328) |
| POST | /api/osc/pdf/upload | osc_pdf_upload_api | [api/blueprints/osc_pdf.py:2338](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_pdf.py#L2338) |
| GET | /api/osc/poll | osc_poll_api | [api/blueprints/web_runtime.py:1276](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L1276) |
| GET,POST | /api/osc/quotation-templates | osc_quotation_templates_api | [api/blueprints/osc_cases.py:7386](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7386) |
| GET,PUT,DELETE | /api/osc/quotation-templates/<int:row_id> | osc_quotation_template_detail_api | [api/blueprints/osc_cases.py:7421](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7421) |
| GET,POST | /api/osc/quotations | osc_quotations_api | [api/blueprints/osc_cases.py:7243](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7243) |
| GET,PUT,DELETE | /api/osc/quotations/<row_id> | osc_quotation_detail_api | [api/blueprints/osc_cases.py:7343](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L7343) |
| GET | /api/osc/quotations/<row_id>/export-pdf | osc_quotation_export_pdf | [api/blueprints/osc_cases.py:11124](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L11124) |
| POST | /api/osc/raziel/delivery | raziel_delivery_api | [api/blueprints/raziel.py:702](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py#L702) |
| GET | /api/osc/raziel/delivery/<path:name> | raziel_delivery_file_api | [api/blueprints/raziel.py:712](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py#L712) |
| GET | /api/osc/raziel/file/<kind> | raziel_file_api | [api/blueprints/raziel.py:724](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py#L724) |
| POST | /api/osc/raziel/run | raziel_run_api | [api/blueprints/raziel.py:675](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py#L675) |
| GET | /api/osc/raziel/status | raziel_status_api | [api/blueprints/raziel.py:641](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py#L641) |
| POST | /api/osc/raziel/tlr-preview | raziel_tlr_preview_api | [api/blueprints/raziel.py:689](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/raziel.py#L689) |
| GET | /api/osc/saas/ai-governance | osc_saas_ai_governance_api | [api/blueprints/osc_cases.py:4635](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4635) |
| POST | /api/osc/saas/client-packet | osc_saas_client_packet_api | [api/blueprints/osc_cases.py:4579](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4579) |
| POST | /api/osc/saas/conflict-check | osc_saas_conflict_check_api | [api/blueprints/osc_cases.py:4544](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4544) |
| GET | /api/osc/saas/diagnostic-pack | osc_saas_diagnostic_pack_api | [api/blueprints/osc_cases.py:4647](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4647) |
| POST | /api/osc/saas/intake | osc_saas_intake_api | [api/blueprints/osc_cases.py:4551](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4551) |
| GET,POST | /api/osc/saas/notification-prefs | osc_saas_notification_prefs_api | [api/blueprints/osc_cases.py:4615](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4615) |
| GET,POST | /api/osc/saas/onboarding | osc_saas_onboarding_api | [api/blueprints/osc_cases.py:4600](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4600) |
| GET | /api/osc/saas/operations-report | osc_saas_operations_report_api | [api/blueprints/osc_cases.py:4641](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4641) |
| GET | /api/osc/saas/overview | osc_saas_overview_api | [api/blueprints/osc_cases.py:4537](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4537) |
| POST | /api/osc/saas/quality-check | osc_saas_quality_check_api | [api/blueprints/osc_cases.py:4572](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4572) |
| GET | /api/osc/saas/task-boards | osc_saas_task_boards_api | [api/blueprints/osc_cases.py:4593](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4593) |
| GET | /api/osc/saas/timeline | osc_saas_timeline_api | [api/blueprints/osc_cases.py:4586](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4586) |
| GET | /api/osc/saas/workflow-templates | osc_saas_workflow_templates_api | [api/blueprints/osc_cases.py:4629](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4629) |
| GET,POST | /api/osc/settings | osc_settings_api | [api/blueprints/osc_settings.py:28](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L28) |
| GET,PUT,DELETE | /api/osc/settings/<path:setting_key> | osc_setting_detail_api | [api/blueprints/osc_settings.py:62](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_settings.py#L62) |
| GET | /api/osc/template-folder | osc_template_folder_api | [api/blueprints/osc_cases.py:5871](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L5871) |
| GET,POST | /api/osc/todos | osc_todos_api | [api/blueprints/osc_cases.py:8101](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8101) |
| GET,PUT,DELETE | /api/osc/todos/<int:row_id> | osc_todo_detail_api | [api/blueprints/osc_cases.py:8264](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8264) |
| POST | /api/osc/todos/bulk-complete-before | osc_todos_bulk_complete_before_api | [api/blueprints/osc_cases.py:8229](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L8229) |
| GET,POST | /api/osc/user-settings | osc_user_settings_api | [api/blueprints/osc_cases.py:4810](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4810) |
| GET,PUT,DELETE | /api/osc/user-settings/<int:row_id> | osc_user_setting_detail_api | [api/blueprints/osc_cases.py:4848](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_cases.py#L4848) |
| POST | /api/self-repair | api_self_repair | [api/blueprints/admin_runtime.py:1927](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1927) |
| POST | /api/skills/<skill_name>/rollback | api_skill_rollback | [api/blueprints/admin_runtime.py:2072](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2072) |
| GET | /api/skills/<skill_name>/versions | api_skill_versions | [api/blueprints/admin_runtime.py:2052](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2052) |
| GET | /api/skills/interview-history | api_skill_interview_history | [api/blueprints/admin_runtime.py:2037](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2037) |
| POST | /api/skills/run | api_skills_run_compat | [api/blueprints/dashboard_pages.py:576](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L576) |
| GET | /api/status | api_status | [api/blueprints/admin_runtime.py:2999](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L2999) |
| POST | /api/system-test | api_system_test | [api/blueprints/admin_runtime.py:1911](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1911) |
| POST | /api/transcribe | transcribe_audio | [api/blueprints/admin_runtime.py:3717](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3717) |
| GET | /app | mobile_home | [api/blueprints/dashboard_pages.py:1206](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1206) |
| GET | /app-admin | mobile_admin | [api/blueprints/dashboard_pages.py:1216](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1216) |
| GET,POST | /callback | callback | [api/server.py:919](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L919) |
| GET | /capabilities | agent_capabilities | [api/agentic/http_gateway.py:245](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L245) |
| POST | /case-status | agent_case_status | [api/agentic/http_gateway.py:338](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L338) |
| POST | /chat | chat | [skills/bridge/casper_bridge.py:54](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/skills/bridge/casper_bridge.py#L54) |
| GET | /clients | api_query_clients | [api/tools_api.py:3580](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3580) |
| POST | /clients | api_add_client | [api/tools_api.py:3590](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3590) |
| POST | /code/autofix | api_code_autofix | [api/tools_api.py:3306](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3306) |
| POST | /code/skill-cycle | api_code_skill_cycle | [api/tools_api.py:3333](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3333) |
| POST | /collab/chat | api_collab_chat | [api/tools_api.py:3373](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3373) |
| POST | /collab/music | api_collab_music | [api/tools_api.py:3362](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3362) |
| POST | /collab/transcribe | api_collab_transcribe | [api/tools_api.py:3463](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3463) |
| POST | /collab/translate | api_collab_translate | [api/tools_api.py:3343](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3343) |
| GET | /connections | connections_status | [api/tools_api.py:1722](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1722) |
| POST | /council/core/approve | api_council_core_approve | [api/tools_api.py:3510](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3510) |
| GET | /council/core/pending | api_council_core_pending | [api/tools_api.py:3499](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3499) |
| POST | /council/core/reject | api_council_core_reject | [api/tools_api.py:3526](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3526) |
| GET | /dashboard | dashboard | [api/blueprints/dashboard_pages.py:1139](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1139) |
| GET | /dashboard/beginner | dashboard_beginner | [api/blueprints/dashboard_pages.py:1152](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1152) |
| GET | /dashboard/golem | golem_console | [api/blueprints/dashboard_pages.py:1175](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1175) |
| GET | /dashboard/legacy | dashboard_legacy | [api/blueprints/dashboard_pages.py:1145](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1145) |
| GET | /dashboard/nerv | magi_adjust | [api/blueprints/dashboard_pages.py:1168](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1168) |
| GET | /dashboard/nerv/api/health | nerv_api_health | [api/blueprints/admin_runtime.py:1764](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1764) |
| GET | /dashboard/status | status_center | [api/blueprints/dashboard_pages.py:1159](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1159) |
| GET | /dashboard/website | dashboard_website | [api/blueprints/dashboard_pages.py:1276](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1276) |
| GET | /definitions | api_definitions | [api/tools_api.py:3676](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3676) |
| GET | /exports/<path:filename> | serve_exports | [api/server.py:627](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L627) |
| GET | /favicon.ico | favicon | [api/server.py:830](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L830) |
| POST | /fetch | agent_fetch | [api/agentic/http_gateway.py:422](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L422) |
| POST | /fetch | api_fetch | [api/tools_api.py:1907](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1907) |
| GET | /golem | golem_console | [api/blueprints/dashboard_pages.py:1175](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1175) |
| GET | /health | agent_health | [api/agentic/http_gateway.py:213](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L213) |
| GET | /health | health | [api/blueprints/admin_runtime.py:3287](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3287) |
| GET | /health | health | [api/tools_api.py:1318](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1318) |
| GET | /intel | intel_panel | [api/blueprints/dashboard_pages.py:551](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L551) |
| POST | /iron-dome/auto-harden | api_iron_dome_auto_harden | [api/tools_api.py:3282](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3282) |
| GET | /iron-dome/patterns | api_iron_dome_patterns_list | [api/tools_api.py:3250](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3250) |
| POST | /iron-dome/patterns | api_iron_dome_patterns_add | [api/tools_api.py:3266](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3266) |
| GET | /jobs/<job_id> | api_get_job | [api/tools_api.py:2968](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2968) |
| POST | /laf/smoke_login | api_laf_smoke_login | [api/tools_api.py:3689](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3689) |
| GET | /legal | api_legal_skills_list | [api/tools_api.py:3653](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3653) |
| POST | /legal/<skill_name> | api_legal_skill | [api/tools_api.py:3629](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3629) |
| GET,POST | /line/webhook | callback | [api/server.py:919](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L919) |
| GET | /livez | livez | [api/blueprints/admin_runtime.py:3128](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3128) |
| GET | /livez | livez | [api/tools_api.py:1308](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1308) |
| GET,POST | /login | login | [api/server.py:835](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L835) |
| GET | /logout | logout | [api/server.py:907](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L907) |
| GET | /magi-adjust | magi_adjust | [api/blueprints/dashboard_pages.py:1168](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1168) |
| GET | /magi-research | research_panel | [api/blueprints/dashboard_pages.py:1093](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1093) |
| GET | /magi-settings | magi_adjust | [api/blueprints/dashboard_pages.py:1168](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1168) |
| GET | /manual | maintenance_manual | [api/blueprints/dashboard_pages.py:1181](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1181) |
| GET | /manual/markdown | maintenance_manual_markdown | [api/blueprints/dashboard_pages.py:1193](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1193) |
| GET | /manual/pdf | maintenance_manual_pdf | [api/blueprints/dashboard_pages.py:1187](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1187) |
| GET | /manual/source-index.json | maintenance_manual_source_index | [api/blueprints/dashboard_pages.py:1199](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1199) |
| GET | /meetings | api_list_meetings | [api/tools_api.py:3605](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3605) |
| POST | /meetings | api_book_meeting | [api/tools_api.py:3613](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3613) |
| GET | /melchior/health | api_melchior_health | [api/tools_api.py:2096](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2096) |
| POST | /melchior/skills/sync | api_melchior_sync_skills | [api/tools_api.py:2103](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2103) |
| GET | /mobile | mobile_home | [api/blueprints/dashboard_pages.py:1206](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1206) |
| GET | /mobile-admin | mobile_admin | [api/blueprints/dashboard_pages.py:1216](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1216) |
| GET | /mobile-app | mobile_app_entry | [api/server.py:870](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L870) |
| GET | /mobile/config.json | mobile_config_json | [api/blueprints/dashboard_pages.py:1225](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1225) |
| GET | /mobile/manifest.webmanifest | mobile_manifest | [api/blueprints/dashboard_pages.py:1244](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1244) |
| GET | /mobile/sw.js | mobile_service_worker | [api/blueprints/dashboard_pages.py:1230](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1230) |
| GET | /nerv | magi_adjust | [api/blueprints/dashboard_pages.py:1168](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1168) |
| GET | /ops/process-monitor | process_monitor_page | [api/blueprints/web_runtime.py:836](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/web_runtime.py#L836) |
| GET | /osc | osc_interface | [api/server.py:615](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L615) |
| GET | /osc/debt | osc_debt_interface | [api/server.py:621](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L621) |
| POST | /osc/external/case_status | external_osc_case_status | [api/tools_api.py:1665](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1665) |
| POST | /osc/external/chat | external_osc_chat | [api/tools_api.py:1344](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1344) |
| GET | /osc/external/health | external_osc_health | [api/tools_api.py:1326](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1326) |
| GET | /osc/external/ui | external_osc_ui | [api/tools_api.py:1608](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1608) |
| POST | /plans | agent_prepare_action | [api/agentic/http_gateway.py:459](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L459) |
| GET | /plans | agent_list_plans | [api/agentic/http_gateway.py:501](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L501) |
| GET | /plans/<plan_id> | agent_get_plan | [api/agentic/http_gateway.py:515](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L515) |
| POST | /plans/<plan_id>/cancel | agent_cancel_plan | [api/agentic/http_gateway.py:532](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L532) |
| POST | /plans/<plan_id>/confirm | agent_confirm_plan | [api/agentic/http_gateway.py:549](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L549) |
| POST | /read | agent_read | [api/agentic/http_gateway.py:267](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L267) |
| GET | /readyz | readyz | [api/blueprints/admin_runtime.py:3139](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3139) |
| POST | /recall | api_recall | [api/tools_api.py:3561](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3561) |
| GET,POST | /register | register | [api/server.py:875](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L875) |
| POST | /remember | api_remember | [api/tools_api.py:3543](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3543) |
| POST | /research | agent_research | [api/agentic/http_gateway.py:405](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L405) |
| GET | /research | research_panel | [api/blueprints/dashboard_pages.py:1093](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1093) |
| POST | /research | api_research | [api/tools_api.py:1866](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1866) |
| GET | /research/judgment-classifier | research_judgment_classifier | [api/blueprints/dashboard_pages.py:1099](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1099) |
| GET | /research/rss-preview | research_rss_preview | [api/blueprints/dashboard_pages.py:1105](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1105) |
| GET | /s/<token> | osc_files_public_share_api | [api/blueprints/osc_files.py:2229](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/osc_files.py#L2229) |
| GET | /saas-readyz | saas_readyz | [api/blueprints/admin_runtime.py:3282](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L3282) |
| GET | /sages | sages_status | [api/tools_api.py:1803](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1803) |
| POST | /search | agent_search | [api/agentic/http_gateway.py:388](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L388) |
| POST | /search | api_search | [api/tools_api.py:1834](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1834) |
| POST | /shortcut/ocr | api_shortcut_ocr | [api/tools_api.py:2518](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2518) |
| POST | /shortcut/pdf_text | api_shortcut_pdf_text | [api/tools_api.py:2608](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2608) |
| POST | /shortcut/summarize | api_shortcut_summarize | [api/tools_api.py:2637](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2637) |
| POST | /shortcut/transcribe | api_shortcut_transcribe | [api/tools_api.py:2675](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2675) |
| GET | /skills | api_list_skills | [api/tools_api.py:2714](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2714) |
| POST | /skills | api_create_skill | [api/tools_api.py:2720](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2720) |
| POST | /skills/acquire | api_acquire_skill | [api/tools_api.py:2759](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2759) |
| POST | /skills/canary/start | api_skill_canary_start | [api/tools_api.py:3072](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3072) |
| POST | /skills/canary/stop | api_skill_canary_stop | [api/tools_api.py:3105](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3105) |
| POST | /skills/ci | api_skill_ci | [api/tools_api.py:3119](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3119) |
| POST | /skills/discover | api_discover_skills | [api/tools_api.py:2735](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2735) |
| GET | /skills/events | api_skill_events | [api/tools_api.py:3134](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3134) |
| POST | /skills/import/toolsai-auto-skill | api_import_toolsai_auto_skill | [api/tools_api.py:3231](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3231) |
| POST | /skills/install | api_install_skill | [api/tools_api.py:2747](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2747) |
| POST | /skills/internalize | api_skill_internalize | [api/tools_api.py:3181](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3181) |
| POST | /skills/internalize/codebase | api_skill_internalize_codebase | [api/tools_api.py:3203](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3203) |
| GET | /skills/knowledge/stats | api_skill_knowledge_stats | [api/tools_api.py:3297](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3297) |
| GET | /skills/release | api_skill_release_state | [api/tools_api.py:3045](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3045) |
| POST | /skills/rollback | api_skill_rollback | [api/tools_api.py:3031](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3031) |
| POST | /skills/run | api_run_skill | [api/tools_api.py:2820](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2820) |
| POST | /skills/stable | api_skill_set_stable | [api/tools_api.py:3057](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3057) |
| POST | /skills/teach | api_skill_teach | [api/tools_api.py:3144](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3144) |
| POST | /skills/teach/file | api_skill_teach_file | [api/tools_api.py:3164](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3164) |
| POST | /skills/versions | api_skill_versions | [api/tools_api.py:3018](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L3018) |
| GET | /start | dashboard_beginner | [api/blueprints/dashboard_pages.py:1152](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1152) |
| GET | /static/exports/<path:filename> | static_exports | [api/tools_api.py:408](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L408) |
| GET | /static/worldmonitor_reports | worldmonitor_reports_redirect | [api/blueprints/dashboard_pages.py:539](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L539) |
| GET | /static/worldmonitor_reports/ | worldmonitor_reports_redirect | [api/blueprints/dashboard_pages.py:539](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L539) |
| GET | /status | status_center | [api/blueprints/dashboard_pages.py:1159](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L1159) |
| GET | /status/api/health | nerv_api_health | [api/blueprints/admin_runtime.py:1764](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/admin_runtime.py#L1764) |
| POST | /summarize | agent_summarize | [api/agentic/http_gateway.py:443](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/agentic/http_gateway.py#L443) |
| POST | /summarize | api_summarize | [api/tools_api.py:2114](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2114) |
| GET | /summarize/health | api_summarize_health | [api/tools_api.py:2450](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L2450) |
| GET,POST | /telegram/webhook | telegram_webhook | [api/webhooks/telegram.py:982](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/webhooks/telegram.py#L982) |
| GET,POST,PUT,PATCH,DELETE,OPTIONS | /toolsapi/<path:subpath> | toolsapi_compat_proxy | [api/server.py:642](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/server.py#L642) |
| POST | /vision | api_vision | [api/tools_api.py:1942](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/tools_api.py#L1942) |
| GET | /worldmonitor | worldmonitor_entry | [api/blueprints/dashboard_pages.py:545](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L545) |
| GET | /worldmonitor/ | worldmonitor_entry | [api/blueprints/dashboard_pages.py:545](https://github.com/WhaleChao/MAGI-public/blob/29222c40cd5f898f27670c13feb4c134c751bdb3/api/blueprints/dashboard_pages.py#L545) |

<a id="appF"></a>
# 附錄 F. 維修命令速查與名詞表

### 只讀維修命令

```bash
# Git / source
git status --short
git log -1 --format='%H %s'
shasum -a 256 PATH

# Process / ports (read-only)
ps -p PID -o pid=,ppid=,pgid=,etime=,command=
lsof -nP -iTCP:5002 -sTCP:LISTEN
launchctl print gui/$UID/com.magi.v3.gateway

# Local health
curl -fsS http://127.0.0.1:5002/health
python3 scripts/magi_doctor.py --json
python3 scripts/ops/business_module_live_check.py --json
python3 scripts/ops/function_health_index.py --json

# Source/privacy tests
python3 scripts/public_release_audit.py --strict --json
python3 -m pytest -q TEST_PATH
```

### 禁止直接執行的捷徑
- `rm lock/state/checkpoint`：可能造成雙 owner 或遺失 durable work。
- 直接編輯 cron JSON：破壞 occurrence/command/retry 證據。
- 對 canonical owner `kill -9`：跳過 terminal/reap/rollback。
- 在 installed release 直接改 `.py`：破壞 immutable manifest。
- 複製舊 success receipt：造成假綠。

### 名詞表

| 名詞 | 定義 |
| --- | --- |
| active marker | 唯一生效 release/transaction 的原子紀錄 |
| canonical | 由正式 runtime root、manifest、schema 與 realpath 共同認定 |
| checkpoint | 可續跑的最小進度與內容 hash cache |
| cursor | 公平輪轉 all-files 案件的位置 |
| fail closed | 證據不足就拒絕，不猜成功 |
| formal chain | 正式發布各 gate 的 hash-bound 工件集合 |
| owner | 持有某 domain lock 且身分可由 PID/exe/argv/root 證明的程序 |
| receipt | 動作輸入、輸出、read-back 與 identity 的不可逆證據 |
| staging | 尚未 atomic commit 的暫存資料；必須可清理且不得被當完成 |
| terminal | worker 已合法完成 chunk/cycle 或結構化失敗並釋放 owner |


---
**文件完整性：** 本 PDF/Markdown 是維修導航；完整原始碼仍以 Git branch 中的檔案與 `MAGI_V3_原始碼索引_rc643.json` SHA 為準。任何後續版本都應重建索引與本書，不可只改頁面文字。
