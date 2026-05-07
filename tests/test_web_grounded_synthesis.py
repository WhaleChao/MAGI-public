"""
Regression test: Bug #5 — web_research_synthesize and natural web-grounded routing.
"""
import os
import sys
import re
import unittest
from unittest.mock import patch, MagicMock

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

from skills.research.web_research import _maybe_route_to_web_grounded


class TestWebGroundedRouter(unittest.TestCase):
    """Natural language queries should route to web_grounded synthesis correctly."""

    WEB_GROUNDED_PROMPTS = [
        ("鼎泰豐永康店評價如何？", "review"),
        ("從台北車站到士林地方法院怎麼去？", "route"),
        ("三創園區營業時間？", "hours"),
        ("iPhone 16 和 Samsung S25 哪個比較好？", "compare"),
        ("最近有什麼法律新聞？", "news"),
    ]

    SMALL_TALK_PROMPTS = [
        "你覺得我今天運氣好嗎",
        "謝謝你",
        "你好",
        "哈囉",
    ]

    def test_web_grounded_prompts_are_routed(self):
        """All 5 web-grounded categories should be detected."""
        for prompt, expected_cat in self.WEB_GROUNDED_PROMPTS:
            result = _maybe_route_to_web_grounded(prompt)
            self.assertIsNotNone(result,
                f"Prompt '{prompt}' should route to web_grounded (expected '{expected_cat}'), got None")

    def test_small_talk_not_routed(self):
        """Pure small-talk should not trigger web_grounded routing."""
        for prompt in self.SMALL_TALK_PROMPTS:
            result = _maybe_route_to_web_grounded(prompt)
            self.assertIsNone(result,
                f"Small-talk prompt '{prompt}' should NOT route to web_grounded (got: {result})")

    def test_short_message_not_routed(self):
        """Very short messages should not trigger web_grounded."""
        result = _maybe_route_to_web_grounded("好")
        self.assertIsNone(result, "Too-short message should not trigger web_grounded")


class TestWebResearchSynthesize(unittest.TestCase):
    """web_research_synthesize should produce structured output with sources."""

    def _make_mock_result(self):
        """Create mock search result and page content."""
        return [
            {"title": "鼎泰豐評論 - Food Review Site", "url": "https://example.com/1", "snippet": "小籠包非常好吃，服務一流"},
            {"title": "台北美食推薦 - Travel Blog", "url": "https://example.com/2", "snippet": "鼎泰豐是台北必訪餐廳"},
            {"title": "網友評價討論 - PTT", "url": "https://example.com/3", "snippet": "CP值高，排隊值得"},
        ]

    @patch("skills.research.web_research.search_duckduckgo")
    @patch("skills.research.web_research.requests.get")
    @patch("skills.research.web_research._internet_guard")
    def test_synthesize_output_format(self, mock_guard, mock_get, mock_search):
        """synthesize should return answer + source section with [1][2][3] markers."""
        from skills.research.web_research import web_research_synthesize

        # Setup mocks
        mock_guard.return_value = (True, "")
        mock_search.return_value = self._make_mock_result()

        mock_resp = MagicMock()
        mock_resp.text = "<html><body><p>小籠包非常好吃，服務一流，推薦必訪</p><p>環境整潔，性價比高</p></body></html>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # Mock LLM synthesis
        with patch("skills.bridge.grounded_ai._generate_local") as mock_llm:
            mock_llm.return_value = "鼎泰豐永康店評價極佳 [來源 1]，小籠包皮薄餡多，服務專業 [來源 2]。排隊時間較長但值得等待 [來源 3]。"

            result = web_research_synthesize("鼎泰豐永康店評價如何？", max_sources=3)

        self.assertIn("── 資料來源 ──", result, "Result must contain source section")
        self.assertIn("[1]", result, "Result must have source [1]")
        self.assertIn("[2]", result, "Result must have source [2]")
        self.assertIn("[3]", result, "Result must have source [3]")
        self.assertGreater(len(result), 80, "Result must be > 80 chars (LLM synthesized)")

    @patch("skills.research.web_research.search_duckduckgo")
    @patch("skills.research.web_research._internet_guard")
    def test_no_results_returns_google_fallback(self, mock_guard, mock_search):
        """When search returns empty, fallback with Google search URL."""
        from skills.research.web_research import web_research_synthesize

        mock_guard.return_value = (True, "")
        mock_search.return_value = []

        result = web_research_synthesize("某某不存在的查詢")
        self.assertIn("google.com/search", result, "Empty result should fallback to Google URL")

    def test_function_exists(self):
        """web_research_synthesize must be importable."""
        from skills.research.web_research import web_research_synthesize
        self.assertTrue(callable(web_research_synthesize))

    def test_router_function_exists(self):
        """_maybe_route_to_web_grounded must be importable."""
        from skills.research.web_research import _maybe_route_to_web_grounded
        self.assertTrue(callable(_maybe_route_to_web_grounded))


if __name__ == "__main__":
    unittest.main()
