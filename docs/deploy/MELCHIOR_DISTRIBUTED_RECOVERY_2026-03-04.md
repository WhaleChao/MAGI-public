# Melchior 分散式推理修復與更新（2026-03-04）

## 背景
- 目前 Melchior 可切換到 distributed，但 `/v1/models` 長時間卡在 `Loading model`，導致分散式逾時。
- 原因多為：模型過大 + 12GB VRAM 無法在合理時間載入，或 llama-server 啟動參數不合適。

## 目標
1. 升級 Melchior 代理到 `melchior_agent_v2.py`（含 `/api/brain/recover`、/api/warmup）。
2. 使用 12GB VRAM 可穩定載入的 GGUF 模型（建議 8B 或 14B）。
3. 保證 `/v1/models` 在 1~3 分鐘內可就緒。

## Melchior 端更新步驟（在 Melchior 主機）
1. **覆蓋代理程式**
   - 將 `For_Melchior_Setup/melchior_agent_v2.py` 上傳到 Melchior 主機並取代舊版本。

2. **設定 `melchior.env`**
   - 檔案位置：與 `melchior_agent_v2.py` 同一資料夾
   - 建議範例（請依實際路徑修改）：
```
MELCHIOR_AGENT_PORT=5002
MELCHIOR_OLLAMA_URL=http://127.0.0.1:11434
MELCHIOR_DEFAULT_MODEL=llama3.1:8b
MELCHIOR_LLAMA_SERVER_BIN=C:\\AI\\llama.cpp\\bin\\llama-server.exe
MELCHIOR_LLAMA_MODEL_PATH=C:\\AI\\models\\llama-3.1-8b-instruct.Q4_K_M.gguf
MELCHIOR_LLAMA_V1_PORT=8080
MELCHIOR_RPC_PORT=50052
MELCHIOR_LLAMA_CTX=4096
MELCHIOR_LLAMA_NGL=35
MELCHIOR_LLAMA_THREADS=0
MELCHIOR_LLAMA_BATCH=0
MELCHIOR_STOP_OLLAMA_IN_DISTRIBUTED=1
```
   - 若 8B 仍卡載入：將 `MELCHIOR_LLAMA_CTX` 降為 `3072`，或 `MELCHIOR_LLAMA_NGL` 降到 `30`。

3. **重新啟動代理**
   - Windows：`start_melchior_v2.bat`
   - 或直接 `python melchior_agent_v2.py`

4. **健康檢查**
```
curl http://127.0.0.1:5002/api/health
curl http://127.0.0.1:8080/v1/models
curl -X POST http://127.0.0.1:5002/api/warmup -H "Content-Type: application/json" -d "{\"model\":\"llama3.1:8b\",\"timeout\":120}"
```

## Casper 端設定（已更新）
- `MAGI_MAIN_MODEL=llama3.1:8b`
- `MAGI_BIG_BRAIN_REMOTE_REPAIR=1`
- `MAGI_BIG_BRAIN_LOADING_GRACE_SEC=600`

## 自測 / 自修復指令
- 自測：
```
python3 /Users/ai/Desktop/MAGI/scripts/ops/selftest_big_brain.py
```
- 自我修復（強制重啟 distributed）：
```
python3 /Users/ai/Desktop/MAGI/scripts/ops/repair_big_brain.py
```

## 成功標準
- `/v1/models` 回傳 200
- `selftest_big_brain.py` 輸出 `probe.success = true`
- MAGI 自動化不再出現 `distributed_loading_model` 逾時
