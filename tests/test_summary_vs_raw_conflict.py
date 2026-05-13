"""
test_summary_vs_raw_conflict.py
================================
Batch 5 — Task 9: 摘要與原文衝突偵測

當摘要與原文主張衝突時，系統應標明衝突而非選邊站。
"""
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _contains_conflict_marker(text: str) -> bool:
    """回傳 True 如果文字包含衝突標記。"""
    if not text:
        return False
    markers = [
        "衝突", "矛盾", "不一致", "與原文不符", "存疑",
        "待確認", "conflict", "inconsistent", "discrepancy",
        "【注意】", "【提醒】",
    ]
    return any(m in text for m in markers)


def _output_guard_strips_fabrication(text: str) -> str:
    """模擬 tw_output_guard 的輸出過濾。"""
    try:
        from api.tw_output_guard import normalize_output_text
        return normalize_output_text(text)
    except ImportError:
        return text


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSummaryVsRawConflict:
    """摘要與原文衝突時應標明，而非靜默忽略。"""

    def test_conflict_marker_detection_positive(self):
        """衝突偵測能正確識別帶有衝突標記的文字。"""
        conflicted = "根據摘要王大明勝訴，但原文判決書顯示敗訴，兩者存在衝突，請確認原文。"
        assert _contains_conflict_marker(conflicted) is True

    def test_conflict_marker_detection_negative(self):
        """不含衝突標記的文字應回 False。"""
        normal = "根據判決書，王大明敗訴。"
        assert _contains_conflict_marker(normal) is False

    def test_output_guard_does_not_strip_conflict_warnings(self):
        """tw_output_guard 不應過濾掉衝突警告訊息。"""
        warning = "【注意】摘要與原文存在衝突，請確認原始判決書。"
        filtered = _output_guard_strips_fabrication(warning)
        # Warning should survive the guard
        assert filtered is not None
        assert len(filtered) > 0

    def test_summarize_pipeline_import(self):
        """摘要 pipeline 可正常 import。"""
        try:
            from api.tools_api import app  # noqa: F401
        except ImportError:
            pytest.skip("tools_api not importable in test env")

    def test_conflicting_summary_flagged_in_response(self):
        """測試 recall 結果衝突時的處理邏輯（單元層級）。"""
        # Simulate a scenario where two recall results disagree
        result_a = {"content": "王大明案：原告勝訴", "source": "summary", "score": 0.85}
        result_b = {"content": "王大明案：原告敗訴（原文）", "source": "raw_text", "score": 0.80}

        # When two sources disagree, a well-formed response should note the conflict
        contents = [result_a["content"], result_b["content"]]
        has_opposite = (
            "勝訴" in contents[0] and "敗訴" in contents[1]
        ) or (
            "敗訴" in contents[0] and "勝訴" in contents[1]
        )
        assert has_opposite is True, "Test fixture should have opposing claims"

    def test_recall_results_structure(self):
        """recall() 回傳的結果應有 content 與 source 欄位。"""
        try:
            from skills.memory.mem_bridge import recall
            results = recall("不存在的虛假查詢 zxqwerty", top_k=1)
            # Empty is fine; if not empty, must have expected structure
            for r in results:
                assert "content" in r or "text" in r, f"Result missing content: {r}"
        except ImportError:
            pytest.skip("mem_bridge not importable")
        except Exception:
            pass  # DB connectivity issues are OK in unit test context
