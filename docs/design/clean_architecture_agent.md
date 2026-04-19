# MAGI v2 — 乾淨架構 AI Agent 設計文件

**作者**：Claude Sonnet（執行）+ 4/19 最終審查報告（Opus 輸入）
**日期**：2026-04-19
**狀態**：草案 v1（待使用者確認後拆分 Phase 執行）
**前置**：Opus 設計 / Sonnet 執行 / Opus 驗證（2026-04-19 分工規則）

---

## 0. TL;DR

MAGI v2 功能上已經「能打仗」，全套測試 1499+ passed，六模組（法扶/閱卷/筆錄/摘要/翻譯/逐字稿）live E2E 都通。但長期縫縫補補，架構有 **8 類系統性債務**，每次修 bug 都是在補丁上打補丁，不是在解根因。

**本設計文件的目標**：用 4 個 Phase、估 6-8 週的工作量，把 MAGI v2 從「能打仗」收斂到「可放心無腦擴充」的狀態。

**收斂後會達到 4 個可驗證的特性**：

1. **每個遠端依賴都走同一層** — circuit breaker / backoff / degraded fallback 不再是各寫各的
2. **每個 subprocess 都是 argv 形式** — 不再有 `shell=True` / `os.system` 插值
3. **每個 timeout 都有 inflight 治理** — 不再只有 `/osc/external/chat` 一條路受保護
4. **repo 永遠是 clean 的** — runtime state / scratch / smoke 不再汙染工作樹

---

## 1. 問題盤點（整合 4/19 最終審查 + 可靠性審計）

### 🔴 P1 — 結構性風險（會導致事故或重複補丁）

| # | 類別 | 位置 / 範圍 | 症狀 | 根因 |
|---|---|---|---|---|
| P1-A | **遠端依賴** | 全系統（Balthasar / Melchior / LAF / 司法院 / ezlawyer / NAS / oMLX / NIM） | 每個 call site 自己寫防護，有的做對有的沒做，bug「修過超多次」 | 沒有架構層 `RemoteHealthGate` — 各自為政 |
| P1-B | **Shell 執行面** | `daemon.py:628-633` cron fallback + `daemon.py:1530-1532` reviewer launchctl | `shell=True` + prefix allowlist / `os.system(f"...{var}...")` 字串插值 | 只靠 `startswith()` 檢查無法防 `;` `&&` command substitution |
| P1-C | **Timeout 治理不一致** | 只有 `/osc/external/chat` 有 inflight counter，`/search` `/research` `/fetch` `/summarize` `/transcribe` 都還用同一套 no-op cancel | API 已 timeout 回應但背景任務未必停，多端點同時卡住會累積吃 RAM | `_run_with_timeout()` 只是 `future.result(timeout=)`，`future.cancel()` 對已執行的 `ThreadPoolExecutor` task 是 no-op |

### 🟡 P2 — 工程化缺口（不會爆但拖慢所有迭代）

| # | 類別 | 位置 / 範圍 | 症狀 | 根因 |
|---|---|---|---|---|
| P2-D | **Repo 邊界** | `cron_jobs.json` / `static/external_chat_metrics.jsonl` 被追蹤；`.runtime/` `_laf_smoke/` `scratch/` `static/worldmonitor_reports/` 長期在 repo 內生成 | 工作樹永遠 dirty，真正 regression 與 runtime 噪音混在一起 | runtime state 沒有搬出 repo root |
| P2-E | **文件 / 封裝漂移** | `README.md:47` 寫 Python 3.9+；`pyproject.toml:9` 寫 `>=3.12`；README 講 `magi status`，pyproject 只有 `magi-start` / `magi-check` | 新機器按 README 做會假紅燈 | 實際可用路徑與文件說法不同步 |
| P2-F | **Monolith gate 覆蓋不足** | `skills/magi-autopilot/action.py` 5741 行 / `skills/legal/laf.py` 5682 行 / `skills/judgment-collector/action.py` 4956 行 / `skills/pdf-namer/action.py` 3951 行 | CI monolith gate 只管 `api/server.py` / `api/orchestrator.py` / `templates/osc.html`，真正熱區沒在 gate 內 | 大檔 + 高變動 + 高耦合 → 回歸風險急速上升 |

### 🟢 P3 — 值得注意但不急

| # | 類別 | 位置 | 說明 |
|---|---|---|---|
| P3-G | **Import-time 副作用** | `api/tools_api.py:3301-3302` warmup thread | 單純 `import module` 帶副作用，測試/腳本/REPL 行為難預測 |
| P3-H | **except-everything 模式** | 全系統多處 `except Exception: pass` | 高度依賴「吞錯後 best-effort 繼續」，真正問題會延後到更難追的地方才爆 |

---

## 2. 架構原則（North Star）

以下 7 條是收斂後要能站得住的原則。每個 Phase 的 PR 必須對照這 7 條檢查：

1. **單一共用入口（Single Gate）** — 同類行為（遠端呼叫、subprocess、timeout、log）只能有一個官方入口，不得 ad-hoc 自寫
2. **Fail fast, fail loud, fail logged** — 錯誤有明確分類、必可觀測、不得靜默 `pass`；degraded 模式必須標記 `degraded=True`
3. **無 shell 字串插值** — 所有 subprocess 一律 argv list，需要 shell feature 時明確標註並走 denylist + 結構化 parser
4. **無可變 runtime state 進 repo** — 所有 `.json` / `.jsonl` / `.log` / `.png` 受 `MAGI_RUNTIME_DIR` 管理，repo 內只留 example
5. **Cancellation token 而非 timeout-and-pray** — 長任務必須接受 cancellation signal，不能只靠 API timeout 回應就當任務停了
6. **Import 無副作用** — module import 不啟動 thread / 不連 DB / 不建檔；startup hooks 顯式呼叫
7. **Typed error over exception** — 關鍵路徑用 `Result[T, ErrorCode]` 或 dataclass error，不靠 catch-all `except`

---

## 3. 目標架構（分層圖）

```
┌─────────────────────────────────────────────────────────────────┐
│  L0  INTERFACE         Discord / LINE / Telegram / CLI / HTTP   │
│                        api/pipelines/message_pipeline.py         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  L1  ORCHESTRATION     意圖分派 / 工具路由                        │
│                        api/orchestrator.py                       │
│                        api/pipelines/command_dispatch.py         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  L2  DOMAIN SKILLS     laf / file-review / transcript / pdf-*    │
│                        translator / summarize / research         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  L3  INFERENCE GATE    Casper / Melchior / Balthasar / NIM      │
│                        skills/bridge/inference_gateway.py        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  L4  PLATFORM KERNEL   ⭐ 這一層是本文件的重點 ⭐                 │
│                        （目前缺失 / 各自為政 / 需抽提）           │
│  ├─ RemoteHealthGate    統一遠端可達性 / circuit breaker         │
│  ├─ SafeProcess         統一 subprocess argv / denylist          │
│  ├─ TaskExecutor        統一 inflight / cancellation / timeout   │
│  ├─ RuntimeDir          統一 runtime state 路徑                  │
│  ├─ ErrorCatalog        統一 typed error + 分類                  │
│  └─ ObservabilityHub    統一 metrics / log / degraded marker     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  L5  EXTERNAL SERVICES Tailscale peers / Gmail / NAS / portals   │
│                        oMLX / NIM / Scrapling / Playwright       │
└─────────────────────────────────────────────────────────────────┘
```

**核心洞察**：現在 L2/L3 的每個模組都直接碰 L5，中間沒有 **L4 Platform Kernel**。各自為政就是因為缺這一層。

---

## 4. Platform Kernel 六元件規格

### 4.1 `RemoteHealthGate`（解 P1-A）

**檔案**：`skills/platform/remote_health_gate.py`（新增）

**動機**：把今天修 Balthasar 的 pattern 與 `melchior_client.py:146` 已經做對的 CB 邏輯抽成共用基礎設施。

**API**：

```python
class RemoteHealthGate:
    """All remote health probes + circuit breakers go through here."""

    def register(
        self,
        peer_id: str,                    # e.g. "balthasar", "melchior", "laf_portal"
        probe_fn: Callable[[], bool],    # domain-specific health check
        threshold: int = 2,              # trip after N consecutive failures
        ttl_sec: int = 300,              # circuit open duration
        backoff_curve: List[int] = None, # e.g. [30, 90, 180] for escalating CB
        on_trip: Callable = None,        # hook for audit marker / notification
    ) -> None: ...

    def is_reachable(self, peer_id: str, cache_ttl: int = 30) -> bool: ...
    def mark_unhealthy(self, peer_id: str, reason: str) -> None: ...
    def circuit_status(self, peer_id: str) -> CircuitStatus: ...
    def force_reset(self, peer_id: str) -> None: ...
```

**接入清單**（驗收時需逐一通過）：

- [ ] Balthasar (`100.118.235.126:5002`) — 已有今日補丁，搬到 gate
- [ ] Melchior (`100.116.54.16`) — 已有 `_CIRCUIT_BREAKER`，搬到 gate
- [ ] NIM (`integrate.api.nvidia.com`) — 已有 circuit breaker，搬到 gate
- [ ] LAF portal (`laf.org.tw`) — 新接入
- [ ] ezlawyer / 司法院 (`www.judicial.gov.tw`) — 新接入
- [ ] Gmail API — 新接入（配合 token 自動 refresh）
- [ ] NAS SMB (`whale-1.taillscale.net`) — 新接入
- [ ] oMLX (`127.0.0.1:8080/8082/8083`) — 新接入（kernel panic pattern detection）

**驗收**：`tests/test_remote_health_gate.py` 覆蓋 10+ tests；live smoke 對每個 peer 做 N 連續失敗 → 短路 → TTL 過期 → 自動恢復。

---

### 4.2 `SafeProcess`（解 P1-B）

**檔案**：`skills/platform/safe_process.py`（新增）

**動機**：`daemon.py:628-633` / `1530-1532` 的 `shell=True` / `os.system` 必須拔乾淨。

**API**：

```python
class SafeProcess:
    """All subprocess execution goes through here. No shell=True ever."""

    @staticmethod
    def run(
        argv: List[str],                 # MUST be list, not string
        timeout: int,
        cwd: Optional[str] = None,
        env: Optional[Dict] = None,
        allowed_commands: Optional[Set[str]] = None,  # argv[0] whitelist
    ) -> ProcessResult:
        """Blocks until done. Safe argv execution only."""

    @staticmethod
    def launchctl(op: Literal["bootout", "bootstrap", "kickstart"],
                  label: str, plist: Optional[str] = None) -> ProcessResult:
        """Typed wrapper for launchctl ops. No shell injection possible."""

    @staticmethod
    def parse_cron_command(command: str) -> List[str]:
        """Parse cron_jobs.json command strings into safe argv.
        Rejects shell metacharacters: ; & | > < ` $( ) { }"""
```

**Migration map**（從 `shell=True` → argv）：

```python
# Before (daemon.py:632)
subprocess.run(command, shell=True, ...)

# After
argv = SafeProcess.parse_cron_command(command)
result = SafeProcess.run(argv, timeout=...)
```

```python
# Before (daemon.py:1530)
os.system(f'launchctl bootout gui/{_uid}/{_label} 2>/dev/null')

# After
SafeProcess.launchctl("bootout", label=f"gui/{_uid}/{_label}")
```

**驗收**：
- 全 repo `grep -rE "shell\s*=\s*True" skills/ api/ daemon.py` → 必須 0 match
- 全 repo `grep -rE "os\.system\(" skills/ api/ daemon.py` → 必須 0 match
- `tests/test_safe_process.py` 含 shell injection 攻擊測試（`"ls; rm -rf /"` 必須被拒）

---

### 4.3 `TaskExecutor`（解 P1-C）

**檔案**：`skills/platform/task_executor.py`（新增）

**動機**：把 `/osc/external/chat` 已有的 inflight/backpressure 模型擴到所有重路徑，並補上 cancellation token。

**API**：

```python
class TaskExecutor:
    """Unified timeout + cancellation + inflight backpressure."""

    def __init__(self, name: str, max_concurrent: int, max_queue: int): ...

    def submit(
        self,
        fn: Callable[[CancellationToken], T],   # fn MUST accept cancel token
        timeout: int,
        priority: int = 0,
    ) -> Future[T]: ...

    def inflight_count(self) -> int: ...
    def queue_depth(self) -> int: ...
    def stats(self) -> ExecutorStats: ...

class CancellationToken:
    """Fn periodically checks this to cooperatively cancel."""
    def is_cancelled(self) -> bool: ...
    def check(self) -> None:  # raises CancelledError
```

**接入清單**（必須通過 cancellation check）：

- [ ] `/osc/external/chat` — 已有 inflight，補 cancel token
- [ ] `/search` `/research` `/fetch` — 新接入
- [ ] `/summarize` — 新接入
- [ ] `/transcribe` — 新接入
- [ ] `/translate` — 新接入（translator fork-bomb defense 已有，整合進來）

**驗收**：`tests/test_task_executor.py` 覆蓋：(1) timeout 後 fn 真的停；(2) max_concurrent 超過時會 queue；(3) queue 滿時回 429；(4) cancellation token 可 cooperative cancel。

---

### 4.4 `RuntimeDir`（解 P2-D）

**檔案**：`api/runtime_paths.py`（已存在，擴充）

**動機**：所有 runtime state 集中到 `$MAGI_RUNTIME_DIR`（預設 `~/.magi/runtime/`），repo 永遠 clean。

**API**：

```python
class RuntimeDir:
    """All mutable runtime state lives here. Repo stays clean."""

    @staticmethod
    def get(*subpaths: str) -> Path: ...

    @staticmethod
    def for_cron_state() -> Path:  # cron_jobs.json last_run
        return RuntimeDir.get("cron", "state.json")

    @staticmethod
    def for_metrics(name: str) -> Path:
        return RuntimeDir.get("metrics", f"{name}.jsonl")

    @staticmethod
    def for_audit_marker(category: str) -> Path:
        return RuntimeDir.get("audit", category)
```

**Migration map**：

| 現況 | 搬到 |
|---|---|
| `cron_jobs.json`（被追蹤） | Repo 保留 template；state 搬到 `$MAGI_RUNTIME_DIR/cron/state.json` |
| `static/external_chat_metrics.jsonl` | `$MAGI_RUNTIME_DIR/metrics/external_chat.jsonl` |
| `.runtime/*` | `$MAGI_RUNTIME_DIR/*`（保留結構） |
| `_laf_smoke/` / `scratch/` | `$MAGI_RUNTIME_DIR/smoke/laf/` + `.gitignore` |
| `static/worldmonitor_reports/` | `$MAGI_RUNTIME_DIR/reports/worldmonitor/` |

**驗收**：
- `git status` 在任何 runtime 操作後必須是 clean（除非主動改 source code）
- `tests/test_runtime_dir.py` 驗證所有路徑都在 `$MAGI_RUNTIME_DIR` 下

---

### 4.5 `ErrorCatalog`（解 P3-H）

**檔案**：`skills/platform/error_catalog.py`（新增）

**動機**：把 `skills/engine/error_classifier.py`（2026-04-16 已建）的 13 類錯誤擴成跨平台分類，替換零散的 `except Exception: pass`。

**API**：

```python
@dataclass(frozen=True)
class ErrorCode:
    code: str                # e.g. "REMOTE_UNREACHABLE"
    category: Literal["remote", "input", "auth", "quota", "internal"]
    retryable: bool
    should_compress: bool
    should_fallback: bool
    user_message_template: str

class ErrorCatalog:
    REMOTE_UNREACHABLE = ErrorCode(...)
    REMOTE_TIMEOUT = ErrorCode(...)
    AUTH_EXPIRED = ErrorCode(...)
    QUOTA_EXCEEDED = ErrorCode(...)
    OCR_GARBLED = ErrorCode(...)
    # ... etc.

    @staticmethod
    def classify(exc: Exception) -> ErrorCode: ...
```

**驗收**：新功能必須 raise `MAGIError(code=ErrorCode.X, ...)`，禁止 bare `except Exception`。Migration 以 PR 為單位，不要求一次全改。

---

### 4.6 `ObservabilityHub`（解 degraded audit 需求）

**檔案**：`skills/platform/observability.py`（新增）

**動機**：把今天 Balthasar 寫 Synology marker 的 pattern 做成預設行為：**所有 degraded 模式都自動寫 audit marker，律師事後可查**。

**API**：

```python
class ObservabilityHub:
    @staticmethod
    def emit_degraded(
        component: str,          # e.g. "balthasar", "laf_login"
        reason: str,
        case_context: Optional[Dict] = None,  # client_name / case_no 等
    ) -> None:
        """Writes audit marker to Synology Drive + metrics jsonl."""

    @staticmethod
    def emit_metric(name: str, value: float, tags: Dict) -> None: ...

    @staticmethod
    def query_degraded_events(since: datetime) -> List[DegradedEvent]: ...
```

---

## 5. 執行 Roadmap（4 Phase，估 6-8 週）

### Phase 1：Platform Kernel 基礎（週 1-2，低風險 refactor）

**目標**：建立 L4 六元件骨架，內部行為不變。

**工作項**：
1. 建 `skills/platform/` 目錄
2. 實作 `RemoteHealthGate` — 把 `melchior_client.py:146` 的 CB 邏輯抽出來
3. 實作 `SafeProcess` — 先只做 API，不動 call site
4. 實作 `TaskExecutor` — 先只做 API，不動 call site
5. 實作 `RuntimeDir` — 擴充 `api/runtime_paths.py`
6. 實作 `ErrorCatalog` — 吸收 `error_classifier.py`
7. 實作 `ObservabilityHub` — 統一 degraded audit

**退出條件**（Phase 1 完成判準）：
- 六個模組各自有 unit tests（每個 ≥ 8 tests）
- 全套 pytest 零回歸
- 無任何生產 call site 被動到

### Phase 2：遠端依賴收斂（週 3-4，主攻 P1-A）

**目標**：8 個遠端依賴全部走 `RemoteHealthGate`。

**工作項**：每個 peer 一個 PR，每個 PR 含：
1. 把 ad-hoc CB / retry 邏輯移到 gate
2. 對應的 unit tests
3. Live smoke 驗證真實 peer 掛掉時會短路
4. 對應的 degraded audit marker

**接入順序**（按風險從低到高）：
1. Balthasar（已做，搬家而已）
2. Melchior（已有 CB，搬家）
3. NIM（已有 CB，搬家）
4. oMLX（加 kernel panic pattern detection）
5. NAS SMB（取代 nas_mount_guard 的 ping 邏輯）
6. Gmail API（結合 token auto-refresh）
7. 司法院 / ezlawyer（取代 `judicial.py:1327` 的 502 retry）
8. LAF portal（最複雜，CAPTCHA + 登入 retry）

**退出條件**：
- 每個 peer 有 live smoke 紀錄
- 全 repo `grep` 不到散落的 circuit breaker state
- `balthasar_circuit_status` / `melchior_circuit_status` 等 public API 移除（改走 gate）

### Phase 3：Shell safety + Timeout 治理（週 5-6，主攻 P1-B / P1-C）

**目標**：拔 `shell=True` / `os.system`，擴 TaskExecutor 到所有重路徑。

**工作項**：

**Shell safety**：
1. `daemon.py:628-633` cron fallback → `SafeProcess.run(argv)`
2. `daemon.py:1530-1532` reviewer launchctl → `SafeProcess.launchctl()`
3. 新增 CI gate：`scripts/ci/check_shell_true.py` 禁止 `shell=True`

**Timeout 治理**：
1. `/search` `/research` `/fetch` `/summarize` `/transcribe` `/translate` 全部改用 `TaskExecutor`
2. 長任務 fn 改為接受 cancellation token
3. 新增 metrics：inflight / queue_depth / cancelled_count

**退出條件**：
- CI gate `check_shell_true.py` 通過
- 所有重路徑有 inflight 上限 + cancellation 支援
- Live smoke：5 concurrent 任何重路徑，超過上限必須 429

### Phase 4：Repo 邊界 + 文件統一 + Monolith gate 擴充（週 7-8，P2 收尾）

**目標**：收尾所有工程化缺口。

**工作項**：

**Repo 邊界**：
1. `cron_jobs.json` 分成 `template` + `state`，後者搬出 repo
2. `static/external_chat_metrics.jsonl` 搬到 `$MAGI_RUNTIME_DIR`
3. `.runtime/` `_laf_smoke/` `scratch/` 加入 `.gitignore`（如果還沒）
4. 新增 CI gate：`scripts/ci/check_clean_repo.py` 偵測 runtime state 被追蹤

**文件統一**：
1. `README.md` Python 版本對齊 `pyproject.toml`
2. 唯一官方入口：`./venv/bin/pytest` / `magi-check` / `magi-start`
3. 新增 `docs/OPERATOR_RUNBOOK.md` 作為單一 source of truth

**Monolith gate 擴充**：
1. `scripts/ci/check_monolith_size.py` 加入熱點檔：
   - `skills/magi-autopilot/action.py`（5741 → 目標 < 2000）
   - `skills/legal/laf.py`（5682 → 目標 < 2000）
   - `skills/judgment-collector/action.py`（4956 → 目標 < 2000）
   - `skills/pdf-namer/action.py`（3951 → 目標 < 1500）
2. 每個熱點檔寫拆分計畫文件（放 `docs/design/monolith_*.md`）

**退出條件**：
- `git status` 在任何操作後是 clean
- 新機器按 `README.md` 做可直接通過 `magi-check`
- Monolith gate 所有檔案在閾值內，或有排程拆分計畫

---

## 6. Non-goals（明確不做的事）

為了範圍收斂，以下**不納入**本設計：

- ❌ **不重寫業務邏輯** — 法扶 / 閱卷 / 筆錄的業務流程不動
- ❌ **不改對外 API** — `/osc/external/chat` / `/summarize` 等 payload 結構不變
- ❌ **不換模型 / 不換推理引擎** — oMLX / Gemma / Phi-4 / SmolLM 維持
- ❌ **不動 Discord / LINE / Telegram channel routing** — L0 層不變
- ❌ **不換 DB** — MariaDB + FAISS 維持
- ❌ **不引入新的外部服務** — 只收斂現有依賴，不加新的
- ❌ **不做完整 async 重寫** — 維持現有 thread-based 模型
- ❌ **不拆 orchestrator 以外的熱點檔** — Phase 4 只建立 gate + 計畫，實際拆是後續獨立工作

---

## 7. 驗收標準（Done Definition）

每個 Phase 都必須通過以下 7 項檢查才算結束：

| # | 檢查項 | 方法 |
|---|---|---|
| V1 | 全套 pytest 零回歸 | `./venv/bin/pytest -q` |
| V2 | `check_hardcodes` PASS | `scripts/ci/check_hardcodes.py` |
| V3 | 相關 live smoke 通過 | 每 Phase 有對應的 smoke script |
| V4 | Code review 遵守 7 條原則 | PR checklist |
| V5 | CLAUDE.md 有正確驗收層級標示 | 「測試」vs「驗收」分清楚 |
| V6 | magi status 全綠 + health OK | `magi restart` 後驗證 |
| V7 | 六模組 self_test success=true | LAF / 閱卷 / 筆錄 / 摘要 / 翻譯 / 逐字稿 |

---

## 8. 風險與 Rollback

### 已識別風險

| 風險 | 發生機率 | 影響 | 緩解 |
|---|---|---|---|
| Phase 2 搬家時遺漏 edge case | 中 | 某個遠端掛掉時行為變差 | 每個 peer 有 live smoke，且保留原 API 一個版本做 deprecation |
| Phase 3 改 `shell=True` 破壞 cron job | 中 | 某個 cron job 無聲失敗 | `SafeProcess.parse_cron_command()` 遇到無法 parse 時 log error 並 alert admin，不是靜默 skip |
| Phase 4 搬 runtime state 弄壞 dedup 記憶 | 高 | LAF / 閱卷重複通知 | Migration 腳本 + 保留舊路徑一週做 dual-read |
| 6-8 週總工期估錯 | 中 | 壓不進其他工作 | 每 Phase 獨立，可隨時暫停；任何時點都不會比現況更差 |

### Rollback 策略

每個 PR 必須滿足：

1. **可獨立 revert** — 單 PR revert 不破壞其他 PR
2. **feature flag 保護** — 新行為預設關閉，`MAGI_USE_PLATFORM_KERNEL=1` 才啟用
3. **舊 API 至少保留一個 Phase** — Phase 2 搬 CB 時，`balthasar_circuit_status()` 等舊 API 在 Phase 2 內仍可用，Phase 3 才移除

---

## 9. 執行決策清單（給使用者）

在開始 Phase 1 前，需要使用者確認以下：

- [ ] **優先順序確認**：P1-A（遠端依賴）先，還是 P1-B（shell safety）先？建議 P1-A，因為它是今天你問「有沒有同類 bug」的直接答案
- [ ] **時程確認**：6-8 週是否接受？如果要壓縮，建議只做 Phase 1-2（4 週，解掉最痛的遠端依賴問題）
- [ ] **Feature flag 策略**：所有新元件預設 opt-in（`MAGI_USE_PLATFORM_KERNEL=0` → 舊行為）還是 opt-out（預設啟用，失敗自動回退）？建議 opt-in 到 Phase 2 結束，之後轉 opt-out
- [ ] **是否需要 Opus 再做一次設計審查**：按 2026-04-19 分工規則「Opus 設計 / Sonnet 執行 / Opus 驗證」，本文件由 Sonnet 起草，是否要 Opus 過一次再開工？
- [ ] **Phase 2 peer 接入順序**：按本文件的 1-8 順序，還是你想先修 LAF（最痛）？

---

## 10. 附錄

### A. 相關文件

- **4/19 最終審查報告**：`/Users/ai/Desktop/MAGI_v2_最終審查報告_20260419.md`（P1/P2 列表來源）
- **NIM 整合設計**：`/Users/ai/Desktop/MAGI_v2_NIM整合後續交付_20260419_SONNET_EXEC.md`（遠端 fallback 先例）
- **Assistant Memory 三層**：`docs/design/assistant_memory_three_layers.md`（類似收斂的先例）
- **CLAUDE.md**：完整變更歷史 + standing instruction

### B. 已經做對的參考實作（可抄）

| 元件 | 位置 | 為什麼值得抄 |
|---|---|---|
| Melchior CB | `skills/bridge/melchior_client.py:146-156` | 完整的 `_CIRCUIT_BREAKER` 狀態 + 指數冷卻 30→90→180s |
| NIM circuit breaker | `skills/bridge/nim_heavy.py` | 完整的 PII scrub + budget guard + BoundedSemaphore |
| http_pool retry | `skills/bridge/http_pool.py:18-24` | urllib3 `Retry(backoff_factor=0.3)` + status force-list |
| External chat backpressure | `api/tools_api.py:988-1020` | inflight counter + 429 回應 |
| Translator fork-bomb defense | `skills/translator/action.py` | subprocess 孤兒防護 |
| Assistant Memory 三層 | `api/session/conversation_history.py` | SQLite + 自動升級 gate + 安全紅線 |

### C. 術語表

| 詞 | 定義 |
|---|---|
| **Circuit Breaker (CB)** | 連續失敗 N 次後短路，冷卻 TTL 秒後自動恢復，避免重複踩雷 |
| **Inflight counter** | 當前執行中的任務數，超過上限回 429 做 backpressure |
| **Cancellation token** | 任務週期性檢查的取消信號，支援 cooperative cancel（vs `future.cancel()` 的 no-op） |
| **Degraded mode** | 主路徑失敗、走次要路徑時的狀態；必須標記 `degraded=True` 且寫 audit marker |
| **Audit marker** | Synology Drive 上的 JSON 檔，紀錄某案件某時點走了 degraded 模式 |
| **Platform Kernel** | L4 層的六元件，所有 L2/L3 模組透過它存取 L5 外部服務 |

---

**結語**

這份文件不是「要你現在全部做完」。它的價值是**讓每次修 bug 都能放在正確的架構位置上**，而不是「又在補丁上打補丁」。

即便只先做 Phase 1（兩週），你就已經有了六個共用元件的骨架；下次再遇到 Balthasar 這種問題，就是 `RemoteHealthGate.register(...)` 一行的事，不用像今天花半天重寫 probe 邏輯。

**請用決策清單（§9）回覆，Sonnet 就可以開工。**
