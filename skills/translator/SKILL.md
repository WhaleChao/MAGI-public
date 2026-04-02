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
