"""Smoke tests for Apple Translation sidecar adapter.

These tests hit the real Swift sidecar when available and skip otherwise —
suitable for developer machines and the macOS-14+ CI runner, no-ops on any
Linux runner.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from skills.engine.apple_translation import (
    is_available,
    normalize_lang,
    translate,
)


def test_normalize_lang_aliases():
    assert normalize_lang("繁體中文") == "zh-Hant"
    assert normalize_lang("zh-TW") == "zh-Hant"
    assert normalize_lang("英文") == "en"
    assert normalize_lang("") == ""
    # Unknown passthrough
    assert normalize_lang("xx-YY") == "xx-YY"


def test_empty_input_rejected_without_sidecar():
    r = translate("", source_lang="zh-Hant", target_lang="en")
    assert r["success"] is False
    assert r["error"] == "empty_input"


@pytest.mark.skipif(not is_available()[0], reason="Apple Translation sidecar not built")
def test_short_zh_to_en_succeeds():
    r = translate("明天開庭", source_lang="zh-Hant", target_lang="en", timeout_sec=15)
    # We can't assert exact wording (Apple may tweak), but it should succeed
    # and produce English-ish output.
    assert r["success"] is True, r
    assert r["text"].strip()
    assert r["provider"] == "apple_translation"


@pytest.mark.skipif(not is_available()[0], reason="Apple Translation sidecar not built")
def test_round_trip_preserves_numbers():
    r = translate("原告應給付新臺幣200,000元", source_lang="zh-Hant", target_lang="en",
                  timeout_sec=15)
    assert r["success"] is True, r
    assert "200,000" in r["text"] or "200000" in r["text"]
