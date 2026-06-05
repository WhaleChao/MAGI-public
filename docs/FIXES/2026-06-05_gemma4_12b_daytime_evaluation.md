# Gemma 4 12B 日間模型替換評估（2026-06-05）

## 結論

Gemma 4 12B 暫不替換 MAGI 日間 E4B 模型。

原因不是模型本身不適合，而是目前 MAGI 使用的本機 oMLX / MLX runtime 尚未支援 `gemma4_unified` 架構。實測 12B 可下載、可被 oMLX 列為模型，但一旦呼叫 chat completions 會回傳 HTTP 500，因此不能上線，也不能留下半套替換。

正式路由維持既有日夜模型輪值，12B 僅保留為候選模型。

## 外部來源

- Google 官方 2026-06-03 發表 Gemma 4 12B，定位為 E4B 與 26B MoE 之間的中型模型，支援本機 multimodal agent 工作流。
- Hugging Face 官方模型：`google/gemma-4-12B-it`，Apache 2.0，模型標籤包含 `gemma4_unified`。
- MLX 量化候選：`mlx-community/gemma-4-12B-it-4bit`。

## 本機測試

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

## 不替換原因

1. 目前 runtime 不能成功推論，替換後會造成 MAGI 日間主模型直接不可用。
2. MAGI 已退役 Ollama 作為主服務，不應為單一模型臨時恢復第二套推論路由。
3. 12B 是新的 unified multimodal 架構，必須先通過工具調用、摘要、翻譯、逐字稿、法律 RAG 與壓力測試 gate，才能替換 E4B。

## 後續部署條件

只有在下列條件全部通過時，才可啟用替換：

1. oMLX / MLX runtime 已支援 `gemma4_unified`。
2. `GET /v1/models`、`POST /v1/chat/completions` 均通過。
3. MAGI 工具調用測試通過：不得把日曆、氣象、法扶、閱卷等工具混用。
4. `@heavy` 翻譯品質 gate 通過。
5. 摘要、逐字稿、PDF 命名、實務見解摘要、法扶問答、OSC 待辦建立 smoke 全部通過。
6. 壓力測試下沒有 OOM，且 NAS / DB / Web API 不被拖垮。

## 狀態

狀態：候選模型，等待 runtime 支援。

禁止行為：不要把 `gemma-4-12B-it-4bit` 設為日間預設模型，也不要改寫 production model routing 指向 12B。
