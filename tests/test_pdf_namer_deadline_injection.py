# -*- coding: utf-8 -*-
"""Task D.1 — 驗證 deadline 正確注入到檔名括號（5 類別白名單 + 關鍵字正規化）"""
import sys
import os
import importlib.util

# 動態載入 action.py（連字號目錄）
_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer")
_spec = importlib.util.spec_from_file_location("pdf_namer_action", os.path.join(_SKILL_DIR, "action.py"))
_mod = importlib.util.module_from_spec(_spec)
# 把 skill 目錄加到 sys.path 讓 action.py 的相對 import 能找到
sys.path.insert(0, _SKILL_DIR)
try:
    _spec.loader.exec_module(_mod)
except Exception:
    pass  # allow partial import for unit tests

_build = _mod._build_name_result


def _name(category, deadline=None, deadline_type="", party="王大明", smry="應補正"):
    """Helper: 用最小參數呼叫 _build_name_result，回傳 filename。"""
    result = _build(
        found_date="20241015",
        found_court="花蓮地方法院",
        found_case_no="114年度原訴字第24號",
        found_type=category,
        found_party=party,
        summary=smry,
        deadline=deadline,
        deadline_type=deadline_type,
    )
    return result.get("filename", "")


def test_deadline_injected_for_judgment():
    """判決 + deadline=15日內 + type=補正 → 檔名 bracket 含「15日內補正」"""
    fname = _name("判決", deadline="15日內", deadline_type="補正")
    assert "15日內補正" in fname, f"期望含 15日內補正，實際: {fname}"


def test_deadline_injected_for_ruling():
    """裁定 + deadline=20日 + type=繳費 → 檔名 bracket 含「20日繳納」（繳費→繳納正規化）"""
    fname = _name("裁定", deadline="20日", deadline_type="繳費")
    assert "繳納" in fname, f"期望含繳納（正規化），實際: {fname}"
    assert "20日" in fname, f"期望含 20日，實際: {fname}"
    assert "繳費" not in fname, f"不應出現「繳費」（應被正規化），實際: {fname}"


def test_deadline_not_injected_for_other_types():
    """委任狀 + deadline 存在 → 白名單外，不注入 deadline"""
    result = _build(
        found_date="20241015",
        found_court="",
        found_case_no="",
        found_type="委任狀",
        found_party="王大明",
        deadline="15日內",
        deadline_type="補正",
    )
    fname = result.get("filename", "")
    assert "15日內補正" not in fname, f"委任狀不應含 deadline，實際: {fname}"


def test_payment_keyword_normalized():
    """deadline_type=繳費 → 檔名應出現「繳納」而非「繳費」（對齊 OSC regex）"""
    fname = _name("函文", deadline="30日內", deadline_type="繳費")
    assert "繳納" in fname, f"「繳費」應被正規化為「繳納」，實際: {fname}"
    assert "繳費" not in fname, f"不應出現「繳費」，實際: {fname}"


def test_review_period_keyword_normalized():
    """deadline_type=閱卷期限 → 檔名應出現「閱卷」而非「閱卷期限」"""
    fname = _name("庭通知書", deadline="10日內", deadline_type="閱卷期限")
    assert "閱卷" in fname, f"「閱卷期限」應被正規化，實際: {fname}"
    assert "閱卷期限" not in fname, f"不應出現「閱卷期限」，實際: {fname}"
