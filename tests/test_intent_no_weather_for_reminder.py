"""
Regression test: Bug #3 — reminder-style prompts must NOT be classified as weather queries.
"""
import os
import sys
import unittest

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

from skills.engine.realtime_data_gateway import classify_realtime_query


class TestNoWeatherForReminder(unittest.TestCase):
    """Reminder/schedule prompts should not be misrouted to weather intent."""

    REMINDER_PROMPTS = [
        "明天下午三點提醒我開會",
        "明天早上九點提醒我",
        "幫我記下來明天有會議",
        "今天下午兩點有個會議",
        "後天下午提醒我備忘",
    ]

    WEATHER_PROMPTS = [
        "今天台北天氣如何？",
        "明天會不會下雨",
        "台中氣溫幾度",
        "颱風來了嗎",
    ]

    def test_reminder_prompts_not_weather(self):
        """Reminder-style prompts (含「提醒」「開會」「會議」「備忘」) must not classify as weather."""
        for prompt in self.REMINDER_PROMPTS:
            result = classify_realtime_query(prompt)
            self.assertNotEqual(
                result, "weather",
                f"Prompt '{prompt}' was incorrectly classified as weather (got: {result})"
            )

    def test_weather_prompts_still_classified(self):
        """Actual weather prompts should still be classified as weather."""
        for prompt in self.WEATHER_PROMPTS:
            result = classify_realtime_query(prompt)
            self.assertEqual(
                result, "weather",
                f"Weather prompt '{prompt}' was NOT classified as weather (got: {result})"
            )


if __name__ == "__main__":
    unittest.main()
