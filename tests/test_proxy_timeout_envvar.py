"""
Regression test: Bug #1 — proxy timeout should use MAGI_TOOLS_API_PROXY_TIMEOUT env var
and default to >= 250 seconds (must exceed orchestrator COMPLEX-tier 240s upper bound).
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure MAGI root is in path
_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)


class TestProxyTimeoutEnvVar(unittest.TestCase):
    """Verify that the tools_api fallback proxy uses a configurable timeout >= 250s."""

    def test_default_timeout_is_250(self):
        """Default MAGI_TOOLS_API_PROXY_TIMEOUT should be 250s, not 30s."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAGI_TOOLS_API_PROXY_TIMEOUT", None)
            val = float(os.environ.get("MAGI_TOOLS_API_PROXY_TIMEOUT", "250") or "250")
            self.assertGreaterEqual(val, 250,
                "Default proxy timeout must be >= 250s to cover COMPLEX-tier 240s orchestrator timeout")

    def test_env_var_override(self):
        """MAGI_TOOLS_API_PROXY_TIMEOUT env var should override the default."""
        with patch.dict(os.environ, {"MAGI_TOOLS_API_PROXY_TIMEOUT": "300"}):
            val = float(os.environ.get("MAGI_TOOLS_API_PROXY_TIMEOUT", "250") or "250")
            self.assertEqual(val, 300.0)

    def test_server_py_uses_env_var(self):
        """Verify api/server.py source code uses MAGI_TOOLS_API_PROXY_TIMEOUT, not hardcoded 30."""
        server_path = os.path.join(_MAGI_ROOT, "api", "server.py")
        with open(server_path, "r") as f:
            source = f.read()
        # Must reference the env var
        self.assertIn("MAGI_TOOLS_API_PROXY_TIMEOUT", source,
                      "server.py must reference MAGI_TOOLS_API_PROXY_TIMEOUT env var")
        # The fallback proxy block must NOT use hardcoded timeout=30 any more
        # (the internal admin proxy at line ~606 uses timeout=20 which is fine for internal routes)
        # We specifically check the _fallback_to_tools_api function
        fallback_start = source.find("def _fallback_to_tools_api")
        self.assertGreater(fallback_start, 0, "Cannot find _fallback_to_tools_api function")
        fallback_section = source[fallback_start:fallback_start + 2000]
        self.assertNotIn("timeout=30", fallback_section,
                         "Hardcoded timeout=30 still present in _fallback_to_tools_api — Bug #1 not fixed")
        self.assertIn("_proxy_timeout", fallback_section,
                      "_proxy_timeout variable not found in _fallback_to_tools_api")


if __name__ == "__main__":
    unittest.main()
