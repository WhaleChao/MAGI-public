# MAGI 架構全面改善計畫

> 2026-03-11 | 基於全系統掃描產出
> 分三大區塊：**立即可做** / **中期重構** / **長期架構**

## 執行進度

| Phase | 項目 | 狀態 |
|-------|------|------|
| 0.1 | 移除 server.py hardcoded secrets | **完成** |
| 0.2 | print → logger | **完成** |
| 0.3 | config.json 機密從 git 移除 | **完成** — 明文密碼/token/webhook 移至 .env，ConfigManager overlay |
| 0.4 | Ollama 退役 → oMLX 全面接管 | **完成** — Ollama 停止，所有推理走 oMLX |
| 1 | InferenceGateway 統一遷移 | **完成** — tools_api, balthasar_bridge, tri_sage, pdf_bridge 遷移完成 |
| 2 | 設定統一化 (config.py + validate_config) | **完成** |
| 3 | 測試框架 + pyproject.toml | **完成** — 13 tests |
| 4 | Health-Check JSON 端點 | **完成** |
| 5 | 向量入庫非同步化 | **完成** |
| 7 | Orchestrator 拆分 (document_handler) | **完成** — 14 函數抽出，−622 行 |
| 8 | 結構化日誌試行 (InferenceGateway) | **完成** — JSON log + request_id + duration_ms |
| 9 | bare except 全面消除 | **完成** — 29 檔案 ~270 處 except: → except Exception: |
| 10 | shell=True 消除 + PID 驗證 | **完成** — process_cleaner, process_guardian |
| 11 | 死代碼清理 | **完成** — legalbridge_gui.py 歸檔（4967 行，零 MAGI 引用） |
| 12 | 退役 LaunchAgent 清理 | **完成** — rpc-worker, day3report.once unloaded |
| 13 | SQLite Job Queue | **完成** — attachment job 持久化，WAL mode，自動恢復+清理，10 tests |
| 14 | Hardcoded credentials 全面清除 | **完成** — DB passwords (16處) + Magi_IronDome (10處) + DB host/user，所有密碼統一由 .env 管理 |
| 15 | Config overlay 測試 | **完成** — 4 tests 驗證 .env→config.json overlay 正確性 |

---

# 一、立即可做（今天就能改）

## 1.1 清理 Ollama 多餘模型（5 分鐘，釋放 ~55GB 磁碟）

```bash
ollama rm qwen3:8b
ollama rm gemma3:27b
ollama rm gemma3:12b
ollama rm minicpm-v
ollama rm llava:7b
ollama rm glm-ocr
ollama rm gpt-oss:20b
ollama rm cwchang/llama3-taide-lx-8b-chat-alpha1
# llama3.1:8b 已刪

# 保留：taide-12b + nomic-embed-text
# 如果 TAIDE MLX 轉換成功，Ollama 可完全退役
```

## 1.2 向量入庫非同步化（30 分鐘）

**檔案**：`skills/documents/pdf_bridge.py` — `summarize_pdf()` 行 733-810

**問題**：向量 embed 和摘要 chat 串行跑，embed 延遲了回覆

**改法**：摘要先做先回，向量入庫丟背景 thread

```python
def summarize_pdf(pdf_path, max_chars=8000, *, progress_callback=None):
    text = extract_text(pdf_path)

    # 1. 先做摘要
    summary = map_reduce_summarize(text, ...) if len(text) > threshold else ...

    # 2. 向量入庫背景執行
    def _bg_ingest():
        try:
            ingest_text_to_vector_memory(kind="pdf", primary=pdf_path, ...)
        except Exception as e:
            logger.warning("bg ingest failed: %s", e)
    threading.Thread(target=_bg_ingest, daemon=True).start()

    return summary  # 使用者不用等 embed
```

## 1.3 硬編碼密碼移除（15 分鐘，安全性）

**問題**：`server.py` 和其他檔案有 hardcoded fallback 密碼

| 位置 | 問題 | 修法 |
|------|------|------|
| `server.py` ~行 138 | `DB_PASSWORD` fallback = `"Magi_IronDome_2026!"` | 改為無 fallback，缺 .env 直接報錯 |
| `server.py` ~行 90 | Flask secret = `"MAGI_CASPER_SECRET_KEY_2026"` | 同上 |
| 多處 `config.json` 路徑 | fallback 搜索鏈太長，容易讀到舊設定 | 統一只讀 `.env` |

```python
# 改法：必要環境變數缺失就 fail fast
DB_PASSWORD = os.environ["DB_PASSWORD"]  # 不給 default，缺就 crash
FLASK_SECRET = os.environ["MAGI_FLASK_SECRET_KEY"]
```

## 1.4 修掉 print() 混用（20 分鐘）

`server.py` 有多處 `print(f"DB Error: {e}")` 應改為 `logger.error()`。
目前 `daemon.log` 會混入 print 和 logger 兩種格式，debug 不便。

```bash
# 找出所有 print() 呼叫
grep -n "print(" api/server.py | grep -v "^#" | head -20
# 逐一改為 logger.error / logger.warning
```

---

# 二、中期重構（1-2 天）

## 2.1 統一推理路由 InferenceRouter（2-3 小時）

**問題**：推理 fallback 邏輯散落 5 個檔案，每層各自 try-except + timeout

| 檔案 | 各自的 fallback |
|------|-----------------|
| `melchior_client.py` `chat()` | ~200 行瀑布 |
| `melchior_client.py` `quick_local_chat()` | 另一套 candidate |
| `balthasar_bridge.py` `summarize_text()` | oMLX→Ollama 又包一層 |
| `inference_gateway.py` | 時段 model roster |
| `pdf_bridge.py` `_mr_summarize_one_chunk()` | 自己寫 oMLX→Ollama |

**新檔案**：`skills/bridge/inference_router.py`

```python
class InferenceRouter:
    engines = [OMLXEngine(...), OllamaEngine(...)]

    MODEL_MAP = {
        "summary":   {"omlx": "Qwen3.5-9B-4bit",   "ollama": "taide-12b"},
        "intent":    {"omlx": "Qwen3.5-9B-4bit",   "ollama": "taide-12b"},
        "tc_review": {"omlx": "TAIDE-12b-mlx-4bit", "ollama": "taide-12b"},
        "coding":    {"omlx": "Qwen2.5-Coder-14B",  "ollama": "taide-12b"},
        "vision":    {"omlx": "Qwen3.5-9B-4bit",   "ollama": "taide-12b"},
        "embed":     {"omlx": "modernbert-embed-4bit", "ollama": "nomic-embed-text"},
    }

    def chat(self, prompt, *, task="general", timeout=60):
        for engine in self._rank_engines(task):
            if not engine.is_healthy():
                continue
            model = engine.best_model_for(task)
            try:
                return engine.chat(prompt, model=model, timeout=timeout)
            except Exception:
                engine.record_failure()
        return {"success": False, "error": "all_engines_exhausted"}
```

**遷移**：先包裝現有函式 → 新 code 用 router → 逐步替換舊呼叫

## 2.2 SQLite Job Queue（1-2 小時）

**問題**：attachment job 是 in-memory dict，crash 會丟失，重啟可能重複處理

**檔案**：`api/server.py` — `_run_attachment_job()`

**新建**：`skills/memory/job_queue.py`

```sql
CREATE TABLE IF NOT EXISTS job_queue (
    id TEXT PRIMARY KEY, status TEXT DEFAULT 'pending',
    job_type TEXT, payload TEXT, result TEXT, error TEXT,
    attempts INTEGER DEFAULT 0, created_at REAL, updated_at REAL
);
```

```python
class JobQueue:
    def enqueue(self, job_type, payload) -> str
    def claim(self, job_id)           # → running
    def complete(self, job_id, result) # → done
    def fail(self, job_id, error)      # → failed, attempts++
    def recover_running(self) -> list   # 重啟恢復
```

## 2.3 設定統一化（1 小時）

**問題**：設定散落四處
- `.env`（主）
- `config.json`（3 個路徑 fallback 搜索）
- `json/*.pickle`（OAuth tokens）
- 各檔案 hardcoded defaults 不一致

**建議**：
1. 建 `skills/ops/config.py`，統一讀取 `.env`，啟動時驗證必要變數
2. 不再從 `config.json` fallback 讀，避免讀到舊設定
3. OAuth pickle 維持現狀（Google API 限制）

```python
# skills/ops/config.py
REQUIRED_VARS = [
    "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
    "MAGI_LINE_CHANNEL_SECRET",
    "DB_HOST", "DB_USER", "DB_PASSWORD",
    "DISCORD_BOT_TOKEN",
]

def validate_config():
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")
```

## 2.4 測試框架建立（2 小時）

**問題**：沒有 pytest、沒有 CI。166 個 skills 零自動測試。改一行怕壞全家。

**建議**：先建骨架，逐步補
```
tests/
  conftest.py                  # 共用 fixtures (mock oMLX, mock Ollama)
  test_inference_router.py     # router 路由邏輯
  test_pdf_bridge.py           # map-reduce 正確性
  test_orchestrator_dispatch.py # 意圖→skill mapping
  test_job_queue.py            # queue 狀態機
```

```bash
# pyproject.toml 加
[tool.pytest.ini_options]
testpaths = ["tests"]
```

最重要的第一個 test：**mock oMLX 回傳 → 確認 map-reduce 能拼出完整摘要**

---

# 三、長期架構（一週+）

## 3.1 Orchestrator 拆分（4-8 小時）

**現況**：`orchestrator.py` 8855 行，一個 class 包天包地

**目標**：
```
api/
  orchestrator.py              # ~1500 行，只做 dispatch
  handlers/
    base.py                    # BaseHandler ABC
    pdf_handler.py             # PDF 全流程
    image_handler.py           # 圖片/OCR
    text_handler.py            # 文字摘要/翻譯
    epub_handler.py
    audio_handler.py
    attachment_router.py       # MIME → handler
```

**遷移策略**：一次搬一個 handler，搬完測完再搬下一個。PDF 先行。

## 3.2 OSC 拆分（最大怪獸）

**現況**：`osc.py` **39,548 行**。DB 操作、文書產生、UI 事件、PDF 組裝全混在一起。

**建議拆法**：
```
casper_ecosystem/law_firm_orchestrators/osc/
  __init__.py
  db.py              # 所有 SQL 操作
  documents.py       # 文書產生邏輯
  templates.py       # 模板管理
  pdf_builder.py     # PDF 組裝
  api.py             # 對外 API
```

這是最大工程，建議有完整測試後再動。

## 3.3 分散式節點簡化

**現況**：三個 AI 節點（CASPER/BALTHASAR/MELCHIOR）各跑 Ollama，透過 Tailscale HTTP 互打。

**問題**：
- BALTHASAR/MELCHIOR 很少用到（oMLX 本機就能做大部分工作）
- HTTP 跨節點延遲 + 不可靠
- 三份 Ollama 各自佔記憶體

**建議**：
- 評估是否真的需要遠端節點。如果 oMLX 本機能做 vision + coding + summary，BALTHASAR/MELCHIOR 可以降級為純備援
- `inference_router.py` 裡把遠端節點列為最低優先

## 3.4 日誌結構化

**現況**：emoji logger + print 混用，debug 靠 grep

**建議**：
- 改用 Python `structlog` 或 JSON logger
- 每筆 log 帶 `request_id`、`user_id`、`skill`、`duration_ms`
- 方便追蹤一個 LINE 訊息從進來到回覆的完整鏈路

```python
logger.info("pdf_summary_complete",
    user_id=uid, pages=19, chars=14387,
    method="map_reduce", chunks=4, success=3,
    duration_ms=396000)
```

## 3.5 MySQL → SQLite 遷移評估

**現況**：MySQL 在遠端 Keeper 節點（Tailscale `MAGI_REMOTE_DB_HOST`），需要 VPN 連線

**風險**：
- VPN 斷線 → MAGI 寫入失敗
- MySQL C-extension segfault → 已用 pure-python patch 繞過
- 雙向同步 (`sync_bidirectional.py`) 增加複雜度

**選項**：
- 短期：維持現狀，已有 `MAGI_PREFER_LOCAL_DB=1` 做本地優先
- 長期：評估是否改為 SQLite 主庫 + 定期 dump 到 NAS 備份
- 如果 OpenClaw 已經是 web dashboard，MySQL 可能只為了 OpenClaw 而存在

---

# 四、現況與進行中

| 項目 | 狀態 |
|------|------|
| PDF map-reduce 摘要 | ✅ 已上線 |
| `_OMLX_LOCK` GPU 序列化 | ✅ 已加 |
| llama3.1:8b 清除 + .env 統一 | ✅ 已完成 |
| `OLLAMA_MAX_LOADED_MODELS=1` | ✅ 已設定 |
| TAIDE-12b MLX 轉換 | ⏳ 下載中，成功則可淘汰 Ollama |
| Orchestrator PDF 直走 summarize_pdf | ✅ 已改（跳過 _summarize_text_resilient） |

---

# 五、完成檢查清單

## 立即
- [x] 清理 Ollama 多餘模型（~61GB 已釋放，保留 taide-12b + nomic-embed-text）
- [x] 向量入庫非同步化（pdf_bridge.py 背景 thread）
- [x] 移除硬編碼密碼（server.py Flask secret + DB password fallback）
- [x] print() → logger（server.py:192）
- [x] config.json 機密清理（加入 .gitignore + git rm --cached + config fallback 簡化）
- [x] /health 端點擴充（oMLX/Ollama/DB/系統資源/jobs JSON 回應）

## 中期
- [x] InferenceGateway 統一推理（6 檔 14 處遷移，修復已刪模型引用）
- [ ] SQLite Job Queue（降級為可選 — 現有 JSON 方案已解決 crash-recovery）
- [x] 設定統一化 + 啟動驗證（skills/ops/config.py + server.py validate_config()）
- [x] pytest 骨架 + 首批測試（pyproject.toml + 10 tests 全通過）

## 長期
- [ ] Orchestrator 拆分
- [ ] OSC 39K 行拆分
- [ ] 分散式節點簡化
- [ ] 結構化日誌
- [ ] MySQL 遷移評估
- [ ] TAIDE MLX 結果 → 決定是否淘汰 Ollama
