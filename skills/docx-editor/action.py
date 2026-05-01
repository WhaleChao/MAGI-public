"""
docx-editor — DOCX tracked changes editor

Port of Mike OSS Legal Platform docxTrackedChanges.ts to Python.
Applies anchored find-and-replace as Word tracked changes (<w:ins>/<w:del>).

Usage:
    python action.py --task apply --doc <path> --edits <json_path_or_inline>
    python action.py --task extract --doc <path>
    python action.py --task find --doc <path> --query <text>
    python action.py --task self_test
"""

import argparse
import json
import os
import sys
import traceback
from typing import Dict, List, Optional

# Ensure lib is importable
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SKILL_DIR)

from lib.tracked_edits import apply_tracked_edits, EditInput
from lib.docx_io import extract_paragraph_text
from lib.anchor_matcher import find_unique_anchor


def cmd_apply(
    doc_path: str,
    edits: List[Dict],
    output_path: str = "",
    author: str = "MAGI",
) -> Dict:
    """套用 edits，輸出檔到 output_path（預設 <doc>.edited.docx）。"""
    if not output_path:
        base, _ = os.path.splitext(doc_path)
        output_path = base + ".edited.docx"

    with open(doc_path, "rb") as f:
        docx_bytes = f.read()

    edit_inputs = []
    for e in edits:
        edit_inputs.append(EditInput(
            find=e.get("find", ""),
            replace=e.get("replace", ""),
            context_before=e.get("context_before", ""),
            context_after=e.get("context_after", ""),
            reason=e.get("reason"),
        ))

    result = apply_tracked_edits(docx_bytes, edit_inputs, author=author)

    success_count = len(result.changes)
    error_count = len(result.errors)

    # Only write output if at least one edit succeeded
    if success_count > 0:
        with open(output_path, "wb") as f:
            f.write(result.bytes)

    return {
        "ok": error_count == 0,
        "output_path": output_path if success_count > 0 else None,
        "changes": [
            {
                "id": c.id,
                "del_id": c.del_id,
                "ins_id": c.ins_id,
                "deleted_text": c.deleted_text,
                "inserted_text": c.inserted_text,
                "context_before": c.context_before,
                "context_after": c.context_after,
                "reason": c.reason,
            }
            for c in result.changes
        ],
        "errors": [
            {"index": e.index, "reason": e.reason}
            for e in result.errors
        ],
        "success_count": success_count,
        "error_count": error_count,
    }


def cmd_extract(doc_path: str) -> Dict:
    """抽出整份 docx 純文字（給 LLM 讀）。回傳 {text: str, paragraph_count: int}"""
    import zipfile
    from lxml import etree

    with open(doc_path, "rb") as f:
        docx_bytes = f.read()

    with zipfile.ZipFile.__new__(zipfile.ZipFile) as _:
        pass  # just import check

    import io
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        with zf.open("word/document.xml") as xf:
            root = etree.parse(xf).getroot()

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs = []

    def collect(el):
        for child in el:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p":
                paragraphs.append(extract_paragraph_text(child))
            elif tag in ("tbl", "tr", "tc", "sdt", "sdtContent"):
                collect(child)

    body = root.find(f"{{{W}}}body")
    collect(body if body is not None else root)

    return {
        "text": "\n".join(paragraphs),
        "paragraph_count": len(paragraphs),
    }


def cmd_find(
    doc_path: str,
    query: str,
    max_results: int = 20,
    context_chars: int = 80,
) -> Dict:
    """Ctrl+F 等價，回傳 {matches: [{paragraph_index, offset, before, match, after}, ...]}"""
    import io
    import zipfile
    from lxml import etree
    from lib.docx_io import extract_paragraph_text

    with open(doc_path, "rb") as f:
        docx_bytes = f.read()

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        with zf.open("word/document.xml") as xf:
            root = etree.parse(xf).getroot()

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs = []

    def collect(el):
        for child in el:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p":
                paragraphs.append(extract_paragraph_text(child))
            elif tag in ("tbl", "tr", "tc", "sdt", "sdtContent"):
                collect(child)

    body2 = root.find(f"{{{W}}}body")
    collect(body2 if body2 is not None else root)

    matches = []
    for para_idx, para_text in enumerate(paragraphs):
        offset = 0
        while offset <= len(para_text) - len(query):
            pos = para_text.find(query, offset)
            if pos < 0:
                break
            before = para_text[max(0, pos - context_chars):pos]
            after = para_text[pos + len(query):pos + len(query) + context_chars]
            matches.append({
                "paragraph_index": para_idx,
                "offset": pos,
                "before": before,
                "match": para_text[pos:pos + len(query)],
                "after": after,
            })
            if len(matches) >= max_results:
                break
            offset = pos + 1
        if len(matches) >= max_results:
            break

    return {"matches": matches, "total": len(matches)}


def cmd_self_test() -> Dict:
    """讀 tests/fixtures/docx_editor/simple.docx → 套一個 edit → 寫到 /tmp → 驗回讀。"""
    import tempfile
    import io
    import zipfile
    from lxml import etree
    from lib.tracked_edits import apply_tracked_edits, EditInput
    from lib.docx_io import extract_paragraph_text

    errors = []

    # Find fixture path
    skill_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(skill_dir))
    fixture_path = os.path.join(repo_root, "tests", "fixtures", "docx_editor", "simple.docx")

    if not os.path.exists(fixture_path):
        return {"ok": False, "errors": [f"Fixture not found: {fixture_path}"]}

    try:
        with open(fixture_path, "rb") as f:
            docx_bytes = f.read()

        edits = [EditInput(
            find="Hello World",
            replace="Hello MAGI",
            context_before="",
            context_after="",
        )]

        result = apply_tracked_edits(docx_bytes, edits, author="MAGI")

        if result.errors:
            errors.append(f"apply_tracked_edits errors: {result.errors}")

        if not result.changes:
            errors.append("No changes were applied")

        # Write to temp and read back
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
            tf.write(result.bytes)
            tmp_path = tf.name

        # Verify the output is readable
        with zipfile.ZipFile(io.BytesIO(result.bytes)) as zf:
            with zf.open("word/document.xml") as xf:
                root = etree.parse(xf).getroot()

        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        # Check del/ins elements exist
        xml_str = result.bytes  # it's bytes
        # Read the document.xml string
        with zipfile.ZipFile(io.BytesIO(result.bytes)) as zf:
            xml_content = zf.read("word/document.xml").decode("utf-8")

        has_del = "w:del" in xml_content
        has_ins = "w:ins" in xml_content

        if not has_del:
            errors.append("Output docx missing <w:del> element")
        if not has_ins:
            errors.append("Output docx missing <w:ins> element")

        os.unlink(tmp_path)

    except Exception as e:
        errors.append(f"Exception: {e}\n{traceback.format_exc()}")

    return {"ok": len(errors) == 0, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="docx-editor skill")
    parser.add_argument("--task", required=True, choices=["apply", "extract", "find", "self_test"])
    parser.add_argument("--doc", help="Path to .docx file")
    parser.add_argument("--edits", help="JSON string or path to JSON file with edits list")
    parser.add_argument("--output", help="Output path for apply task")
    parser.add_argument("--author", default="MAGI", help="Author name for tracked changes")
    parser.add_argument("--query", help="Text to search for (find task)")
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--context-chars", type=int, default=80)

    args = parser.parse_args()

    try:
        if args.task == "apply":
            if not args.doc:
                print(json.dumps({"ok": False, "error": "--doc required"}))
                sys.exit(1)
            if not args.edits:
                print(json.dumps({"ok": False, "error": "--edits required"}))
                sys.exit(1)

            # edits can be inline JSON or a file path
            edits_raw = args.edits
            if os.path.isfile(edits_raw):
                with open(edits_raw) as f:
                    edits = json.load(f)
            else:
                edits = json.loads(edits_raw)

            result = cmd_apply(args.doc, edits, output_path=args.output or "", author=args.author)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.task == "extract":
            if not args.doc:
                print(json.dumps({"ok": False, "error": "--doc required"}))
                sys.exit(1)
            result = cmd_extract(args.doc)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.task == "find":
            if not args.doc:
                print(json.dumps({"ok": False, "error": "--doc required"}))
                sys.exit(1)
            if not args.query:
                print(json.dumps({"ok": False, "error": "--query required"}))
                sys.exit(1)
            result = cmd_find(args.doc, args.query, args.max_results, args.context_chars)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.task == "self_test":
            result = cmd_self_test()
            print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
