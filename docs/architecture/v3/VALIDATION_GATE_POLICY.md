# MAGI V3 驗證関門政策

這份文件定義驗證的實際任務邊界。原則不是「測試越少越好」，而是同一項風險
只在正確的時點、對正確的 immutable inputs 驗一次。

## 1. 同步核心 gate

只包含會造成立即上線事故的項目：

- exact source commit、release manifest、suite manifest、runtime 與 test-source hashes；
- 完整 formal certification，對同一組 immutable inputs 僅一次；
- privacy、backup、actual restore、independent restore；
- plist、ProcessType、installed root、owner、residue 與 rollback artifacts；
- old supervisor quiesce、durable handoff、single-active activation；
- 本機核心 HTTP、authentication、CSRF、cache policy 與回滾路徑。

法院、法扶、Drive 全輪、MCP、每日 benchmark 與外部網路不在這個同步 gate。

## 2. 變更模組 gate

change scope 必須對應具體業務契約：

| 模組 | 必要證據 |
|---|---|
| Cookie Cutter | 外框／內圖、光滑輪廓、watertight mesh、列印厚度、local-only、零持久化 |
| 判決趨勢 | 法官＋案由實際抽樣、MCP full text、穩定官方連結、民國日期 picker |
| 閱卷 | raw/public signature、expected∩handled、identity mismatch deferred 精確語意 |
| PDF 命名 | 外層當事人與嵌套法條摘要不得混淆、格式與品質分開計數 |
| 維修手冊 | HTML/PDF/index hashes、目錄、desktop/mobile 表格無裁切、日夜主題 |
| Drive | checkpoint、hash cache、bounded staging、cursor、零 pending/unverified/collision/storage/error |

變更模組 gate 不能重跑已在 formal receipt 中出現的相同 pytest node。

## 3. 獨立背景健康

每個領域使用自己的 receipt、freshness 與 SLA：

- business、function index、Doctor、guardian、Funnel；
- Drive outcome；
- 法院／法扶／MCP 真實抽樣；
- PDF naming 等每日品質 benchmark。

對外狀態必須區分：

- `passed`：必要業務效果與收據完整；
- `waiting` / `busy`：另一個合法 owner 使用資源；
- `deferred` / `retryable`：checkpoint 安全、由 bounded retry 繼續；
- `failed`：超過模組 SLA，或已破壞必要不變量。

`waiting` 與 `deferred` 可以顯示黃燈，但不能把其他模組或已完成的 cutover 改成失敗。

## 4. Receipt 沿用契約

只有以下全部一致才可沿用 formal PASS：

1. source commit；
2. release manifest SHA；
3. suite manifest SHA；
4. Python/runtime identity；
5. selected nodeids 與其 test-source SHA；
6. resource/security policy；
7. receipt schema 與 runner SHA。

任一不同即失效。cron occurrence、PID、lock、portal 登入、外部 API 回應、daily sample
不得當成 formal reuse identity。

### 4.1 共享外部資料的雙層收據

共享外部資料的 payload 完整性與 release 身分必須分開驗證：

- shared payload receipt 只證明共享目錄的來源、邏輯輸入與位元組雜湊；
- deployment-local `static-external-release-receipt.json` 綁定候選 release、deployment、shared receipt 雜湊與目標 snapshot；
- 新 release 不得改寫或「重綁定」上一個 active release 已驗證的 shared receipt；
- pre-cutover 必須同時重驗 shared payload bytes 與 deployment-local binding，任一漂移即 fail closed。

這使 r59 能保持不可變且可回滾，同時允許新候選版本使用內容相同的共享輸入，不會把「共享資料沒變」誤判成「舊 release 應被覆寫」。

### 4.2 V3 到 V3 的原子切換與回滾演練

RC643 後續版本只能用專用的 V3 rotation drill 產生 G27 證據：

- 前版與候選版都必須是 immutable installed release，並由各自 hash-bound launcher 啟動；
- 使用隔離 state root 與私有 active marker，連續三次驗證零 owner → 原子切換 → 候選冷重啟 → 前版冷回滾；
- 生產 active marker、launchctl、production ports、真實業務狀態與網路全程唯讀或禁止存取；
- 每次演練驗證單一 owner、durable SQLite sentinel、零遺失、零重複與 rollback RTO；
- `legacy_v2_validation=disabled` 時，release gate 只接受單一、可重算的 V3 rotation raw report；舊 V2→V3／V3→V2 executor 僅保留為歷史工具，不能放行 active V3 promotion。

因此 r59 不會被演練修改；它同時是正式切換失敗時保留的 immutable cold-rollback 目標。

## 5. 量化驗收

- promotion 與 formal full 的重複 pytest node 必須為 0；
- 同一 immutable inputs 重跑必須有相同結果；
- 外部狀態變化只影響對應模組 receipt；
- formal full 可在 cutover 前預先完成；實際 cutover 同步 gate 目標五分鐘內；
- 不刪 cron/state/lock、不降品質門檻、不以舊 receipt 假綠。
