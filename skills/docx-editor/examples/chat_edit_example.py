"""
chat_edit_example.py — Phase 3: cmd_chat_edit 用法示範

執行方式：
    MAGI_DOCX_EDITOR_ALLOW_CLI=1 python skills/docx-editor/examples/chat_edit_example.py

注意：此範例使用 mock LLM，不需要真實 oMLX 服務。
"""

import os
import sys
from unittest.mock import patch

# Ensure skill is importable
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SKILL_DIR)
sys.path.insert(0, os.path.join(_SKILL_DIR, "lib"))

# Use the fixture as input
_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "tests", "fixtures", "docx_editor", "simple.docx"
)

import importlib.util as _ilu
import json as _json

# Load action module
_spec = _ilu.spec_from_file_location("docx_action", os.path.join(_SKILL_DIR, "action.py"))
_action_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_action_mod)
cmd_chat_edit = _action_mod.cmd_chat_edit


def main():
    # Mock LLM to return a fixed edit
    mock_edit_json = _json.dumps([
        {
            "find": "Hello World",
            "replace": "Hello MAGI",
            "context_before": "",
            "context_after": "",
            "reason": "依律師指令更新問候語",
        }
    ])

    with patch("lib.llm_edit_planner._call_llm", return_value=mock_edit_json):
        result = cmd_chat_edit(
            doc_path=os.path.abspath(_FIXTURE_PATH),
            instruction="把 Hello World 改成 Hello MAGI",
            source="cli",  # MAGI_DOCX_EDITOR_ALLOW_CLI=1 才能用 cli
        )

    print("結果：")
    print(_json.dumps(result, ensure_ascii=False, indent=2))

    if result["ok"] and result.get("output_path"):
        print(f"\n✅ 套用 {result['changes_applied']} 處修改 → {result['output_path']}")
    elif result.get("warnings"):
        print(f"\nℹ️ {result['warnings'][0]}")
    else:
        print(f"\n❌ {result.get('errors', [])}")


if __name__ == "__main__":
    main()
