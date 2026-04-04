"""Unit tests for melchior_client circuit breaker logic."""

import time
import unittest
from unittest.mock import patch


class TestCircuitBreaker(unittest.TestCase):
    """Test the circuit breaker trip/reset/is_open logic."""

    def setUp(self):
        # Import after patching env if needed
        import skills.bridge.melchior_client as mc
        self.mc = mc
        # Always reset before each test
        mc._cb_reset()

    def test_reset_clears_state(self):
        self.mc._cb_reset()
        self.assertEqual(self.mc._CIRCUIT_BREAKER["consecutive_failures"], 0)
        self.assertEqual(self.mc._CIRCUIT_BREAKER["tripped_at"], 0.0)
        self.assertEqual(self.mc._CIRCUIT_BREAKER["cooldown_level"], 0)
        self.assertFalse(self.mc._cb_is_open())

    def test_single_failure_does_not_trip(self):
        """With threshold=3, a single failure should NOT trip the breaker."""
        self.mc._cb_trip("test error")
        self.assertFalse(self.mc._cb_is_open())
        self.assertEqual(self.mc._CIRCUIT_BREAKER["consecutive_failures"], 1)

    def test_threshold_trips_breaker(self):
        """After CIRCUIT_BREAKER_THRESHOLD failures, breaker should trip."""
        for i in range(self.mc.CIRCUIT_BREAKER_THRESHOLD):
            self.mc._cb_trip(f"error #{i}")
        self.assertTrue(self.mc._cb_is_open())

    def test_reset_after_trip(self):
        """Reset should clear tripped state."""
        for i in range(self.mc.CIRCUIT_BREAKER_THRESHOLD):
            self.mc._cb_trip("error")
        self.assertTrue(self.mc._cb_is_open())
        self.mc._cb_reset()
        self.assertFalse(self.mc._cb_is_open())

    def test_cooldown_expires(self):
        """After cooldown period, breaker should allow probes."""
        for i in range(self.mc.CIRCUIT_BREAKER_THRESHOLD):
            self.mc._cb_trip("error")
        self.assertTrue(self.mc._cb_is_open())

        # Fast-forward time past cooldown
        cooldown = self.mc._CIRCUIT_BREAKER.get("effective_cooldown", self.mc.CIRCUIT_BREAKER_COOLDOWN_SEC)
        self.mc._CIRCUIT_BREAKER["tripped_at"] = time.monotonic() - cooldown - 1
        self.assertFalse(self.mc._cb_is_open())

    def test_exponential_backoff_level(self):
        """Each trip should increase the cooldown level."""
        for i in range(self.mc.CIRCUIT_BREAKER_THRESHOLD):
            self.mc._cb_trip("error")
        level_after_first_trip = self.mc._CIRCUIT_BREAKER["cooldown_level"]
        self.assertGreaterEqual(level_after_first_trip, 1)

    def test_effective_cooldown_has_jitter(self):
        """Effective cooldown should include jitter (not exact base value)."""
        for i in range(self.mc.CIRCUIT_BREAKER_THRESHOLD):
            self.mc._cb_trip("error")
        eff = self.mc._CIRCUIT_BREAKER.get("effective_cooldown", 0)
        base = self.mc._CB_COOLDOWN_BASE_SEC
        # Jitter is 0.8-1.2x, so effective should be within that range of base
        self.assertGreater(eff, 0)
        self.assertGreaterEqual(eff, base * 0.7)  # allow some margin
        self.assertLessEqual(eff, base * 1.3)

    def test_get_circuit_breaker_status(self):
        """Status API should return well-formed dict."""
        status = self.mc.get_circuit_breaker_status()
        self.assertIn("open", status)
        self.assertIn("consecutive_failures", status)
        self.assertIn("threshold", status)
        self.assertIn("cooldown_sec", status)
        self.assertIn("status", status)
        self.assertFalse(status["open"])

    def test_resolve_omlx_chat_model_falls_back_to_available_local_model(self):
        resolved = self.mc._resolve_omlx_chat_model(
            "TAIDE-12b-Chat-mlx-4bit",
            available_models=["Qwen2.5-Coder-14B-Instruct-4bit"],
        )
        self.assertEqual(resolved, "Qwen2.5-Coder-14B-Instruct-4bit")


if __name__ == "__main__":
    unittest.main()
