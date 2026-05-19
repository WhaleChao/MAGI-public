# 2026-05-05 — Nemotron Parse OMLX/MLX Phase 2a/2b/2c 完成紀錄

來源桌面計劃：`NVIDIA_Nemotron_Parse_OMLX移植完整實作計劃_20260427.md`（已移至 `/Users/ai/Desktop/desktop_md_archive_20260505/`）。

## 結論

本項不是尚未清理的桌面 MD，而是一個大型 R&D backlog。桌面計劃已被後續三份 MAGI 文件取代：

- `docs/FIXES/2026-04-28_nemotron_parse_community_port_survey.md`
- `docs/FIXES/2026-04-28_nemotron_parse_model_audit.md`
- `docs/FIXES/2026-04-29_nemotron_parse_phase2_design.md`

因此本項自桌面未完成清單移除，改由上述文件作為主要依據。本輪已完成可驗證的 MLX Phase 2a/2b/2c 切片：權重轉換、image processor、C-RADIO encoder、MBart decoder/generation、sidecar、provider client，並已接入 MAGI OCR consensus / Tools API / PDF OCR 的啟用路徑。

## 先前已完成

- Phase -1：社群 MLX port 調查，結論為沒有可直接接入的現成 port，需完全自寫。
- Phase 0：離線架構審計，已釐清 C-RADIO encoder、neck、MBart decoder、processor、權重 dtype、cache 與記憶體風險。
- Phase 1 路線修正：HF baseline 在 24GB Mac 上不可行，改以 NVIDIA `golden_outputs.json` 作 oracle。
- Phase 2 設計：確定走獨立 sidecar（port 8094），不掛 oMLX engine_pool，預設 disabled。

## 本輪已落地（2026-05-05）

新增：

- `scripts/ops/convert_nemotron_parse_to_mlx.py`
- `skills/engine/ocr/nemotron_mlx/config.py`
- `skills/engine/ocr/nemotron_mlx/weight_map.py`
- `skills/engine/ocr/nemotron_mlx/image_processor.py`
- `skills/engine/ocr/nemotron_mlx/radio_encoder.py`
- `skills/engine/ocr/nemotron_mlx/runtime.py`
- `skills/engine/ocr/nemotron_parse_provider.py`
- `skills/engine/ocr/consensus.py`
- `skills/documents/pdf_bridge.py`
- `api/tools_api.py`
- `scripts/serve_nemotron_parse_omlx.py`
- `config/launchagents/com.magi.omlx-nemotron-parse.plist`
- `tests/test_nemotron_mlx_phase2.py`

### Phase 2a：bf16 權重轉換

指令：

```bash
./venv/bin/python scripts/ops/convert_nemotron_parse_to_mlx.py --dtype bf16
```

結果：

- 輸出：`~/.omlx/models-vision/nemotron-parse-v1.2-mlx/bf16/model.safetensors`
- Manifest：`~/.omlx/models-vision/nemotron-parse-v1.2-mlx/bf16/conversion_manifest.json`
- `tensor_count=667`
- `skipped_tensors=[]`
- 輸出大小：`1,872,622,547 bytes`
- true conv transpose：
  - `encoder.conv1.weight`: `[1024,1280,1] -> [1024,1,1280]`
  - `encoder.conv2.weight`: `[1024,1024,1,4] -> [1024,1,4,1024]`
- `summary_idxs` 轉為 `int32`

重要修正：本機 HF snapshot 的 patch embedding 實際為 `encoder.model_encoder.radio_model.model.patch_generator.embedder.weight [1280,768]`，不是舊設計檔假設的 Conv2d patch embed，因此轉換時保留為 linear projection，不做 conv transpose。

### Phase 2b：image processor golden 對齊

指令：

```bash
./venv/bin/python -m skills.engine.ocr.nemotron_mlx.image_processor --self-test
```

結果：

- shape `[1,3,2048,1664]`
- mean `0.06537187099456787`
- std `0.2386380136013031`
- first 20 values 全 `0.0`
- `ok=true`

注意：HF snapshot 裡 `PadIfNeeded(value=[255,255,255])` 在目前 albumentations 版本會被警告為無效參數，實際 golden 是黑色 padding。本輪純 PIL/numpy processor 明確複製這個 golden 行為。

### 本輪測試

```bash
./venv/bin/python -m pytest -q tests/test_nemotron_mlx_phase2.py
```

結果：`7 passed`。

覆蓋：

- image processor golden self-test
- 代表性 tensor name mapping
- bf16 conversion manifest 與輸出檔一致性（本機轉檔存在時）
- sidecar `/health` / `/parse` 外殼
- provider client 預設 disabled safety gate
- `MAGI_NEMOTRON_PARSE_ENABLE=1` 時，Nemotron Parse 會成為 OCR consensus 第三 provider
- `/vision`、`/shortcut/ocr` 與 PDF OCR 在 Nemotron 啟用時會進入 consensus 路徑

LaunchAgent plist 驗證：

```bash
plutil -lint config/launchagents/com.magi.omlx-nemotron-parse.plist
```

結果：`OK`。plist 提供手動 bootstrap 用的 sidecar 設定，`RunAtLoad=false`、`KeepAlive=false`，避免本輪驗證後擅自常駐佔用記憶體。

### Phase 2c：MLX runtime golden 對齊

指令：

```bash
./venv/bin/python -m skills.engine.ocr.nemotron_mlx.radio_encoder --self-test
./venv/bin/python -m skills.engine.ocr.nemotron_mlx.runtime --self-test-decoder-step
./venv/bin/python -m skills.engine.ocr.nemotron_mlx.runtime --self-test-generation
```

結果：

- encoder：`ok=true`，shape `[1,3329,1024]`，mean/std 與 token0 前 16 維落在 golden tolerance 內。
- decoder-step：`ok=true`，logits shape `[1,1,52352]`，top1 與 logit values 通過；top-k 內部有近同分 token 排序 warning。
- generation：`ok=true`，token ids exact match golden，decoded text exact match golden。

關鍵修正：

- decoder 使用 MBart pre-norm 路徑。
- 本機 NemotronParseDecoder 沒有 learned position embedding，MLX runtime 不額外加位置向量。
- generation 必須套用 `generation_config.json` 的 `repetition_penalty=1.1`；未套用時會少產生 token `89`，導致 golden sequence mismatch。

### LIVE sidecar 驗證

啟動：

```bash
./venv/bin/python scripts/serve_nemotron_parse_omlx.py --host 127.0.0.1 --port 8094
```

Health：

```json
{"loaded":false,"ok":true,"weights":"/Users/ai/.omlx/models-vision/nemotron-parse-v1.2-mlx/bf16/model.safetensors"}
```

實際 HTTP parse：

```bash
curl -H 'Content-Type: application/json' \
  -d '{"image_path":"/Users/ai/Desktop/MAGI_v2/test_image.png","max_new_tokens":80}' \
  http://127.0.0.1:8094/parse
```

結果：

- `ok=true`
- `token_ids=[2,0,50004,50008,50001,50010,2]`
- `text="<predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>"`
- sidecar elapsed 約 27 秒（首次載入 + 推理）

Provider client LIVE：

```bash
MAGI_NEMOTRON_PARSE_ENABLE=1 ./venv/bin/python - <<'PY'
from skills.engine.ocr import nemotron_parse_provider
print(nemotron_parse_provider.run('/Users/ai/Desktop/MAGI_v2/test_image.png', timeout_sec=180))
PY
```

結果：`success=true`、`provider="nemotron_parse_mlx"`、`error=null`、duration 約 26 秒。

Consensus LIVE：

```bash
MAGI_NEMOTRON_PARSE_ENABLE=1 \
MAGI_TESSERACT_ENABLE=0 \
MAGI_APPLE_VISION_OCR_ENABLE=0 \
./venv/bin/python - <<'PY'
from skills.engine.ocr.consensus import run_consensus
print(run_consensus('/Users/ai/Desktop/MAGI_v2/test_image.png', task_type='legal', timeout_sec=180))
PY
```

結果：`success=true`；`provider_results["nemotron_parse_mlx"].success=true`；Tesseract / Apple Vision 因測試旗標關閉而失敗；輸出仍由 Nemotron Parse 透過 consensus 回傳。

## 尚未接入的產品 backlog

- 50 份繁中法院 PDF benchmark
- 正式 A/B rollout 閾值調校與品質儀表

## 安全狀態

- 啟用旗標仍應維持預設關閉：`MAGI_NEMOTRON_PARSE_ENABLE=0`。
- `skills.engine.ocr.nemotron_parse_provider.run()` 只有在 `MAGI_NEMOTRON_PARSE_ENABLE=1` 時才會呼叫 sidecar。
- `MAGI_NEMOTRON_PARSE_ENABLE=1` 時，Nemotron Parse 會接入 OCR consensus，並被 `/vision`、`/shortcut/ocr`、PDF OCR fallback 使用。

後續若要繼續，下一步是把繁中法院 PDF benchmark 當產品品質任務處理；不要再從桌面舊計劃直接啟動。
