"""
docx-editor — 簡單使用範例

展示如何用 Python API 對 .docx 套用 tracked changes。
"""

import os
import sys

# Add skill dir to path
_skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _skill_dir)

from lib.tracked_edits import apply_tracked_edits, EditInput


def main():
    # Path to input docx
    input_path = os.path.join(
        os.path.dirname(_skill_dir),
        "tests", "fixtures", "docx_editor", "simple.docx"
    )
    output_path = "/tmp/simple_edit_output.docx"

    print(f"Reading: {input_path}")
    with open(input_path, "rb") as f:
        docx_bytes = f.read()

    # Define edits
    edits = [
        EditInput(
            find="Hello World",
            replace="Hello MAGI",
            context_before="",
            context_after="",
            reason="更新問候語",
        ),
    ]

    # Apply tracked edits
    result = apply_tracked_edits(docx_bytes, edits, author="範例律師")

    print(f"Changes applied: {len(result.changes)}")
    for change in result.changes:
        print(f"  - Deleted: '{change.deleted_text}' → Inserted: '{change.inserted_text}'")

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors:
            print(f"  - Edit #{err.index}: {err.reason}")

    # Write output
    with open(output_path, "wb") as f:
        f.write(result.bytes)

    print(f"\nOutput written to: {output_path}")
    print("Open in Microsoft Word to review tracked changes (Accept/Reject).")


if __name__ == "__main__":
    main()
