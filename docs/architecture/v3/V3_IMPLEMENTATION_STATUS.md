# MAGI V3 實作與驗證狀態

更新：2026-07-14  
目前決策：**NO-GO；V2 是唯一 active production release。**

## 已完成：Milestone 1–2 安全核心

- `magi_v3/**`：side-effect-free composition、SQLite WAL ledger、job/attempt/lease/outbox、明確狀態機、global resource governor、安全 process-group supervisor、輕量 health/readiness、host-global single-active lock。
- `magi_v3/dispatcher.py`：durable dispatcher 與 lease/result/PID fencing；`exit 0` 不代表成功，必須由 verifier 提供業務完成與 artifact/side-effect receipt。
- ledger schema v4 已持久化 Job Envelope required fields：P0–P4、latest-start/deadline/not-before、timeout/queue TTL、preemptible、resource claim、commit phase、ambiguity、artifacts、receipts 與 metrics；讀取與 lease 時重驗不變式。
- 危險或外部寫入工作必須有 idempotency key；destructive/external-commit 工作另需限時、一次性的 confirmation challenge。
- `magi_v3/macos_resources.py`：唯讀 macOS memory-pressure、vm-stat、swap、thermal 與 process inventory sampler；可對明確且同次 inventory 存在的 PID，用 `/usr/bin/footprint` 取得 physical footprint。RSS 明確不是 footprint；per-process Metal 仍無可信外部位元組來源，因此 `governor_ready=false`。
- `scripts/v3_validation/**`：342-route 固定 fingerprint、匿名 replay fixture、legacy response adapter 規格、side-effect fail-closed policy、隔離 LIVE plan/report schema，以及無 socket/host/port 的注入式 HTTP contract runner。
- `scripts/v3_cutover/**`：process/pidfile/port/launchd/ownership probe、single-active live-validation/cutover/rollback workflow、fault simulation。Phase 1 mutation 在程式內無條件停用，CLI 不提供 mutation command。
- cutover preflight 會依 `Asia/Taipei` 強制驗證 02:00–04:00 維護窗；窗外即使其他證據全綠也不能得到 GO。
- `scripts/v3_release_gate.py`：28 項 required evidence 統一 fail-closed；證據綁 campaign、release、hardware、gate-config hash，且重算 artifact SHA-256 與時效。
- `scripts/v3_release_bundle.py`：只從乾淨、已追蹤且與 HEAD 完全一致的 allowlist 建立離線 staging bundle；逐檔 SHA-256、來源三次重驗、symlink/special-file 拒絕，最後才原子建立 completion marker；不能寫 live runtime 或安裝 plist。
- `scripts/v3_campaign/**`：可續跑的 phase-one smoke ledger；所有現有 workload 明確為 non-certifying，七日 smoke 不會產生 release-gate evidence，且 repo/release provenance 不乾淨就拒絕執行。
- `config/v3_pre_cutover_readiness.json`：以實際程式與測試為證據盤點 8 個 production surface；目前 0 個 implemented、0 個 tested、8 個 blocked，因此 `replacement_ready=false`。
- `scripts/v3_deploy_prepare.py`：純離線 deployment renderer；只接受 immutable release 內、manifest 有追蹤且 hash 相符的 Python 與三個正式 entrypoint，不能呼叫 `launchctl`、不能啟動程序，也不能寫入 live runtime。
- `scripts/v3_pre_cutover.py`：唯讀 pre-cutover 聚合器；同時驗證備份/還原、immutable release、readiness、deploy marker、campaign、release gate、維護窗與 V2/V3 ownership，任一缺項即 `NO_GO`。
- `config/v3_validation_campaign.json`：至少七日離線 campaign、三次隔離 LIVE 驗證、三次 cold rollback 演練；`armed=false`。
- Python wheel 已確認包含 `magi_v3` package。

## 本輪驗證結果

- V3 核心、相容、cutover、release gate、campaign、deployment preparation、pre-cutover 與架構測試：`334 passed`。
- 6 份 V3 設定 JSON 與 9 份 Draft 2020-12 schema 驗證通過。
- 342 route：5002=275、5003=67、total=342；fingerprint 一致。
- 目前 426 個 route-method 中 25 個完成 side-effect review、401 個維持 blocked；只有 24/342 routes 全部方法完成 review。Review 不代表 compatibility implementation 已完成。
- 三種 clean single-active workflow simulation 通過；注入殘留 V2 scheduler owner 時正確 No-Go。
- 不完整 LIVE report 即使 schema 合法仍回傳 exit 2；未 review 的 route-method 不可執行。
- HTTP contract runner 會在呼叫注入 client 前驗證 fixture allowlist/hash、inventory fingerprint、exact reviewed route-method 與離線隔離聲明；status、declared headers、content-type、JSON/text/SSE body 必須完全符合 legacy fixture。
- 1,000 次 V3 core liveness probe 先前基線沒有 import MLX、Torch、Playwright、PDF/OCR、Flask、NumPy 或 psutil；短程序 maximum RSS 約 23 MB。這不是完整 LIVE 資源證據。
- 實機 PID-bound footprint smoke 成功取得約 18 MB、0 parser errors；Metal 缺值仍使 governor fail-closed，未把 smoke 保存為 promotion evidence。
- Build wheel 成功且包含 ledger、dispatcher、supervisor、resource、health 與 macOS sampler。
- 實際 release bundle preflight 正確拒絕目前未提交的 V3 allowlist，且沒有建立 staging；因此尚不存在可供 LIVE 啟動的 candidate release。
- deployment renderer 已驗證會拒絕目前缺少的 `magi_v3.gateway`、`magi_v3.control`、`magi_v3.supervisor_service`，也會拒絕沿用 V2 或外部 Python runtime。

## 實機 read-only preflight

結果為 No-Go，且 `mutation_performed=false`：

- active release：只有 V2；unknown active owner=0。
- 5002、5003、5014、8088 listener 與同一個 Playwright/Chrome 工作階段已正確歸屬 V2；瀏覽器子程序不再被誤算成多個 owner。
- host-level RPC、OMLX 8080–8083 與 watchdog 以 exact allowlist 身分辨識，不算成另一個 MAGI release。
- 發現 8 個 stale V2 pidfile。
- V3 immutable runtime root、pidfile coverage 與 launchd label 尚未建立。
- production readiness 盤點的 8 個必要 surface 全部 blocked：5002、5003、8088、342-route handlers、login/session、callbacks/webhooks、SSE、multipart upload。
- 找不到符合新格式且可驗證的 backup metadata、實際 restore drill、immutable release marker、deploy prepared marker、certifying campaign report 或 release-gate report。
- 28 項 release evidence 目前全數尚未完成。
- 最新實測時間 2026-07-14 13:59（Asia/Taipei）位於核准維護窗之外，window hard gate 回傳 `outside_cutover_window`。

這些缺口會阻止 LIVE 驗證及替換。不得為了讓報告變綠而直接刪 pidfile；要先由 ownership/reconciliation 工具證明相對應程序確實不存在，再以可稽核步驟清理。

## 進行中與下一個 Milestone

1. 先實作 `magi_v3.gateway`、`magi_v3.control`、`magi_v3.supervisor_service`，再將 capability→worker-class registry 接到 ingress；補 scheduler/fair queue/checkpoint/preemption、dispatcher crash adoption 與 optional lineage/retry。
2. 逐批完成剩餘 401 個 route-method review，並將已具 multipart bytes、重複 header/`Set-Cookie` 支援的離線 HTTP runner 用於完整 342-route parity。
3. 取得可驗證的 per-process Metal allocation；完成 matched V2/V3 performance 與 worker 100-cycle reap benchmark。
4. 補 notification sender、Retry-After、TTL、collapse、DLQ，以及 WAL/disk-full/fsync fault injection。
5. 建立不可變 V3 release、rendered plist 與明確 V2/V3 ownership manifest，但維持未載入、未啟動。
6. 完成至少七日離線 replay/soak 後，才提出三個彼此間隔至少 24 小時、02:00–04:00 的 single-active LIVE 驗證候選窗。

最終替換仍維持：測試與三次 LIVE 驗證全部完成後，離峰完整停止 V2，確認 zero owner，再只啟動 V3。V2 只保留為停止的 cold rollback artifact；任何階段都不允許 V2/V3 同時運作。
