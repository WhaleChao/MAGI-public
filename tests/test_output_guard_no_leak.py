"""
Regression test: Bug #2 — output_guard veto internal labels must not leak to user output.
"""
import os
import sys
import unittest

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

from skills.bridge.ensemble_inference import ConsensusResult, format_magi_response


class TestOutputGuardNoLeak(unittest.TestCase):
    """Verify internal veto reasons are never exposed to end users."""

    def test_output_guard_only_veto_returns_clean_answer(self):
        """When only output_guard vetoed, format_magi_response must return clean answer without discord block."""
        cr = ConsensusResult(
            unanimous=False,
            result="正常答案：法扶申請需要準備以下文件...",
            vetoed_by=["output_guard"],
            veto_reasons=["output_guard 已修剪內部標籤"],
        )
        reply = format_magi_response(cr)
        self.assertEqual(reply, "正常答案：法扶申請需要準備以下文件...",
                         "output_guard-only veto should return bare clean answer")
        self.assertNotIn("意見分歧", reply, "Should not contain 意見分歧 block")
        self.assertNotIn("輸出防衛", reply, "Should not contain 輸出防衛 label")
        self.assertNotIn("內部標籤", reply, "Should not contain 內部標籤 label")
        self.assertNotIn("三哲人", reply, "Should not contain 三哲人 block")

    def test_output_guard_with_other_veto_shows_other_only(self):
        """When output_guard + another veto, only the non-internal veto should appear."""
        cr = ConsensusResult(
            unanimous=False,
            result="答案文字",
            vetoed_by=["output_guard", "phi4"],
            veto_reasons=["output_guard 已修剪內部標籤", "phi4: 邏輯有誤"],
        )
        reply = format_magi_response(cr)
        self.assertIn("Melchior", reply, "phi4 veto (Melchior) should appear")
        self.assertNotIn("輸出防衛", reply, "output_guard should not appear")
        self.assertIn("三哲人意見分歧", reply)

    def test_no_veto_returns_plain_answer(self):
        """When unanimous, no disagreement block should appear."""
        cr = ConsensusResult(
            unanimous=True,
            result="這是正常回答",
            vetoed_by=[],
            veto_reasons=[],
        )
        reply = format_magi_response(cr)
        self.assertEqual(reply, "這是正常回答")
        self.assertNotIn("意見分歧", reply)

    def test_internal_veto_labels_not_in_reply(self):
        """Exhaustive check: none of the known internal leak strings should appear."""
        cr = ConsensusResult(
            unanimous=False,
            result="案件資料回覆",
            vetoed_by=["output_guard"],
            veto_reasons=["內部標籤洩漏或 persona 跑題"],
        )
        reply = format_magi_response(cr)
        forbidden = ["輸出防衛", "內部標籤洩漏", "三哲人意見分歧", "─── 三哲人"]
        for f in forbidden:
            self.assertNotIn(f, reply, f"Found forbidden string '{f}' in reply")


if __name__ == "__main__":
    unittest.main()
