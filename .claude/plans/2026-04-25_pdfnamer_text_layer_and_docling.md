# PDFNAMER 強化設計報告 — Text-Layer Fast-Path + Docling 結構抽取

**日期**：2026-04-25
**設計者**：Opus 4.7
**執行者**：Sonnet 4.6
**目標檔案**：`/Users/ai/Desktop/MAGI_v2/skills/pdf-namer/action.py`
**約束**：PDFNAMER 核心穩定中（見 memory `pdfnamer_2026_0408_fix.md`），本次僅做**加法式**改動，不得改動既有 `generate_name_proposal()`、`_build_name_result()`、`_ocr_page_rapid()`、`_ocr_consensus()`、vision_parser 任何邏輯。

---

## 0. 任務拆分

兩個獨立 PR，分開測試、分開推送：

- **PR-A**：Text-Layer Fast-Path（CP 值最高，先做）
- **PR-B**：Docling 結構抽取（可選後處理階段，後做）

PR-A 必須先合併並穩定 1 週，才能開始 PR-B。

---

# PR-A：Text-Layer Fast-Path

## A.1 問題

目前 `_get_page_text()`（[action.py:1534](skills/pdf-namer/action.py:1534)）和 `extract_text()`（[action.py:2882](skills/pdf-namer/action.py:2882)）都只用「`page.get_text()` 結果 < 50 字 → 跑 OCR」這個事後判斷。問題：

1. 電子卷證（法源、司法院 PDF）本來就有完整 text layer，但首頁若是封面（短文字），仍會被誤判去跑 RapidOCR，浪費 2~5 秒/頁。
2. 沒有「整份 PDF 是否為純文字 PDF」的前置判斷，無法整份跳過 OCR pipeline。
3. RapidOCR 對電子文字 PDF 的辨識結果常含錯字，反而劣於 native text layer。

## A.2 設計

加一個**檔案級**前置檢測函式 `_pdf_has_reliable_text_layer(pdf_path)`，在 `generate_name_proposal()` 進入主流程前呼叫一次，把結果快取在 `doc` 物件上（`doc._magi_text_layer_quality`）。

下游所有 `len(text.strip()) < 50` 的判斷，新增一條短路：若該 PDF 已被標記為 reliable text layer，且當前 page 的 native text 不為空，就**不要**跑 OCR。

### A.2.1 新增函式

放在 `extract_text()` 上方（約 [action.py:2880](skills/pdf-namer/action.py:2880) 之前）：

```python
def _pdf_has_reliable_text_layer(doc) -> Tuple[bool, dict]:
    """
    判斷整份 PDF 是否擁有可靠的 text layer（電子文字 PDF）。

    判準（全部成立才回 True）：
      1. 取樣前 min(5, page_count) 頁
      2. 至少 60% 取樣頁的 page.get_text() 長度 >= 100 字
      3. 取樣頁的「文字 / 非空白字元比例」中位數 >= 0.85（排除大量亂碼）
      4. 至少一頁包含中文字（[一-鿿]），避免把純圖檔的零碎 metadata 誤判

    回傳：(is_reliable, metrics_dict)
    metrics_dict 包含 sampled_pages, native_chars_per_page, han_ratio, decision_reason，
    寫入 logger.info 以利 debug。
    """
```

實作要點：

- 用 `fitz`（已 import）；不要新增 dependency
- 只讀文字、不 render，純 CPU、毫秒級
- 對 `doc.needs_pass` 已認證的 doc 直接用
- 例外時回傳 `(False, {"decision_reason": "exception:..."})`，**絕不**讓這個函式 raise
- 把 `is_reliable` 結果掛到 `doc._magi_text_layer_quality = {"reliable": bool, **metrics}`，避免重複計算

### A.2.2 接入點 1：`_get_page_text()` 內部（[action.py:1534-1545](skills/pdf-namer/action.py:1534)）

修改後：

```python
def _get_page_text(page_idx):
    """Get text from a page, with RapidOCR fallback (or consensus when enabled)."""
    page = doc[page_idx]
    native = page.get_text() or ""
    text = native
    # ── A.2.2: 若整份 PDF 已被認定為 reliable text layer 且本頁有 native 文字，
    #          直接用 native，不要 OCR（省 2-5s/頁）
    tl = getattr(doc, "_magi_text_layer_quality", None)
    if tl and tl.get("reliable") and len(native.strip()) >= 20:
        return page, native, native
    if len(text.strip()) < 50:
        if _PDF_OCR_CONSENSUS:
            text = _ocr_consensus(page, pdf_path=pdf_path, page_idx=page_idx)
        elif HAS_OCR:
            text = _ocr_page_rapid(page)
    return page, native, text
```

**注意**：`tl.get("reliable")` 為 True 時門檻從 50 降到 20，因為純文字 PDF 的封面/短頁也是合法的 native text，OCR 反而劣化。

### A.2.3 接入點 2：`extract_text()` 內部（[action.py:2905-2910](skills/pdf-namer/action.py:2905)）

```python
        depth = min(max_pages, doc.page_count)
        # A.2.3: 前置判斷整份 PDF 文字品質
        is_reliable, _metrics = _pdf_has_reliable_text_layer(doc)
        doc._magi_text_layer_quality = {"reliable": is_reliable, **_metrics}
        for i in range(depth):
            page = doc[i]
            t = page.get_text()
            # 若整份可靠 → 不 OCR；否則沿用既有 < 50 字判準
            if not is_reliable and (i < ocr_pages) and len(t.strip()) < 50 and HAS_OCR:
                t = _ocr_page_rapid(page)
            text += t + "\n"
        return text, True
```

### A.2.4 接入點 3：`generate_name_proposal()` 開頭（[action.py:1524](skills/pdf-namer/action.py:1524) 附近）

`is_single_page = doc.page_count <= 2` 那行**之後**插入：

```python
    is_single_page = doc.page_count <= 2

    # ── A.2.4: 預先判斷文字品質，後續 _get_page_text 會讀此屬性 ──
    try:
        _is_reliable, _tl_metrics = _pdf_has_reliable_text_layer(doc)
        doc._magi_text_layer_quality = {"reliable": _is_reliable, **_tl_metrics}
        if _is_reliable:
            logger.info(f"[fast-path] {os.path.basename(pdf_path)} 偵測為文字 PDF，跳過 OCR")
    except Exception:
        doc._magi_text_layer_quality = {"reliable": False, "decision_reason": "exception"}
```

### A.2.5 接入點 4：[action.py:1551-1554](skills/pdf-namer/action.py:1551)（envelope 偵測那段）

```python
        p0 = doc[0]
        p0_text = p0.get_text() or ""
        tl = getattr(doc, "_magi_text_layer_quality", None)
        skip_ocr = tl and tl.get("reliable") and len(p0_text.strip()) >= 20
        if len(p0_text.strip()) < 50 and HAS_OCR and not skip_ocr:
            p0_text = _ocr_page_rapid(p0)
```

同樣 [action.py:1577-1578](skills/pdf-namer/action.py:1577) 的 `p1_text` 判斷也加 `skip_ocr` 短路。

### A.2.6 環境變數（kill switch）

新增 `MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH`（預設 `1`）。在 `_pdf_has_reliable_text_layer()` 開頭：

```python
    if os.environ.get("MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH", "1") not in ("1", "true", "True"):
        return False, {"decision_reason": "disabled_by_env"}
```

出事時可一鍵關閉：`launchctl setenv MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH 0`。

## A.3 測試計畫

新增 `/Users/ai/Desktop/MAGI_v2/tests/test_pdf_namer_text_layer.py`：

```python
import pytest
import fitz
from skills.pdf_namer.action import _pdf_has_reliable_text_layer, generate_name_proposal

# 測試素材（已存在於 repo 的 fixtures 或 筆錄下載/）
TEXT_PDF = "/Users/ai/Desktop/MAGI_v2/筆錄下載/14095029.04I.pdf"  # 換成已知為文字 PDF 的範例
SCAN_PDF = "/Users/ai/Desktop/MAGI_v2/筆錄下載/13140642.008.pdf"  # 換成已知為掃描檔的範例

def test_text_pdf_detected_as_reliable():
    doc = fitz.open(TEXT_PDF)
    ok, m = _pdf_has_reliable_text_layer(doc)
    assert ok, f"應判為文字 PDF: {m}"

def test_scan_pdf_detected_as_unreliable():
    doc = fitz.open(SCAN_PDF)
    ok, m = _pdf_has_reliable_text_layer(doc)
    assert not ok, f"應判為掃描檔: {m}"

def test_naming_still_works_with_fastpath(monkeypatch):
    # 開 fastpath 跑命名流程，結果應與關掉時一致或更乾淨
    monkeypatch.setenv("MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH", "1")
    name_on = generate_name_proposal(TEXT_PDF, return_structured=True)
    monkeypatch.setenv("MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH", "0")
    name_off = generate_name_proposal(TEXT_PDF, return_structured=True)
    # 關鍵欄位一致
    for k in ("date", "court", "case_number", "doc_type"):
        assert name_on.get(k) == name_off.get(k), f"{k}: on={name_on.get(k)} off={name_off.get(k)}"

def test_speed_improvement():
    import time
    monkeypatch_env("MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH", "1")
    t0 = time.time(); generate_name_proposal(TEXT_PDF); on = time.time() - t0
    monkeypatch_env("MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH", "0")
    t0 = time.time(); generate_name_proposal(TEXT_PDF); off = time.time() - t0
    assert on < off * 0.7, f"預期至少快 30%: on={on:.2f}s off={off:.2f}s"
```

**Sonnet 執行步驟**：
1. 從 `筆錄下載/` 中挑兩個檔案，一個確認是文字 PDF，一個是掃描檔，更新測試常數
2. 跑 `pytest tests/test_pdf_namer_text_layer.py -v`
3. **必過 4 條測試才可 commit**（依 memory `feedback_test_before_push.md`）

## A.4 灰度與驗證

1. 寫完先在本機對 `筆錄下載/` 全部 PDF 跑一次 `generate_name_proposal()`，對照 fastpath on/off 兩版輸出，差異 > 5% 就停下重檢
2. 開啟一週後，從 `.runtime/` log 撈 `[fast-path]` 命中率，目標 > 30%
3. 對照同期的 `nightly_train.py` 訓練資料，確認命名正確率無下降

## A.5 回滾

單一 env var 關閉。若程式碼層面有問題，git revert PR-A 該 commit 即可，無 schema/資料變更。

---

# PR-B：Docling 結構抽取（可選後階段）

## B.1 目標

把判決書、卷證索引這類**結構固定**文件，OCR/text 之後再過一次 docling，產出 JSON layout（標題層級、段落、表格、頁碼），存成 PDF 旁邊的 `.layout.json` sidecar，供下游（證據能力表、案件索引、bilingual-docx）取用。

**本 PR 不改變 PDFNAMER 命名結果**，只多寫一個 sidecar。

## B.2 範圍

- 不引入 docling 至 `generate_name_proposal()` 主流程
- 新建 `skills/pdf-namer/layout_extractor.py` 一個獨立 module
- 新增 cron 排程「每晚對最近 24h 命名成功的 PDF 補跑 layout」

## B.3 依賴

```toml
# pyproject.toml 新增
"docling>=2.0",  # 確認 Apple Silicon wheel 可用
```

**Sonnet 執行步驟**：
1. `pip install docling` 試裝；若拉超過 5 分鐘或裝不起來，立刻停下回報
2. 跑 `python -c "from docling.document_converter import DocumentConverter; DocumentConverter().convert('某PDF路徑')"` 確認可以動
3. 若 OK 才寫進 pyproject.toml

## B.4 實作

### B.4.1 新檔 `skills/pdf-namer/layout_extractor.py`

```python
"""
Docling-based layout sidecar generator.
產出 <pdf>.layout.json，內容為 docling DoclingDocument 的 JSON 序列化。
不影響 PDFNAMER 命名邏輯，純後處理。
"""
import json, os, logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pdf-namer.layout")

_CONVERTER = None

def _get_converter():
    global _CONVERTER
    if _CONVERTER is None:
        from docling.document_converter import DocumentConverter
        _CONVERTER = DocumentConverter()
    return _CONVERTER

def generate_layout_sidecar(pdf_path: str, force: bool = False) -> Optional[str]:
    """
    對 pdf_path 跑 docling，產出 <pdf>.layout.json。
    已存在則跳過（除非 force=True）。
    回傳 sidecar path 或 None。
    """
    if os.environ.get("MAGI_PDF_NAMER_DOCLING_ENABLED", "0") not in ("1", "true"):
        return None
    sidecar = pdf_path + ".layout.json"
    if os.path.exists(sidecar) and not force:
        return sidecar
    try:
        result = _get_converter().convert(pdf_path)
        doc_json = result.document.export_to_dict()
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(doc_json, f, ensure_ascii=False, indent=2)
        logger.info(f"[docling] wrote {sidecar}")
        return sidecar
    except Exception as e:
        logger.warning(f"[docling] failed for {pdf_path}: {e}")
        return None
```

### B.4.2 Cron 入口 `skills/pdf-namer/nightly_layout.py`

```python
"""
每晚對最近 24h 命名成功的 PDF 補跑 docling layout。
從 .runtime/pdf_namer_history.jsonl 取近期成功命名清單。
"""
import os, json, time, glob
from layout_extractor import generate_layout_sidecar

LOOKBACK_SEC = 86400
HISTORY = "/Users/ai/Desktop/MAGI_v2/.runtime/pdf_namer_history.jsonl"

def main():
    if not os.path.exists(HISTORY):
        return
    cutoff = time.time() - LOOKBACK_SEC
    seen = set()
    with open(HISTORY) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            p = rec.get("renamed_path")
            if not p or p in seen or not os.path.exists(p):
                continue
            seen.add(p)
            generate_layout_sidecar(p)

if __name__ == "__main__":
    main()
```

**注意**：實際 history 檔路徑/格式請 Sonnet 在 [action.py](skills/pdf-namer/action.py) 用 `grep history` 找，可能叫別的名字（`pdf_namer_log.jsonl` / `rename_log.jsonl` 之類），用實際存在的那個。

### B.4.3 cron 註冊

`/Users/ai/Desktop/MAGI_v2/cron_jobs.json` 新增 entry：

```json
{
  "id": "pdfnamer_docling_layout",
  "schedule": "30 2 * * *",
  "command": "python /Users/ai/Desktop/MAGI_v2/skills/pdf-namer/nightly_layout.py",
  "timeout_sec": 1800,
  "description": "夜間 docling layout sidecar 補跑（最近 24h）"
}
```

加進 `_LONG_JOBS` 清單（依 memory 上次教訓 `69db59a fix(cron)`）。

### B.4.4 預設關閉

env `MAGI_PDF_NAMER_DOCLING_ENABLED` 預設 `0`。要正式啟用前先手動跑一週看結果。

## B.5 測試

```python
def test_docling_sidecar_generated(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_PDF_NAMER_DOCLING_ENABLED", "1")
    from skills.pdf_namer.layout_extractor import generate_layout_sidecar
    out = generate_layout_sidecar("/path/to/test.pdf")
    assert out and os.path.exists(out)
    with open(out) as f:
        data = json.load(f)
    assert "texts" in data or "body" in data  # 視 docling export 結構

def test_docling_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAGI_PDF_NAMER_DOCLING_ENABLED", raising=False)
    from skills.pdf_namer.layout_extractor import generate_layout_sidecar
    assert generate_layout_sidecar("/path/to/test.pdf") is None
```

## B.6 回滾

- env var 關閉即停
- sidecar 是檔案，不影響任何主流程；不滿意可整批 `find . -name "*.layout.json" -delete`
- cron 從 `cron_jobs.json` 移除即可

---

# 共同事項

## 推送前檢查（依 memory）

- [ ] **跑測試通過才 commit**（`feedback_test_before_push.md`）
- [ ] **PR-A 合併後檢查 README** 是否需更新「PDFNAMER 已支援文字 PDF fast-path」說明（`feedback_readme_check_on_push.md`）
- [ ] **改完直接推送**，不等使用者提醒（`feedback_push_after_changes.md`）
- [ ] **連帶排查同類 entry point**：`extract_text_quick()`（[action.py:2916](skills/pdf-namer/action.py:2916)）若也有 OCR fallback，是否需要同步加 fast-path（`feedback_notify_bug_cross_audit.md`）

## Commit 訊息模板

PR-A：
```
feat(pdf-namer): text-layer fast-path 跳過 OCR

電子文字 PDF（法源/司法院下載）原本因封面短文字觸發 OCR fallback，
浪費 2-5s/頁且引入錯字。新增 _pdf_has_reliable_text_layer() 前置
檢測，整份判定為可靠 text layer 時所有頁面跳過 RapidOCR。

- env: MAGI_PDF_NAMER_TEXT_LAYER_FASTPATH（預設 1，可關）
- 純加法式改動，命名輸出與既有版本相容
- 測試：tests/test_pdf_namer_text_layer.py 4 案
```

PR-B：
```
feat(pdf-namer): docling layout sidecar（夜間補跑，預設關閉）

新增獨立 module layout_extractor.py + cron pdfnamer_docling_layout，
對命名成功的 PDF 補跑 docling 產出 .layout.json，供下游結構化使用。
不改變既有命名邏輯。

- env: MAGI_PDF_NAMER_DOCLING_ENABLED（預設 0）
- cron: 02:30 每日，timeout 1800s
- dep: docling>=2.0
```

---

# Sonnet 執行順序

1. 讀本文件全文
2. 開 PR-A，依 A.2.1 → A.2.6 順序改 [action.py](skills/pdf-namer/action.py)
3. 寫 A.3 測試，挑兩個實際 PDF 當 fixture
4. 跑測試 → 全綠 → commit → push
5. 更新 README（若需要）→ 新 commit → push
6. **停下回報** PR-A 完成情形與一週觀察計畫，等 Opus 驗證
7. PR-A 穩定 1 週後，由 Opus 再啟動 PR-B
