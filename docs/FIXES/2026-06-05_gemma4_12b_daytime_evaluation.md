# Gemma 4 12B 日間模型替換評估（2026-06-05）

## 2026-06-06 更新結論

Gemma 4 12B 已可透過 MAGI 的 Gemma4 unified oMLX source overlay 進行文字推論與工具調用測試，但仍暫不替換 MAGI 日間 E4B 模型。

原因不是模型本身不適合，而是 Homebrew 正式安裝的 oMLX / MLX runtime 尚未支援 `gemma4_unified` 架構。MAGI 已建立一個不覆蓋正式 oMLX 的 source overlay，將 oMLX、mlx-lm、mlx-vlm 固定到已驗證 commit，並補上 oMLX model discovery 對 `gemma4_unified` / `Gemma4UnifiedForConditionalGeneration` 的辨識。

正式路由維持既有日夜模型輪值，12B 僅登錄為候選日間模型。

Overlay wrapper：

```text
/Users/ai/.omlx/bin/omlx-gemma4-unified-serve
```

## 外部來源

- Google 官方 2026-06-03 發表 Gemma 4 12B，定位為 E4B 與 26B MoE 之間的中型模型，支援本機 multimodal agent 工作流。
- Hugging Face 官方模型：`google/gemma-4-12B-it`，Apache 2.0，模型標籤包含 `gemma4_unified`。
- MLX 量化候選：`mlx-community/gemma-4-12B-it-4bit`。

## 2026-06-05 本機測試

下載位置：

```text
/Users/ai/.omlx/models/gemma-4-12B-it-4bit
```

測試服務：

```bash
omlx serve \
  --base-path /Users/ai/.omlx \
  --model-dir /Users/ai/.omlx/models-text-12b-test \
  --max-model-memory 12GB \
  --max-process-memory 16GB \
  --port 18080 \
  --max-concurrent-requests 1 \
  --no-cache \
  --initial-cache-blocks 4
```

結果：

- `/v1/models` 可列出 `gemma-4-12B-it-4bit`。
- `/v1/chat/completions` 連續 3 次回傳 HTTP 500。
- oMLX log 顯示 `Model type gemma4_unified not supported`。
- 本機 Python 檢查：
  - `mlx_lm.models.gemma4_unified`: 不存在
  - `mlx_vlm.models.gemma4_unified`: 不存在

## 2026-06-06 overlay 測試

Overlay 建立指令：

```bash
./venv/bin/python3 scripts/ops/prepare_omlx_gemma4_unified_runtime.py
```

Source pins：

- oMLX：`bac678ec72c97e497d05c3c6d637fa54f1b3d7e3`
- mlx-lm：`04a19108d4a7fd6606319784d07c5be3017b073a`
- mlx-vlm：`d02eee1d51170e8d46e4266261445134c0535979`

驗證結果：

- `mlx.core.new_thread_local_stream`：存在
- `mlx_lm.models.gemma4`：存在
- `mlx_vlm.models.gemma4_unified`：存在
- oMLX model discovery：`gemma-4-12B-it-4bit` 偵測為 `vlm`
- `/v1/models`：通過，列出 `gemma-4-12B-it-4bit`
- 中文短答：通過
- 法律摘要：通過
- OpenAI tool call：通過，行程查詢正確呼叫 `calendar_lookup`，未誤呼叫 `weather_lookup`

Live 證據：

```text
.runtime/gemma4_12b_omlx_overlay_live_20260606.json
```

完整 gate 證據：

```text
.runtime/gemma4_12b_overlay_full_gate_final2_20260606.json
.runtime/gemma4_12b_overlay_full_gate_final2_20260606.txt
```

完整 gate 通過項目：

- overlay import / model discovery
- `/v1/models`
- 繁體中文短答
- 法律摘要日期保真
- @heavy 類翻譯術語保留（司法通譯、能動性、responsibility 等）
- 正式筆錄待辦抽取器：候核辦 = 無下次庭期，7日後追蹤
- 12B 結構化筆錄待辦輸出：候核辦追蹤，不誤寫庭期前
- 工具調用：日曆、天氣、法扶、閱卷/繳費、筆錄、實務見解
- 純摘要不得誤叫工具
- 長輸入中擷取正確期限
- 8 次連續壓力請求

## 不立即替換原因

1. 正式 Homebrew oMLX 仍未原生支援 `gemma4_unified`，目前靠 source overlay。
2. 12B 是 dense 12B，雖然比 26B MoE 更穩定可預期，但仍須完成長時間壓力測試，確認不會 OOM。
3. MAGI 不能只因聊天與工具調用通過就替換日間模型；翻譯、逐字稿、摘要、法律 RAG、PDF 命名、法扶與 OSC 任務都要通過完整 gate。

## 後續部署條件

只有在下列條件全部通過時，才可啟用替換：

1. oMLX / MLX runtime 已支援 `gemma4_unified`。
2. `GET /v1/models`、`POST /v1/chat/completions` 均通過。
3. MAGI 工具調用測試通過：不得把日曆、氣象、法扶、閱卷等工具混用。
4. `@heavy` 翻譯品質 gate 通過。
5. 摘要、逐字稿、PDF 命名、實務見解摘要、法扶問答、OSC 待辦建立 smoke 全部通過。
6. 壓力測試下沒有 OOM，且 NAS / DB / Web API 不被拖垮。

## 狀態

狀態：候選模型，overlay runtime 已可用，等待完整 MAGI gate 與長測。

禁止行為：不要把 `gemma-4-12B-it-4bit` 設為日間預設模型，也不要改寫 production model routing 指向 12B，除非完整 live gate 通過。
