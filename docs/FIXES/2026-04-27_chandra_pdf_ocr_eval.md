# Chandra PDF OCR 評估與接線紀錄

## 結論

- Chandra OCR 2 可改善 PDF 命名/書籤的上游文字品質，尤其是掃描件、表格、手寫、多欄與低品質 OCR，但它本身不是命名器或書籤器。
- 目前不適合直接取代 MAGI 既有 macOS Vision / RapidOCR / GLM fallback，因為 Chandra 預設需要 vLLM GPU server，HuggingFace 後端會下載大型模型，且模型權重有 OpenRAIL-M modified 使用限制。
- 已安裝到隔離環境 `/tmp/magi_chandra_venv`，避免污染 MAGI 主 venv。
- 已新增 MAGI optional provider，預設關閉；只有既有 OCR 低品質或空白時，且使用者明確啟用與接受模型授權，才會嘗試 Chandra。

## 變更範圍

- `skills/engine/ocr/chandra_provider.py`
  - 解析 Chandra CLI、檢查 feature flag、模型授權確認、vLLM server、HF 後端防呆。
  - 透過 subprocess 呼叫 Chandra CLI，讀取 markdown output。
  - 所有不可用狀態回傳 structured failure，不拋例外。
- `skills/pdf-namer/action.py`
  - 新增 `_chandra_ocr_page()` 與 `_prefer_chandra_if_better()`。
  - 接入 `_ocr_consensus()`、`_vision_analyze_for_naming()`、`batch_ocr_pages()` 的低品質 OCR fallback。
  - 預設 `MAGI_CHANDRA_OCR_ENABLE=0`，不影響正式路徑。
- `skills/engine/ocr/__init__.py`
  - 記錄 Chandra opt-in 邊界。
- Tests
  - `tests/test_chandra_provider.py`
  - `tests/test_pdf_namer_chandra_fallback.py`

## Feature Flags

- `MAGI_CHANDRA_OCR_ENABLE=1`：啟用 Chandra fallback。
- `MAGI_CHANDRA_ACCEPT_MODEL_LICENSE=1`：允許實際模型推理前必填。
- `MAGI_CHANDRA_CLI=/tmp/magi_chandra_venv/bin/chandra`：CLI 路徑；預設也會找此隔離安裝。
- `MAGI_CHANDRA_OCR_METHOD=vllm|hf`：預設 vLLM。
- `MAGI_CHANDRA_VLLM_API_BASE=http://127.0.0.1:8000/v1`：vLLM OpenAI-compatible endpoint。
- `MAGI_CHANDRA_ALLOW_HF=1`：若要用 HF 後端，必須額外明確開啟，避免誤下載大型權重。
- `MAGI_CHANDRA_OCR_MIN_SCORE=0.45`：低於此 OCR 品質分數才嘗試 Chandra。

## 驗證

- 安裝：`python3 -m venv /tmp/magi_chandra_venv && /tmp/magi_chandra_venv/bin/python -m pip install /tmp/chandra_eval`
- CLI：`/tmp/magi_chandra_venv/bin/chandra --help` 成功。
- Import：`from chandra.input import load_pdf_images`; `from chandra.model import InferenceManager` 成功。
- Provider live probe：
  - 未啟用：安全回 `MAGI_CHANDRA_OCR_ENABLE is not enabled`。
  - 啟用但未接受模型授權：安全回 `MAGI_CHANDRA_ACCEPT_MODEL_LICENSE is required before model inference`。
  - 已接受授權但 vLLM 未啟動：安全回 `vLLM unavailable`。
- pdf-namer live smoke：
  - Chandra enabled / license missing：`self_test` success。
  - Chandra enabled / license accepted / vLLM missing：`self_test` success。
- Regression:
  - `48 passed`：Chandra + pdf-namer + skill contract subset。
  - `61 passed`：PDF namer/bookmarker focused regression。
  - `benchmark_pdf_namer.py`: PASS, format/quality/overall 100% on capped sample。
  - `benchmark_pdf_bookmarker.py`: PASS, bookmark recall 100%, label match 100% on benchmark run。
  - `audit_operational_hardening.py`: cron parse/collision 0。
  - `/health`: `status=operational`, `operational_health.ok=true`。

## 限制

- 本機目前沒有 `127.0.0.1:8000/v1` vLLM Chandra server，因此沒有跑到真正 Chandra model inference。
- Chandra vLLM README 建議 NVIDIA GPU/H100 等級部署；Mac 本機只適合作為 client 或 HF 實驗環境。
- 模型權重使用 modified OpenRAIL-M；未來若要正式用於事務所生產，需先確認使用條件與商用/競品限制。
