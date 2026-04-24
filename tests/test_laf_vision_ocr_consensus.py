# -*- coding: utf-8 -*-
"""
Phase F 測試：LAFVision OCR 共識接入（shadow-only）

測試範圍：
  1. extract_start_date() flag-off → 行為 bit-for-bit 與原邏輯相同
  2. extract_start_date_with_metadata() flag-off → dict 格式正確，date 與舊方法一致
  3. shadow 模式 → consensus 被呼叫，但回傳值仍以 legacy 為準
  4. enable + date critical_conflict → date=None, writable=False, confidence=0.0
  5. enable + date 一致 → date 有值, confidence >= 0.75, writable=True

禁止事項（Three-Module Protection Compliance）：
  - 不得在 module level import api.server / api.tools_api / daemon
  - 不得更動 extract_start_date() 的回傳型別（Optional[str]）
  - 不得影響 laf_automation_v2.py captcha OCR（ddddocr，完全分離）
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers: build minimal mock OCRConsensusResult
# ---------------------------------------------------------------------------

def _make_consensus_result(
    success: bool = True,
    confidence: float = 0.85,
    writable: bool = True,
    critical_conflict: bool = False,
    corrected_text: str = "民國113年3月15日",
    warnings: Optional[list] = None,
) -> Any:
    """Build a mock OCRConsensusResult dataclass-like object."""
    r = MagicMock()
    r.success = success
    r.confidence = confidence
    r.writable = writable
    r.critical_conflict = critical_conflict
    r.corrected_text = corrected_text
    r.selected_text = corrected_text
    r.warnings = warnings or []
    return r


# ---------------------------------------------------------------------------
# Helpers: mock InferenceGateway.dispatch
# ---------------------------------------------------------------------------

def _make_gateway_dispatch(date_str: Optional[str] = "2024-03-15"):
    """Return a mock dispatch callable that simulates gateway response."""
    def _dispatch(**kwargs):
        if date_str is None:
            return {"success": False, "error": "mock_failure"}
        return {
            "success": True,
            "analysis": date_str,
            "route": "mock",
            "degraded": False,
            "confidence": 0.9,
        }
    return _dispatch


# ---------------------------------------------------------------------------
# Fixture: import LAFVision with mocked InferenceGateway
# ---------------------------------------------------------------------------

@pytest.fixture()
def laf_vision_cls(tmp_path, monkeypatch):
    """
    Import LAFVision with all heavy dependencies mocked.
    Returns the LAFVision class (not an instance).
    """
    # Ensure the worktree root is in sys.path for imports
    wt_root = str(Path(__file__).parent.parent)
    if wt_root not in sys.path:
        sys.path.insert(0, wt_root)

    # Mock InferenceGateway before importing LAFVision
    mock_gw_mod = types.ModuleType("skills.bridge.inference_gateway")
    mock_gw_cls = MagicMock()
    mock_gw_mod.InferenceGateway = mock_gw_cls
    monkeypatch.setitem(sys.modules, "skills.bridge.inference_gateway", mock_gw_mod)

    # Also mock skills.bridge so sub-import works
    if "skills.bridge" not in sys.modules:
        bridge_mod = types.ModuleType("skills.bridge")
        monkeypatch.setitem(sys.modules, "skills.bridge", bridge_mod)

    # Remove any cached laf_vision module to force fresh import
    # Use monkeypatch.delitem so cleanup is automatic (avoids module pollution)
    for key in list(sys.modules.keys()):
        if "laf_vision" in key:
            monkeypatch.delitem(sys.modules, key, raising=False)

    # Add casper_ecosystem to path
    casper_root = Path(wt_root) / "casper_ecosystem" / "law_firm_orchestrators"
    if str(casper_root.parent) not in sys.path:
        sys.path.insert(0, str(casper_root.parent))

    # Import using spec to avoid package conflicts
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "laf_vision_test_import",
        str(casper_root / "laf_vision.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    return mod.LAFVision


# ---------------------------------------------------------------------------
# Test 1: extract_start_date() flag-off — pure legacy path
# ---------------------------------------------------------------------------

class TestExtractStartDateFlagOff:
    """When both flags are off, extract_start_date() must behave identically to the original."""

    def test_returns_date_string_when_legacy_succeeds(self, laf_vision_cls, monkeypatch):
        """flag off → returns string from legacy, not None."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        # Patch _extract_via_legacy to return a known date
        instance._extract_via_legacy = MagicMock(return_value="2024-03-15")

        result = instance.extract_start_date("/fake/image.png")
        assert result == "2024-03-15"
        instance._extract_via_legacy.assert_called_once_with("/fake/image.png")

    def test_returns_none_when_legacy_fails(self, laf_vision_cls, monkeypatch):
        """flag off → returns None when legacy returns None."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value=None)

        result = instance.extract_start_date("/fake/image.png")
        assert result is None

    def test_consensus_not_called_when_flag_off(self, laf_vision_cls, monkeypatch):
        """flag off → _run_consensus_ocr must NOT be called."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")
        instance._run_consensus_ocr = MagicMock()

        instance.extract_start_date("/fake/image.png")
        instance._run_consensus_ocr.assert_not_called()

    def test_return_type_is_str_or_none(self, laf_vision_cls, monkeypatch):
        """Return type must be Optional[str] — never a dict or other type."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2025-06-01")
        result = instance.extract_start_date("/fake/image.png")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Test 2: extract_start_date_with_metadata() flag-off — dict format + date parity
# ---------------------------------------------------------------------------

class TestExtractStartDateWithMetadataFlagOff:
    """extract_start_date_with_metadata() flag-off must return a valid schema dict."""

    def test_dict_schema_present(self, laf_vision_cls, monkeypatch):
        """Required keys must be present in returned dict."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-03-15")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        for key in ("success", "date", "confidence", "warnings", "provider_trace", "writable"):
            assert key in result, "Missing key: {}".format(key)

    def test_date_matches_legacy(self, laf_vision_cls, monkeypatch):
        """dict['date'] must equal what legacy returns, so extract_start_date() wrapper is equivalent."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        legacy_date = "2023-11-20"
        instance._extract_via_legacy = MagicMock(return_value=legacy_date)

        meta = instance.extract_start_date_with_metadata("/fake/image.png")
        simple = instance.extract_start_date("/fake/image.png")

        assert meta["date"] == legacy_date
        assert simple == legacy_date

    def test_mode_is_legacy_in_provider_trace(self, laf_vision_cls, monkeypatch):
        """provider_trace['mode'] must be 'legacy' when both flags off."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["provider_trace"].get("mode") == "legacy"

    def test_no_raw_text_in_result(self, laf_vision_cls, monkeypatch):
        """Result dict must NOT contain raw OCR text (only date strings allowed)."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        # Recursively check no key named 'raw_text', 'ocr_text', 'text' in top-level or provider_trace
        banned_keys = {"raw_text", "ocr_text", "selected_text", "corrected_text"}
        for k in banned_keys:
            assert k not in result, "Leaked key: {}".format(k)
            assert k not in result.get("provider_trace", {}), "Leaked key in trace: {}".format(k)


# ---------------------------------------------------------------------------
# Test 3: shadow mode — consensus called, result from legacy
# ---------------------------------------------------------------------------

class TestShadowMode:
    """shadow=1, enable=0 → consensus runs but legacy result is returned."""

    def test_shadow_calls_consensus(self, laf_vision_cls, monkeypatch):
        """In shadow mode, _run_consensus_ocr must be called."""
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-05-10")
        instance._run_consensus_ocr = MagicMock(return_value=_make_consensus_result())
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        instance._run_consensus_ocr.assert_called_once()

    def test_shadow_returns_legacy_date(self, laf_vision_cls, monkeypatch):
        """In shadow mode, date in result must come from legacy, not consensus text."""
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)

        instance = laf_vision_cls()
        legacy_date = "2024-05-10"
        instance._extract_via_legacy = MagicMock(return_value=legacy_date)
        consensus_result = _make_consensus_result(corrected_text="2023-01-01-DIFFERENT")
        instance._run_consensus_ocr = MagicMock(return_value=consensus_result)
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["date"] == legacy_date

    def test_shadow_mode_in_provider_trace(self, laf_vision_cls, monkeypatch):
        """provider_trace must indicate shadow mode."""
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-05-10")
        instance._run_consensus_ocr = MagicMock(return_value=_make_consensus_result())
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["provider_trace"].get("mode") == "shadow"

    def test_shadow_writes_metrics(self, laf_vision_cls, monkeypatch):
        """Shadow mode must call _write_consensus_metrics."""
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-05-10")
        instance._run_consensus_ocr = MagicMock(return_value=_make_consensus_result())
        instance._write_consensus_metrics = MagicMock()

        instance.extract_start_date_with_metadata("/fake/image.png")
        instance._write_consensus_metrics.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: enable + critical_conflict → date=None, writable=False, confidence=0.0
# ---------------------------------------------------------------------------

class TestEnableModeConflict:
    """enable=1 and consensus critical_conflict → reject date."""

    def test_critical_conflict_returns_none_date(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")
        conflict_result = _make_consensus_result(
            success=True,
            critical_conflict=True,
            confidence=0.4,
            writable=False,
            corrected_text="2023-06-01",
        )
        instance._run_consensus_ocr = MagicMock(return_value=conflict_result)
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["date"] is None

    def test_critical_conflict_writable_false(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")
        conflict_result = _make_consensus_result(
            success=True,
            critical_conflict=True,
            confidence=0.4,
            writable=False,
        )
        instance._run_consensus_ocr = MagicMock(return_value=conflict_result)
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["writable"] is False

    def test_critical_conflict_confidence_zero(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")
        conflict_result = _make_consensus_result(
            success=True,
            critical_conflict=True,
            confidence=0.4,
            writable=False,
        )
        instance._run_consensus_ocr = MagicMock(return_value=conflict_result)
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["confidence"] == 0.0

    def test_critical_conflict_success_false(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")
        conflict_result = _make_consensus_result(
            success=True,
            critical_conflict=True,
            confidence=0.4,
            writable=False,
        )
        instance._run_consensus_ocr = MagicMock(return_value=conflict_result)
        instance._write_consensus_metrics = MagicMock()

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Test 5: enable + no conflict → date has value, confidence ≥ 0.75, writable=True
# ---------------------------------------------------------------------------

class TestEnableModeSuccess:
    """enable=1 and no conflict → use consensus result, confidence >= 0.75."""

    def _make_good_result(self) -> Any:
        return _make_consensus_result(
            success=True,
            confidence=0.88,
            writable=True,
            critical_conflict=False,
            corrected_text="2025年3月15日律師受任",
        )

    def _patch_gateway(self, instance, date_str: str = "2025-03-15") -> None:
        """Mock gateway.dispatch to return a successful date extraction."""
        instance.gateway = MagicMock()
        instance.gateway.dispatch = MagicMock(
            return_value={
                "success": True,
                "analysis": date_str,
                "route": "mock_omlx",
                "degraded": False,
                "confidence": 0.92,
            }
        )

    def test_success_returns_date(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2025-03-15")
        instance._run_consensus_ocr = MagicMock(return_value=self._make_good_result())
        instance._write_consensus_metrics = MagicMock()
        self._patch_gateway(instance, "2025-03-15")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["success"] is True
        assert result["date"] is not None
        assert result["date"] == "2025-03-15"

    def test_success_confidence_gte_threshold(self, laf_vision_cls, monkeypatch):
        """confidence in result should match or be derived from consensus confidence."""
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2025-03-15")
        good_result = self._make_good_result()  # confidence=0.88
        instance._run_consensus_ocr = MagicMock(return_value=good_result)
        instance._write_consensus_metrics = MagicMock()
        self._patch_gateway(instance, "2025-03-15")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["confidence"] >= 0.75

    def test_success_writable_true(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2025-03-15")
        instance._run_consensus_ocr = MagicMock(return_value=self._make_good_result())
        instance._write_consensus_metrics = MagicMock()
        self._patch_gateway(instance, "2025-03-15")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["writable"] is True

    def test_success_mode_in_provider_trace(self, laf_vision_cls, monkeypatch):
        monkeypatch.setenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2025-03-15")
        instance._run_consensus_ocr = MagicMock(return_value=self._make_good_result())
        instance._write_consensus_metrics = MagicMock()
        self._patch_gateway(instance, "2025-03-15")

        result = instance.extract_start_date_with_metadata("/fake/image.png")
        assert result["provider_trace"].get("mode") == "enabled"


# ---------------------------------------------------------------------------
# Test 6: Three-module protection — no banned module-level imports
# ---------------------------------------------------------------------------

class TestThreeModuleProtection:
    """Ensure laf_vision.py does not import banned modules at module level."""

    def test_no_module_level_api_server_import(self):
        """laf_vision must not import api.server or api.tools_api at module level."""
        laf_vision_path = Path(__file__).parent.parent / (
            "casper_ecosystem/law_firm_orchestrators/laf_vision.py"
        )
        source = laf_vision_path.read_text(encoding="utf-8")

        # These must NOT appear as top-level (non-indented) imports
        banned_patterns = ["from api.server", "import api.server",
                           "from api.tools_api", "import api.tools_api",
                           "import daemon"]
        for pattern in banned_patterns:
            # Only check lines that are NOT inside a function/class (not indented)
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(pattern):
                    # If the original line is indented, it's inside a function — allowed
                    if not line.startswith(" ") and not line.startswith("\t"):
                        pytest.fail(
                            "Banned module-level import found in laf_vision.py: {!r}".format(line)
                        )

    def test_extract_start_date_return_type_is_optional_str(self, laf_vision_cls, monkeypatch):
        """extract_start_date() must always return str or None, never a dict."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value="2024-01-01")
        result = instance.extract_start_date("/fake/image.png")
        assert isinstance(result, (str, type(None)))

    def test_extract_start_date_returns_none_not_dict(self, laf_vision_cls, monkeypatch):
        """When legacy returns None, extract_start_date() must return None (not {})."""
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_ENABLE", raising=False)
        monkeypatch.delenv("MAGI_LAF_OCR_CONSENSUS_SHADOW", raising=False)

        instance = laf_vision_cls()
        instance._extract_via_legacy = MagicMock(return_value=None)
        result = instance.extract_start_date("/fake/image.png")
        assert result is None
        assert not isinstance(result, dict)
