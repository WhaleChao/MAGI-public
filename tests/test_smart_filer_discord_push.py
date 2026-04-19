# -*- coding: utf-8 -*-
"""Opus 驗收補丁 — _best_effort_sync_osc_todos Step 3 Discord push

驗證三個根因修復：
  C-1: stdout 含 {"ok": true} 會觸發 Discord push（原先只看 success 字串永不觸發）
  C-2: topic_key="filing"（非 pdf_filing）才能落到 filing 子頻道
  C-3: case_folder_name 取 local 變數，不會被 analysis 缺欄位洗掉

全部以 subprocess mock 驗證，不打真實 OSC / Discord。
"""
import os
import sys
import json
import importlib.util
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer")
_spec = importlib.util.spec_from_file_location(
    "smart_filer_for_discord_test",
    os.path.join(_SKILL_DIR, "smart_filer.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.path.insert(0, _SKILL_DIR)
try:
    _spec.loader.exec_module(_mod)
except Exception:
    pass


def _fake_completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_step3_pushes_discord_when_ok_true_in_stdout():
    """OSC todo_sync 回 {"ok": true, ...} → 應觸發 Discord push"""
    pushes = []

    def _fake_run(cmd, **kwargs):
        # cmd = [py, OSC_ORCH_PATH, "--task", "todo_sync ..."]
        if "todo_sync" in " ".join(cmd):
            return _fake_completed(0, json.dumps({"ok": True, "case_number": "2025-0001"}))
        return _fake_completed(0, json.dumps({"ok": True}))

    def _fake_push(case_folder, file_name):
        pushes.append((case_folder, file_name))

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch.object(_mod.subprocess, "run", side_effect=_fake_run):
            with patch.object(_mod, "_push_discord_pdf_filing", side_effect=_fake_push):
                with patch.object(_mod.os.path, "exists", return_value=True):
                    _mod._best_effort_sync_osc_todos(
                        "/Volumes/lumi/lumi/01_案件/一般案件/2025-0001-王大明/06_閱卷資料/x.pdf",
                        {"case_info": {"folder_name": "2025-0001-王大明"}},
                        {"doc_type": "判決"},
                    )

    assert len(pushes) == 1, f"Discord push 應被呼叫 1 次（ok:true），實際: {pushes}"
    assert pushes[0][0] == "2025-0001-王大明", (
        f"case_folder_name 應優先用 match.case_info.folder_name（local 變數），實際: {pushes[0]}"
    )
    assert pushes[0][1] == "x.pdf", f"檔名應為 basename，實際: {pushes[0]}"


def test_step3_does_not_push_when_ok_false():
    """stdout 含 ok:false → 不應觸發 Discord push（避免假陽性）"""
    pushes = []

    def _fake_run(cmd, **kwargs):
        if "todo_sync" in " ".join(cmd):
            return _fake_completed(0, json.dumps({"ok": False, "error": "missing_case_number"}))
        return _fake_completed(0, json.dumps({"ok": True}))

    def _fake_push(case_folder, file_name):
        pushes.append((case_folder, file_name))

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch.object(_mod.subprocess, "run", side_effect=_fake_run):
            with patch.object(_mod, "_push_discord_pdf_filing", side_effect=_fake_push):
                with patch.object(_mod.os.path, "exists", return_value=True):
                    _mod._best_effort_sync_osc_todos(
                        "/Volumes/lumi/lumi/01_案件/一般案件/2025-0001-王大明/06_閱卷資料/x.pdf",
                        {"case_info": {"folder_name": "2025-0001-王大明"}},
                        {"doc_type": "判決"},
                    )

    assert len(pushes) == 0, f"ok:false 不應觸發 push，實際: {pushes}"


def test_step3_falls_back_to_substring_on_malformed_json():
    """stdout 非 JSON 但含 '\"ok\": true' → 應 fallback 觸發 push"""
    pushes = []

    def _fake_run(cmd, **kwargs):
        if "todo_sync" in " ".join(cmd):
            # 混合 log + JSON fragment（模擬 action.py 前面有 warning log）
            return _fake_completed(0, 'WARNING: something\n{"ok": true, "case": "..."}')
        return _fake_completed(0, "")

    def _fake_push(case_folder, file_name):
        pushes.append((case_folder, file_name))

    with patch.dict(os.environ, {"PDF_NAMER_OSC_TODO_SYNC": "1"}):
        with patch.object(_mod.subprocess, "run", side_effect=_fake_run):
            with patch.object(_mod, "_push_discord_pdf_filing", side_effect=_fake_push):
                with patch.object(_mod.os.path, "exists", return_value=True):
                    _mod._best_effort_sync_osc_todos(
                        "/Volumes/lumi/lumi/01_案件/一般案件/2025-0001-王大明/06_閱卷資料/x.pdf",
                        {"case_info": {"folder_name": "2025-0001-王大明"}},
                        {"doc_type": "判決"},
                    )

    assert len(pushes) == 1, f"fallback substring 應觸發 push，實際: {pushes}"


def test_push_uses_filing_topic_key_not_pdf_filing():
    """_push_discord_pdf_filing 必須用 topic_key='filing'（OSC canonical key）
    而非 'pdf_filing'（不存在的 alias，會落到 general 頻道）"""
    captured = {}

    def _capture(message, *, severity="", source="", topic_key="", queue_on_fail=True):
        captured["topic_key"] = topic_key
        captured["source"] = source
        captured["severity"] = severity
        return {"ok_any": True}

    # 動態注入 send_telegram_push_with_status 到兩條 import 路徑
    fake_mod = SimpleNamespace(send_telegram_push_with_status=_capture)

    with patch.dict(sys.modules, {
        "skills.ops.red_phone": fake_mod,
        "red_phone": fake_mod,
    }):
        _mod._push_discord_pdf_filing("2025-0001-王大明", "20260419 判決（15日內補正）.pdf")

    assert captured.get("topic_key") == "filing", (
        f"topic_key 必須是 'filing'（canonical），實際: {captured}"
    )
    assert captured.get("source") == "pdf_namer"
