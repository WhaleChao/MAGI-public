"""
test_false_memory_regression.py
================================
Batch 5 — Task 9: 幻覺偵測回歸集

測試 LLM 在查詢不存在的案件時不應編造資料。
"""
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fabrication_indicators(text: str) -> bool:
    """回傳 True 如果文字包含典型編造特徵（虛構案號格式 + 確定語氣）。"""
    if not text:
        return False
    import re
    # 虛構案號格式 + 確定語氣
    has_fake_case = bool(re.search(r"\d{2,3}年度\w+字第\d+號", text))
    has_affirmative = any(w in text for w in ["確認", "已存在", "查到", "找到", "案件為"])
    return has_fake_case and has_affirmative


def _is_honest_not_found(text: str) -> bool:
    """回傳 True 如果回覆誠實表達找不到 / 無法確認。"""
    if not text:
        return False
    honest_phrases = [
        "找不到", "查無", "無法確認", "沒有找到", "不存在",
        "沒有相關", "無此案件", "查不到", "no result",
    ]
    return any(p in text for p in honest_phrases)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFalseMemoryRegression:
    """查詢不存在案件時，LLM 不應編造回應。"""

    def test_nonexistent_case_query_returns_not_found(self):
        """當案號根本不存在於 DB，dispatch_case_management 應回 None 或「找不到」。"""
        try:
            from api.pipelines.skill_dispatch import dispatch_case_management
        except ImportError:
            pytest.skip("skill_dispatch not importable")

        result = dispatch_case_management("查案件 NONEXISTENT_FAKE_99999")
        # Should be None (no intercept) or honest not-found message
        assert result is None or _is_honest_not_found(result), (
            f"Expected not-found response, got: {result!r}"
        )

    def test_fabrication_indicator_detection(self):
        """Fabrication detector 能正確識別虛構語句。"""
        fake = "已確認找到 115年度原訴字第99999號，案件為侵權行為。"
        honest = "查無符合條件的案件。"
        assert _fabrication_indicators(fake) is True
        assert _fabrication_indicators(honest) is False

    def test_honest_not_found_detection(self):
        """Honest-not-found detector 能正確識別誠實回應。"""
        assert _is_honest_not_found("找不到符合「ZZZZZ」的案件。") is True
        assert _is_honest_not_found("確認找到案件。") is False

    def test_case_list_empty_returns_honest(self):
        """當 DB 可能回空集合時，回應不應憑空生成案件資料。"""
        try:
            from api.pipelines.skill_dispatch import dispatch_case_management
        except ImportError:
            pytest.skip("skill_dispatch not importable")

        result = dispatch_case_management("查案件 完全不可能存在的人名xyz123")
        if result is not None:
            assert not _fabrication_indicators(result), (
                f"Response appears to fabricate data: {result!r}"
            )

    def test_update_nonexistent_case_fails_gracefully(self):
        """更新不存在的案件應回 None 或明確錯誤，不應靜默成功。"""
        try:
            from api.pipelines.skill_dispatch import dispatch_case_management
        except ImportError:
            pytest.skip("skill_dispatch not importable")

        result = dispatch_case_management("NONEXISTENT_PERSON_XYZ 改為已結案")
        if result is not None:
            # Should contain a not-found message
            assert _is_honest_not_found(result) or "失敗" in result or "找不到" in result, (
                f"Unexpected response for nonexistent case update: {result!r}"
            )
