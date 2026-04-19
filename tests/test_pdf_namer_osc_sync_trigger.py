# -*- coding: utf-8 -*-
"""Task D.2 — 驗證 _trigger_osc_sync_if_applicable 的快速過濾邏輯"""
import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer")
_spec = importlib.util.spec_from_file_location("pdf_namer_action", os.path.join(_SKILL_DIR, "action.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.path.insert(0, _SKILL_DIR)
try:
    _spec.loader.exec_module(_mod)
except Exception:
    pass

_trigger = _mod._trigger_osc_sync_if_applicable


def test_quick_filter_skips_when_no_deadline_in_filename():
    """檔名無「N日內XX」→ _trigger_osc_sync_if_applicable 不呼叫 sync"""
    called = []

    def _fake_sync(path):
        called.append(path)
        return {"success": True}

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch("importlib.util.spec_from_file_location") as mock_spec:
            mock_mod = MagicMock()
            mock_mod.sync_osc_todos_for_path = _fake_sync
            mock_spec.return_value.loader.exec_module = lambda m: None
            _trigger("/path/20241015 花蓮地院裁定（王大明）.pdf", {})

    assert len(called) == 0, "不含期限的檔名不應觸發 sync"


def test_quick_filter_triggers_when_deadline_present():
    """檔名含「15日內補正」→ 會呼叫 sync_osc_todos_for_path"""
    called = []

    import importlib.util as _ilu

    _real_spec = _ilu.spec_from_file_location

    def _patched_spec(name, path, *args, **kwargs):
        if "smart_filer" in (name or ""):
            spec = MagicMock()

            def exec_mod(mod):
                mod.sync_osc_todos_for_path = lambda p: called.append(p) or {"success": True}

            spec.loader.exec_module = exec_mod
            return spec
        return _real_spec(name, path, *args, **kwargs)

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch("importlib.util.spec_from_file_location", side_effect=_patched_spec):
            _trigger(
                "/Volumes/lumi/lumi/01_案件/一般案件/2025-0001-王大明/05_法院通知/"
                "20241015 花蓮地院裁定（王大明；15日內補正）.pdf",
                {"deadline": "15日內", "deadline_type": "補正"},
            )

    assert len(called) == 1, "含期限的檔名應觸發 sync"


def test_feature_flag_off_skips_trigger():
    """PDF_NAMER_OSC_TODO_SYNC=0 → 不觸發"""
    called = []

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "0"}):
        with patch("importlib.util.spec_from_file_location") as mock_spec:
            mock_mod = MagicMock()
            mock_mod.sync_osc_todos_for_path = lambda p: called.append(p)
            _trigger("/path/20241015 裁定（15日內補正）.pdf", {})

    assert len(called) == 0, "feature_flag=0 時不應觸發"


def test_sync_failure_does_not_break_rename():
    """OSC sync 拋例外 → 不影響 rename_file 主流程"""
    import importlib.util as _ilu
    _real_spec = _ilu.spec_from_file_location

    def _patched_spec(name, path, *args, **kwargs):
        if "smart_filer" in (name or ""):
            spec = MagicMock()

            def exec_mod(mod):
                def _raise(p):
                    raise RuntimeError("subprocess failed")
                mod.sync_osc_todos_for_path = _raise

            spec.loader.exec_module = exec_mod
            return spec
        return _real_spec(name, path, *args, **kwargs)

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch("importlib.util.spec_from_file_location", side_effect=_patched_spec):
            # 不應拋例外
            try:
                _trigger(
                    "/path/20241015 裁定（15日內補正）.pdf",
                    {},
                )
            except Exception as e:
                assert False, f"sync 失敗不應丟例外到外層，但拋了: {e}"
