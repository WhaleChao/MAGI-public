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

## Safety guarantees

- Any failed edit (anchor not found / ambiguous) goes to `errors`, does not affect other edits
- If all edits fail, original bytes are returned unchanged
- Only `word/document.xml` is modified; all other ZIP entries are preserved byte-for-byte
- Word ID uniqueness is ensured by scanning existing `w:id` values first
