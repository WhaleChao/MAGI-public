# -*- coding: utf-8 -*-
"""Task D.3 — 驗證 sync_osc_todos_for_path 路徑解析邏輯"""
import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer")
_spec = importlib.util.spec_from_file_location("smart_filer", os.path.join(_SKILL_DIR, "smart_filer.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.path.insert(0, _SKILL_DIR)

# 預先 mock 掉 smart_filer 的 NAS 相依路徑，避免 import 出錯
_fake_os_path = MagicMock()
_fake_os_path.exists.return_value = False
_fake_os_path.join = os.path.join
_fake_os_path.dirname = os.path.dirname
_fake_os_path.basename = os.path.basename
_fake_os_path.normpath = os.path.normpath
_fake_os_path.sep = os.sep

try:
    _spec.loader.exec_module(_mod)
except Exception:
    pass

sync_fn = _mod.sync_osc_todos_for_path


def test_sync_osc_todos_for_path_resolves_case_folder():
    """path 含 01_案件 → 正確推出 case_folder_name"""
    test_path = (
        "/Volumes/lumi/lumi/01_案件/一般案件/2025-0001-王大明/06_閱卷資料/"
        "20241015 裁定（王大明；15日內補正）.pdf"
    )
    called_with = []

    def _fake_sync(filed_path, match, analysis):
        called_with.append(analysis.get("case_folder_name", ""))

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch.object(_mod, "_best_effort_sync_osc_todos", side_effect=_fake_sync):
            result = sync_fn(test_path)

    assert result.get("success") is True, f"應 success=True，實際: {result}"
    assert result.get("case_folder_name") == "2025-0001-王大明", (
        f"應解析出 2025-0001-王大明，實際: {result}"
    )
    assert called_with == ["2025-0001-王大明"], (
        f"_best_effort_sync_osc_todos 應被以正確案件名呼叫，實際: {called_with}"
    )


def test_sync_osc_todos_for_path_skips_when_not_in_case_tree():
    """path 不含 01_案件 → return skipped: not_in_case_tree"""
    test_path = "/tmp/test_20241015 裁定.pdf"

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        result = sync_fn(test_path)

    assert result.get("success") is False
    assert result.get("skipped") == "not_in_case_tree", f"應 skipped=not_in_case_tree，實際: {result}"
