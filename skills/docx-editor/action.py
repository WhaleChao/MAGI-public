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


def cmd_generate(
    title: str,
    sections_json: str,
    output_path: str = "",
    landscape: bool = False,
    author: str = "MAGI",
) -> Dict:
    """從 JSON 產 .docx。

    sections_json: JSON string of List[SectionSpec dict]
    每個 SectionSpec dict 可含：
      - heading: str (optional)
      - level: int (1/2/3, default 1)
      - content: str (多段以 \\n\\n 分隔, optional)
      - table: {headers: [...], rows: [[...],...]} (optional)
      - page_break: bool (optional)

    output_path: 預設 /tmp/magi_docx_gen/<title>.docx

    Returns: {ok: bool, output_path: str, error: str}
    """
    import re as _re
    from lib.generator import generate_docx, GenerateDocxRequest, SectionSpec, TableSpec

    try:
        raw_sections = json.loads(sections_json)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"sections_json parse error: {e}", "output_path": None}

    sections = []
    for i, s in enumerate(raw_sections):
        if not isinstance(s, dict):
            return {"ok": False, "error": f"section[{i}] is not a dict", "output_path": None}
        table = None
        if s.get("table"):
            t = s["table"]
            table = TableSpec(
                headers=t.get("headers", []),
                rows=t.get("rows", []),
            )
        sections.append(SectionSpec(
            heading=s.get("heading"),
            level=int(s.get("level", 1)),
            content=s.get("content"),
            table=table,
            page_break=bool(s.get("page_break", False)),
        ))

    req = GenerateDocxRequest(
        title=title,
        sections=sections,
        landscape=landscape,
        author=author,
    )

    try:
        docx_bytes = generate_docx(req)
    except (ValueError, Exception) as e:
        return {"ok": False, "error": str(e), "output_path": None}

    # Determine output path
    if not output_path:
        out_dir = "/tmp/magi_docx_gen"
        os.makedirs(out_dir, exist_ok=True)
        # Sanitize title for filename
        safe_title = _re.sub(r"[^\w一-鿿\-]", "_", title)[:60]
        output_path = os.path.join(out_dir, f"{safe_title}.docx")

    with open(output_path, "wb") as f:
        f.write(docx_bytes)

    return {"ok": True, "output_path": output_path, "error": None, "size_bytes": len(docx_bytes)}


def cmd_chat_edit(
    doc_path: str,
    instruction: str,
    output_path: str = "",
    author: str = "MAGI",
    source: str = "",
) -> Dict:
    """律師 chat 入口：給 docx + 指令，回 edited.docx。

    安全閘門：source 必須含 user/telegram/discord/line（防 CLI 直接呼叫無預期改動律師檔案）。
    可用 MAGI_DOCX_EDITOR_ALLOW_CLI=1 bypass（測試用）。

    注意：此函式在 Phase 3 (commit 6) 完整實作；目前為骨架佔位。

    Returns: {
        "ok": bool,
        "output_path": str | None,
        "changes_applied": int,
        "warnings": [str, ...],
        "errors": [{"index": N, "reason": "..."}],
    }
    """
    # --- 安全閘門 ---
    _safe_sources = ("user", "telegram", "discord", "line")
    _allow_cli = os.environ.get("MAGI_DOCX_EDITOR_ALLOW_CLI", "0").strip() == "1"
    _source_ok = _allow_cli or any(s in source.lower() for s in _safe_sources)
    if not _source_ok:
        return {
            "ok": False,
            "output_path": None,
            "changes_applied": 0,
            "warnings": [],
            "errors": [{"index": -1, "reason": "安全閘門：source 必須含 user/telegram/discord/line，或設 MAGI_DOCX_EDITOR_ALLOW_CLI=1"}],
        }

    # --- 讀取文件全文 ---
    extract_result = cmd_extract(doc_path)
    docx_text = extract_result.get("text", "")

    # --- LLM edit planner ---
    from lib.llm_edit_planner import plan_edits_with_llm

    edits, warnings_list = plan_edits_with_llm(
        docx_text=docx_text,
        user_instruction=instruction,
    )

    if not edits:
        # LLM 回空 list（指令超出 anchored edit 範圍）
        return {
            "ok": True,
            "output_path": None,
            "changes_applied": 0,
            "warnings": warnings_list + ["LLM 判定指令超出 anchored edit 範圍，建議用 @MAGI 產文件"],
            "errors": [],
        }

    # --- 套用 edits ---
    import datetime
    if not output_path:
        out_dir = "/tmp/magi_docx_edits"
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = os.path.basename(doc_path)
        output_path = os.path.join(out_dir, f"{ts}_{base_name}")

    edits_dicts = [
        {
            "find": e.find,
            "replace": e.replace,
            "context_before": e.context_before,
            "context_after": e.context_after,
            "reason": e.reason,
        }
        for e in edits
    ]

    apply_result = cmd_apply(doc_path, edits_dicts, output_path=output_path, author=author)

    return {
        "ok": apply_result["ok"],
        "output_path": apply_result.get("output_path"),
        "changes_applied": apply_result.get("success_count", 0),
        "warnings": warnings_list,
        "errors": apply_result.get("errors", []),
    }


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
    parser.add_argument("--task", required=True, choices=["apply", "extract", "find", "self_test", "generate", "chat_edit"])
    parser.add_argument("--doc", help="Path to .docx file")
    parser.add_argument("--edits", help="JSON string or path to JSON file with edits list")
    parser.add_argument("--output", help="Output path for apply/generate task")
    parser.add_argument("--author", default="MAGI", help="Author name for tracked changes")
    parser.add_argument("--query", help="Text to search for (find task)")
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--context-chars", type=int, default=80)
    # generate task args
    parser.add_argument("--title", help="Document title (generate task)")
    parser.add_argument("--sections", help="JSON array of SectionSpec dicts (generate task)")
    parser.add_argument("--landscape", action="store_true", help="Landscape orientation (generate task)")
    # chat_edit task args
    parser.add_argument("--instruction", help="Edit instruction for chat_edit task")

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

        elif args.task == "generate":
            if not args.title:
                print(json.dumps({"ok": False, "error": "--title required"}))
                sys.exit(1)
            if not args.sections:
                print(json.dumps({"ok": False, "error": "--sections required"}))
                sys.exit(1)
            result = cmd_generate(
                title=args.title,
                sections_json=args.sections,
                output_path=args.output or "",
                landscape=args.landscape,
                author=args.author,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.task == "chat_edit":
            if not args.doc:
                print(json.dumps({"ok": False, "error": "--doc required"}))
                sys.exit(1)
            if not args.instruction:
                print(json.dumps({"ok": False, "error": "--instruction required"}))
                sys.exit(1)
            _allow_cli = os.environ.get("MAGI_DOCX_EDITOR_ALLOW_CLI", "0").strip() == "1"
            result = cmd_chat_edit(
                doc_path=args.doc,
                instruction=args.instruction,
                output_path=args.output or "",
                author=args.author,
                source="cli" if _allow_cli else "",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
