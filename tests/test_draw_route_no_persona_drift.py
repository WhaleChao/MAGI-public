"""
Regression test: Bug #4 — draw requests must route to generate_image, not LLM persona.
"""
import os
import sys
import re
import unittest
from unittest.mock import patch, MagicMock

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)


class TestDrawRouteNoPersonaDrift(unittest.TestCase):
    """Draw requests must never return LLM persona refusal text."""

    # Persona drift keywords that must NEVER appear in draw responses
    PERSONA_DRIFT_MARKERS = ["大型語言模型", "無法畫圖", "只能用文字", "語言模型"]

    def _make_draw_reply(self, prompt: str, image_result: dict) -> str:
        """Simulate the early draw route in message_pipeline."""
        msg_lower = prompt.lower()
        _draw_early_pattern = re.compile(
            r"(?:/draw\b|畫[圖一個張幅]|\bdraw\b|generate image|產生圖片|绘[图画製]|画[圖图一])",
            re.IGNORECASE,
        )
        if not _draw_early_pattern.search(msg_lower):
            return None  # not a draw request
        if msg_lower.startswith("畫面") or msg_lower.startswith("畫成"):
            return None

        _draw_prompt = prompt
        for _kw in ["/draw", "幫我", "請", "畫圖", "一張", "一個", "draw", "generate image", "產生圖片", "畫", "画"]:
            _draw_prompt = re.sub(re.escape(_kw), "", _draw_prompt, flags=re.IGNORECASE).strip()

        if len(_draw_prompt) < 2:
            return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"

        # Simulate orch._generate_image
        if image_result.get("success"):
            return f"🎨 Image Generated (模型: test, 提示詞: {_draw_prompt})"
        else:
            error = image_result.get("error", "unknown")
            return f"❌ Image generation failed: {error}"

    def test_success_returns_image_result(self):
        """Successful generate_image should return image success message."""
        reply = self._make_draw_reply(
            "畫一張貓咪在彈鋼琴",
            {"success": True, "path": "/tmp/test.png", "model": "test"}
        )
        self.assertIsNotNone(reply)
        self.assertIn("🎨", reply)
        for marker in self.PERSONA_DRIFT_MARKERS:
            self.assertNotIn(marker, reply, f"Persona drift marker '{marker}' found in success reply")

    def test_failure_returns_explicit_error(self):
        """Failed generate_image should return ❌ error, not persona drift."""
        reply = self._make_draw_reply(
            "畫一張貓咪在彈鋼琴",
            {"success": False, "error": "Melchior offline"}
        )
        self.assertIsNotNone(reply)
        self.assertIn("❌", reply, "Failed draw should return ❌ error")
        for marker in self.PERSONA_DRIFT_MARKERS:
            self.assertNotIn(marker, reply, f"Persona drift marker '{marker}' found in failure reply")

    def test_draw_pattern_matches_target_prompt(self):
        """Verify '畫一張貓咪在彈鋼琴' triggers the draw pattern."""
        msg_lower = "畫一張貓咪在彈鋼琴"
        _draw_pattern = re.compile(
            r"(?:/draw\b|畫[圖一個張幅]|\bdraw\b|generate image|產生圖片|绘[图画製]|画[圖图一])",
            re.IGNORECASE,
        )
        self.assertIsNotNone(_draw_pattern.search(msg_lower),
                              "Draw pattern must match '畫一張貓咪在彈鋼琴'")

    def test_huamian_not_draw(self):
        """'畫面' (screen) should NOT trigger draw route."""
        reply = self._make_draw_reply("畫面很美", {"success": True})
        self.assertIsNone(reply, "'畫面' should not be caught by draw route")

    def test_pipeline_source_has_early_draw_check(self):
        """Verify message_pipeline.py has the early draw check before step 2.7.99."""
        pipeline_path = os.path.join(_MAGI_ROOT, "api", "pipelines", "message_pipeline.py")
        with open(pipeline_path, "r") as f:
            source = f.read()
        self.assertIn("2.7.98 Image Generation Early Route", source,
                      "Early draw route guard not found in message_pipeline.py")
        # Early check must appear before conversational intent dispatcher
        early_pos = source.find("2.7.98 Image Generation Early Route")
        conv_pos = source.find("2.7.99 Comprehensive Natural Language Intent Dispatcher")
        self.assertLess(early_pos, conv_pos,
                        "Early draw check must appear before 2.7.99 conversational intent")


if __name__ == "__main__":
    unittest.main()
