---
name: translator
description: 完整翻譯（預設不摘要）並在內容過長時自動輸出 TXT 或 DOCX 表格到 /static/exports，方便透過 LINE/DC 傳送連結或檔案。也支援翻譯網頁 URL（含分頁/區塊）。
---

# translator

## 目的

- 使用 CASPER/MELCHIOR 本地分散式推理做翻譯。
- **預設只做完整翻譯、不摘要**（除非你明確要求摘要）。
- 內容太長時，自動輸出 TXT 到 `MAGI/static/exports` 並回傳路徑與可下載連結（若 `MAGI_PUBLIC_BASE_URL` 已設定）。
- **支援 DOCX 雙語對照表格輸出**：設定 `export_format: "docx"` 即可產生漂亮的原文/翻譯並排表格 `.docx` 檔。

## 使用方式（CLI / tools run）

- `help`：顯示可用指令
- `self_test`：冒煙測試
- `translate {json}`：翻譯文字或 URL

### translate payload

- `text` (必填)：可為一般文字或包含 URL 的句子（例如 `請翻譯這個網頁：https://...`）
- `target_lang`：預設 `繁體中文`
- `source_lang`：預設 `auto`
- `mode`：預設 `full`（完整翻譯、不摘要）。可用 `auto` 讓系統自行判斷。
- `export`：`auto|1|0`，預設 `auto`（內容太長就輸出 TXT）
- `export_format`：`docx|txt|none`，**預設 `docx`**（自動產生雙語對照表格 .docx）。設為 `txt` 退回純文字，`none` 不輸出檔案
- `docx_title`：DOCX 文件標題（僅 `export_format=docx` 時有效）
- `docx_subtitle`：DOCX 副標題（僅 `export_format=docx` 時有效）
- `export_prefix`：檔名前綴（預設 `translate`）
- `max_inline_chars`：回傳 inline 文字的上限（預設 3000；超過就只回傳 preview + 連結）

### DOCX 輸出範例

```json
{
  "text": "The court finds that...",
  "target_lang": "繁體中文",
  "export_format": "docx",
  "docx_title": "科孚海峽案（賠償金額評估）",
  "docx_subtitle": "ICJ Judgment of December 15th, 1949"
}
```

回傳結果會多出：
- `docx_exported`: true
- `docx_path`: 檔案路徑
- `docx_filename`: 檔名
- `docx_url`: 下載連結（若有設定 public base URL）

## 呼叫格式
觸發詞：翻譯、translate
參數：text=要翻譯的文字, target=目標語言(預設繁體中文), file=檔案路徑(選填)

## 呼叫範例
使用者：翻譯這段英文：The court held that...
→ 翻譯 text=The court held that... target=繁體中文

使用者：翻譯這個檔案 /tmp/doc.pdf
→ 翻譯 file=/tmp/doc.pdf

---

## APE 法律翻譯升級（Apple Translation + LLM Post-Editing）

### 概覽

短到中長度法律文本（zh↔en）可啟用 APE（Automatic Post-Editing）路徑，品質顯著高於純 Google GTX：

```
zh 原文
  │
  ▼  Apple Translation（離線，~50ms）
baseline 機器初譯
  │
  ▼  LLM post-edit（E4B 日 / 26B 夜，~2-5s）
     Tier 1 MOJ 雙語 + Tier 2 學理詞條作為 glossary 注入
polished 譯文
  │
  ▼  雙保險 validator
     ├── 長度差異 ≤35%（短句寬鬆）
     ├── 數字 / 案號 / 當事人名保留
     ├── 繁體中文目標時無簡體字
     └── 無 LLM 重複崩潰
  │
  ├── valid → 回傳 polished（provider=apple_translation_ape）
  └── invalid → fallback baseline（provider=apple_translation_baseline, degraded=True）
```

### 啟用方式

```bash
# 單次翻譯
MAGI_TRANSLATOR_APE=1 skills/translator/action.py --task 'translate {"text":"原告訴之聲明...","target_lang":"en"}'

# 長文分段 APE（每段 ≤1800 字）
MAGI_TRANSLATOR_APE_CHUNKS=1  # 預設開啟，在 MAGI_TRANSLATOR_APE=1 時生效
```

### 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `MAGI_TRANSLATOR_APE` | `0` | 主路由 APE 開關（opt-in） |
| `MAGI_TRANSLATOR_APE_MAX_CHARS` | `1200` | 主路由最大字元數 |
| `MAGI_TRANSLATOR_APE_CHUNKS` | `1` | 長文分段 APE |
| `MAGI_TRANSLATOR_APE_CHUNK_MAX_CHARS` | `1800` | 每段最大字元數 |
| `MAGI_APPLE_TRANSLATION_TIMEOUT_SEC` | `10.0` | Apple sidecar timeout |

### 回傳欄位（APE 路徑）

```json
{
  "success": true,
  "text": "Prayer for relief: The defendant shall pay NT$200,000.",
  "provider": "apple_translation_ape",
  "baseline": "Plaintiff's statement: The defendant shall pay NT$200,000.",
  "validator": { "valid": true, "reasons": [], "stats": { ... } },
  "degraded": false,
  "elapsed_ms": 3200
}
```

### 前置條件

1. macOS 15+ Sequoia（Apple Translation framework 需求）
2. 系統設定 → 語言與地區 → 翻譯語言 → 下載「英文」「繁體中文」
3. `skills/engine/apple_translation/_sidecar/magi_translator_sidecar` 已編譯（預編譯 arm64 已包含）

### 夜間回歸

`cron_jobs.json` 中 `job_translator_ape_regression`（每日 03:15）執行
`scripts/ops/benchmark_translator_ape.py`，結果寫至 `static/translator_ape_latest.json`。
APE 術語命中率低於 baseline 或退化率 > 50% 時自動發 DC 告警。
