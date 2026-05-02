# docx-editor

MAGI skill for applying anchored find-and-replace as Word tracked changes.

Ported from Mike OSS Legal Platform `docxTrackedChanges.ts`.

## Overview

This skill edits `.docx` files by inserting real Word tracked changes (`<w:ins>` / `<w:del>`) rather than doing direct text replacement. When a lawyer opens the output in Microsoft Word, they can review each change and Accept or Reject individually.

## Usage

### Apply tracked edits

```bash
python skills/docx-editor/action.py --task apply \
  --doc /path/to/document.docx \
  --edits '[{"find": "原文", "replace": "新文", "context_before": "前文", "context_after": "後文"}]'
```

Or use a JSON file:

```bash
python skills/docx-editor/action.py --task apply \
  --doc /path/to/document.docx \
  --edits /path/to/edits.json \
  --output /path/to/output.docx \
  --author "律師姓名"
```

### Extract text

```bash
python skills/docx-editor/action.py --task extract --doc /path/to/document.docx
```

### Find text

```bash
python skills/docx-editor/action.py --task find --doc /path/to/document.docx --query "搜尋文字"
```

### Self test

```bash
python skills/docx-editor/action.py --task self_test
```

## Edit format

Each edit in the `edits` JSON array:

```json
{
  "find": "要被替換的原文（必須在 anchor 範圍內 verbatim 出現）",
  "replace": "替換後的新文（空字串 = 純刪除）",
  "context_before": "find 前的 anchor 文字",
  "context_after": "find 後的 anchor 文字",
  "reason": "編輯理由（可選，律師可見）"
}
```

## Python API

```python
from skills.docx_editor.lib.tracked_edits import apply_tracked_edits, EditInput

edits = [
    EditInput(
        find="舊文字",
        replace="新文字",
        context_before="前後文",
        context_after="後文字",
        reason="修正錯誤",
    )
]

with open("document.docx", "rb") as f:
    docx_bytes = f.read()

result = apply_tracked_edits(docx_bytes, edits, author="MAGI")
# result.bytes: 改後的 docx bytes
# result.changes: 成功套用的 edits
# result.errors: 失敗的 edits

with open("document.edited.docx", "wb") as f:
    f.write(result.bytes)
```

## Phase 3: Chat-driven docx edit (DC/TG/LINE)

律師上傳 .docx 附件，訊息含觸發詞 → MAGI 用 LLM 產 edits → 套用 tracked changes → 回傳 edited.docx。

### 觸發詞
- `@MAGI 編輯 <指令>`
- `@MAGI 修改 <指令>`
- `編輯這份 <指令>`
- `修改這份 <指令>`
- `edit this <指令>`

### CLI
```bash
MAGI_DOCX_EDITOR_ALLOW_CLI=1 python skills/docx-editor/action.py \
  --task chat_edit \
  --doc /path/to/document.docx \
  --instruction "把所有『甲方』改成『原告』"
```

### Python API
```python
from skills.docx_editor.action import cmd_chat_edit

result = cmd_chat_edit(
    doc_path="/path/to/document.docx",
    instruction="把所有『甲方』改成『原告』",
    source="telegram",  # 安全閘門：必須含 user/telegram/discord/line
)
# result["ok"]: bool
# result["output_path"]: /tmp/magi_docx_edits/<timestamp>_<filename>
# result["changes_applied"]: int
# result["warnings"]: [str, ...]  # LLM 預檢警告
```

---

## Phase 4: 從零產文件 (generate_docx)

給 sections list → 產出 .docx。

### CLI
```bash
python skills/docx-editor/action.py --task generate \
  --title "結案報告書" \
  --sections '[
    {"heading":"事實摘要","level":1,"content":"本案當事人..."},
    {"heading":"法律意見","level":1,"content":"依民法第..."},
    {"level":2,"heading":"損害賠償","content":"...","table":{"headers":["項目","金額"],"rows":[["律師費","50000"]]}}
  ]'
```

### Python API
```python
from skills.docx_editor.lib.generator import generate_docx, GenerateDocxRequest, SectionSpec, TableSpec

req = GenerateDocxRequest(
    title="結案報告書",
    sections=[
        SectionSpec(heading="事實摘要", level=1, content="本案當事人..."),
        SectionSpec(heading="費用明細", level=1, table=TableSpec(
            headers=["項目", "金額"],
            rows=[["律師費", "50000"]],
        )),
    ],
)
docx_bytes = generate_docx(req)
```

---

## Phase 5: Citation 系統 (ensemble_inference)

LLM 引用文件時，使用 Mike citation 格式 `[N]` + `<CITATIONS>` JSON block。

### 啟用方式
```python
from skills.bridge.ensemble_inference import ensemble_chat_verified

result = ensemble_chat_verified(
    prompt="依據合約第三條，被告應如何負責？",
    enable_citation=True,  # 預設 False
)
# result.individual_results["citations"]: [{"ref": 1, "doc_id": "...", "page": "3", "quote": "..."}]
# result.individual_results["prose"]: 移除 <CITATIONS> 後的乾淨文字
```

### Citation 格式
```
依據[1]，被告應負損害賠償責任。

<CITATIONS>
[{"ref": 1, "doc_id": "doc-0", "page": "3", "quote": "被告應負損害賠償責任"}]
</CITATIONS>
```

---

## Safety guarantees

- Any failed edit (anchor not found / ambiguous) goes to `errors`, does not affect other edits
- If all edits fail, original bytes are returned unchanged
- Only `word/document.xml` is modified; all other ZIP entries are preserved byte-for-byte
- Word ID uniqueness is ensured by scanning existing `w:id` values first
