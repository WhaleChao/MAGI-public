---
name: docx-editor
description: Edit Word documents with anchored replacements, tracked changes, text extraction, search, chat-driven edits, and document generation.
compatibility: Requires python-docx and lxml.
metadata:
  role: document
  entry: skills/docx-editor/action.py
---

# docx-editor

`docx-editor` edits `.docx` files while preserving lawyer-reviewable tracked changes. It can extract text, find anchored text, apply replacement plans, generate simple Word documents, and run chat-driven edits when the calling channel is explicitly allowed.

## Commands

```bash
python skills/docx-editor/action.py --task self_test
python skills/docx-editor/action.py --task extract --doc /path/to/document.docx
python skills/docx-editor/action.py --task find --doc /path/to/document.docx --query "搜尋文字"
python skills/docx-editor/action.py --task apply --doc /path/to/document.docx --edits /path/to/edits.json --output /path/to/output.docx
```

## Edit Contract

Each edit should include `find`, `replace`, and enough `context_before` / `context_after` text to anchor the change safely. The output document stores insertions and deletions as Word tracked changes so a lawyer can accept or reject them in Microsoft Word.

## Guardrails

- Chat-driven edits require an approved source such as Telegram, Discord, LINE, or an explicit CLI allow flag.
- Ambiguous anchors should be reported as warnings instead of silently replacing the wrong paragraph.
