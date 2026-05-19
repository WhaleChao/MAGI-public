# 2026-04-25 — pytest 模組污染 + 筆錄 subprocess NameError 修復

## Bug 1：pytest 全套測試 `test_nim_failure_does_not_retry_second_time` 失敗

### 根因
`test_summarize_pipeline.py` / `test_translate_pipeline.py` import handler 時觸發 `load_dotenv()`，將 `.env` 的生產值注入 `os.environ`：

```
MAGI_HEAVY_STRICT_NIM=1
MAGI_HEAVY_STRICT_NIM_RETRIES=6
NVIDIA_NIM_ENABLE=1
```

後續 `test_nim_failure_does_not_retry_second_time` 的 `setup_method` 只重設 `NVIDIA_NIM_ENABLE`，未清除 strict 模式 → `inference_gateway._chat_inner()` 的 strict NIM retry loop 從預期的 1 次膨脹為 7 次（`_max_attempts = _strict_retries + 1 = 7`）→ 指數退避合計 ~182 秒後超時 → 測試失敗。

### 修法
**`tests/conftest.py`** — `mock_env_vars` autouse fixture 加三個安全預設：

```python
"NVIDIA_NIM_ENABLE": "0",
"MAGI_HEAVY_STRICT_NIM": "0",
"MAGI_HEAVY_STRICT_NIM_RETRIES": "0",
```

**`tests/test_laf_vision_ocr_consensus.py`** — module reload 清理從裸 `del sys.modules[key]` 改為 `monkeypatch.delitem(sys.modules, key, raising=False)`，防止 teardown 未還原造成跨 test 汙染。

### 結果
全套 pytest **1965 passed / 0 failed**（修前：1 failure + 182s hang）

### 守則
- `conftest.py` 的 `mock_env_vars` 是全套測試的 env var 安全閥；任何 `.env` 有生產值但測試應該關閉的 flag，都要在此加預設 `"0"`
- 需要 opt-in NIM/strict 行為的測試，在自己的 `setup_method` 或 test body 用 `monkeypatch.setenv(...)` 覆蓋
- module reload 清理一律用 `monkeypatch.delitem()`，不用裸 `del`

### 驗收層級
測試。不影響長駐 runtime，無需 magi restart。

---

## Bug 2：筆錄同步 `name 'subprocess' is not defined`

### 表象
runtime log：`❌ 筆錄同步失敗: name 'subprocess' is not defined`

### 根因
`skills/transcript-downloader/action.py` 的 `_run_md5_scan_subprocess()`（line 304、312）使用 `subprocess.run()` / `subprocess.TimeoutExpired`，但檔案頂部 import 區塊**未** import `subprocess` module。

`MAGI_TRANSCRIPT_SYNC_MD5_SCAN_MODE` 預設為 `"subprocess"`（line 80），cmd_sync 路徑遇到 MD5 scan 階段就 `NameError`，外層 except 捕到後通知失敗。

### 修法
`action.py` 頂部 import 區塊加 `import subprocess`（依字母順序插入 `import re` 與 `import sys` 之間）：

```python
import argparse
import json
import logging
import os
import re
import subprocess  # ← 新增
import sys
import traceback
```

### 驗收
- `python3 -m py_compile skills/transcript-downloader/action.py` 通過
- `python3 skills/transcript-downloader/action.py --task self_test` 正常回傳（剩 ezlawyer SSL 已知警告，非本 bug）

### 守則
- cmd_sync 走 subprocess 模式跑 MD5 scan 是預設行為；任何在 `_run_md5_scan_subprocess()` 內部的擴充必須確認 import 完整
- 此 bug 是純 NameError，無 unit test 覆蓋（測試環境會走不同路徑）；以後加類似 helper 必須在頂部立即 import 而非延後

### 驗收層級
測試。不影響長駐 runtime；cmd_sync 由 cron 觸發，下次排程會自動使用修正後的程式碼。

---

## 修改檔案清單

| 檔案 | 變更 |
|------|------|
| `tests/conftest.py` | `mock_env_vars` 新增三個 NIM 相關預設值 |
| `tests/test_laf_vision_ocr_consensus.py` | `del sys.modules[key]` → `monkeypatch.delitem(...)` |
| `skills/transcript-downloader/action.py` | 頂部 import 補 `import subprocess` |
