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


# ─── Opus 驗收補丁（D-1 / D-2）新增 regression ─────────────────────

def test_deadline_as_int_still_injected():
    """Opus D-1: Vision prompt 回 deadline=10（純數字），OCR extractor 也寫 int(days)。
    原 `"日" in str(deadline)` 對 int 永遠 fail。修正後整數型 deadline 應正規化為 N日內 並注入。"""
    fname = _name("判決", deadline=15, deadline_type="補正")
    assert "15日內補正" in fname, f"int(15) 應被正規化為 15日內補正，實際: {fname}"


def test_deadline_as_numeric_string_still_injected():
    """Opus D-1: Vision prompt 有時回 '30' 字串（非 '30日內'），也要能正規化注入。"""
    fname = _name("函文", deadline="30", deadline_type="繳費")
    assert "30日內繳納" in fname, f"純數字字串 '30' 應被正規化為 30日內繳納，實際: {fname}"


def test_d3_ocr_normalizes_simplified_nei_to_traditional():
    """Opus D-3: OCR 常把「內」(U+5167) 讀成「内」(U+5185)；
    `_extract_legal_fields_from_ocr` 必須正規化，否則 regex 完全漏抓。"""
    text = "主旨：應於文到10日内補正，逾期不補正即駁回。"
    legal = _mod._extract_legal_fields_from_ocr(text, "裁定")
    assert legal.get("deadline") == 10, f"應抓到 deadline=10，實際: {legal}"
    assert legal.get("deadline_type") == "補正", f"應抓到 deadline_type=補正，實際: {legal}"


def test_d3_chenbao_pattern_matched_and_normalized_to_chenshuyijian():
    """Opus D-3: 函文常用「陳報」（非「陳述意見」），regex 必須抓，
    且 `_OSC_KEYWORDS` 把「陳報」轉為「陳述意見」以觸發 OSC todo_sync。"""
    text = "主旨：於文到10日内陳報如說明二之事項，查照。"
    legal = _mod._extract_legal_fields_from_ocr(text, "函文")
    assert legal.get("deadline") == 10, f"應抓到 deadline=10，實際: {legal}"
    assert legal.get("deadline_type") == "陳報", f"應抓到 deadline_type=陳報，實際: {legal}"

    # 用 _build_name_result 驗證 OSC keyword 正規化
    result = _mod._build_name_result(
        found_date="20250915",
        found_court="臺灣花蓮地方法院",
        found_case_no="114年度簡上字第25號",
        found_type="函文",
        found_party="[當事人K]",
        deadline=10,
        deadline_type="陳報",
    )
    fname = result.get("filename", "")
    assert "10日內陳述意見" in fname, f"陳報應被正規化為陳述意見，實際: {fname}"


def test_fast_text_path_extracts_deadline_from_ocr_text():
    """Opus D-2: fast text path 對 searchable PDF 也要呼叫 _extract_legal_fields_from_ocr
    並把 deadline 注入檔名。這條路之前完全沒接 deadline 抽取，造成真實 PDF 漏掉。"""
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "pdf_namer_d2",
        os.path.join(_SKILL_DIR, "action.py"),
    )
    _m = importlib.util.module_from_spec(_spec)
    try:
        _spec.loader.exec_module(_m)
    except Exception:
        pass
    # 模擬 searchable PDF 的 text layer：含日期、法院、案號、函文標記與「文到 10 日內陳述意見」
    # 內容長度貼近真實函文，避免少量全形標點被 _is_garbled_text 誤判
    text = (
        "臺灣花蓮地方法院 函\n"
        "地址 花蓮縣花蓮市府前路十五號\n"
        "承辦人 林書記官 電話 03-8225111\n"
        "中華民國114年9月15日\n"
        "發文字號 花院民簡上114字第25號\n"
        "速別 普通件 密等及解密條件或保密期限 普通\n"
        "附件 如說明所載\n"
        "受文者 [當事人K]\n"
        "主旨 本院114年度簡上字第25號返還土地事件 請於文到10日內陳述意見\n"
        "說明 一 依民事訴訟法規定辦理\n"
        "二 應於文到10日內具狀陳述意見並檢附相關資料回復本院\n"
        "三 逾期未陳述視為無意見\n"
        "正本 [當事人K]\n"
        "副本 本院書記官室\n"
    )
    result = _m._maybe_fast_text_name_result(text, case_name="[當事人K]")
    assert result is not None, "fast text path 應能從 text 抽出結構化結果"
    fname = result.get("filename", "")
    # 有抽到 deadline 並注入（日內陳述意見 或 日內XX）
    assert "日內" in fname, f"fast text path 應把 deadline 注入檔名，實際: {fname}"
