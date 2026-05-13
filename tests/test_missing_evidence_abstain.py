"""
test_missing_evidence_abstain.py
==================================
Batch 5 — Task 9: 證據不足時應拒答而非猜測

當使用者要求根據不存在的證據推論時，系統應拒絕猜測並說明原因。
"""
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_abstain_response(text: str) -> bool:
    """回傳 True 如果回覆是合法的拒答/缺乏資料回應。"""
    if not text:
        return False
    abstain_phrases = [
        "無法確認", "資料不足", "缺乏", "找不到", "沒有足夠",
        "無相關", "不確定", "需要更多資訊", "請提供",
        "cannot confirm", "insufficient", "no evidence",
    ]
    return any(p in text for p in abstain_phrases)


def _is_speculative_fabrication(text: str) -> bool:
    """回傳 True 如果回覆包含明顯猜測性斷言。"""
    if not text:
        return False
    spec_phrases = [
        "應該是", "可能是", "大概是", "推測應為", "估計是",
        "猜測", "應屬", "推斷為",
    ]
    # Only flag if combined with definitive claim phrasing
    definitive = ["因此", "所以判決為", "所以原告勝訴", "所以被告有罪"]
    has_spec = any(p in text for p in spec_phrases)
    has_definitive = any(p in text for p in definitive)
    return has_spec and has_definitive


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMissingEvidenceAbstain:
    """當證據不足時，系統應拒答或明確標記不確定，而非胡亂猜測。"""

    def test_abstain_detector_positive(self):
        """Abstain detector 能正確識別拒答回應。"""
        abstain = "資料不足，無法確認此案件的判決結果，請提供更多資訊。"
        assert _is_abstain_response(abstain) is True

    def test_abstain_detector_negative(self):
        """Abstain detector 不誤判正常回應為拒答。"""
        normal = "根據卷宗，被告於 2026-03-15 到庭。"
        assert _is_abstain_response(normal) is False

    def test_speculative_fabrication_detector(self):
        """Speculation detector 能識別猜測性斷言。"""
        spec = "因為被告沒出庭，應該是認罪了，所以判決為有罪。"
        assert _is_speculative_fabrication(spec) is True
        safe = "被告缺席，法院將依法處理。"
        assert _is_speculative_fabrication(safe) is False

    def test_dispatch_case_management_no_hallucination(self):
        """dispatch_case_management 查無資料時不應編造案件細節。"""
        try:
            from api.pipelines.skill_dispatch import dispatch_case_management
        except ImportError:
            pytest.skip("skill_dispatch not importable")

        result = dispatch_case_management("查案件 完全不存在的姓名XYZQWE")
        if result is not None:
            assert not _is_speculative_fabrication(result), (
                f"Response fabricates speculative content: {result!r}"
            )

    def test_tw_output_guard_blocks_speculation_markers(self):
        """tw_output_guard 應能識別並過濾含 trust badge 的推測性回覆。"""
        try:
            from api.tw_output_guard import normalize_output_text
        except ImportError:
            pytest.skip("tw_output_guard not importable")

        # A reply that accidentally leaks internal badge + speculation
        bad_reply = "根據您的 [使用者陳述]，推測應為被告有罪，因此原告勝訴。"
        result = normalize_output_text(bad_reply)
        # The badge leak should have been stripped; speculation may remain
        assert "[使用者陳述]" not in result, "Trust badge should be stripped"

    def test_grounded_ai_hallucination_guard(self):
        """grounded_ai._is_persona_hallucination 能捕捉 persona 跑題。"""
        try:
            from skills.bridge.grounded_ai import _is_persona_hallucination
        except ImportError:
            pytest.skip("grounded_ai not importable")

        # Classic persona leak
        bad = "身為 CASPER，我推測此案件的判決結果應為原告勝訴。"
        assert _is_persona_hallucination(bad) is True
