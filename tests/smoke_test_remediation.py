#!/usr/bin/env python3
"""Smoke tests for remediation fixes. No running services required."""

import sys
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

def test_vision_date_validation():
    """Test 1: Vision parser date validation logic."""
    sys.path.insert(0, f"{_MAGI_ROOT}/skills/pdf-namer")
    from vision_parser import _parse_date_from_text

    # Test that placeholder "YYYYMMDD" is NOT parsed as a valid date
    result = _parse_date_from_text("YYYYMMDD")
    assert result is None or result == "", (
        f"FAIL: placeholder 'YYYYMMDD' should not parse as valid date, got: {result}"
    )

    # Test that real date "20250904" IS parsed
    result = _parse_date_from_text("20250904")
    assert result and "2025" in result, (
        f"FAIL: real date '20250904' should parse, got: {result}"
    )

    print("PASS: Vision date validation logic")


def test_vision_prompt_guardrail():
    """Test 2: Vision guardrail - JSON format example does NOT use real sample values."""
    sys.path.insert(0, f"{_MAGI_ROOT}/skills/pdf-namer")
    import inspect
    from vision_parser import extract_info_with_vision

    source = inspect.getsource(extract_info_with_vision)
    # Real sample name must not appear anywhere in the function
    assert "\u5289\u4fe1\u7fa9" not in source, (
        "FAIL: prompt still contains real sample name '\u5289\u4fe1\u7fa9'"
    )
    # JSON format example must use fictional sender, not 花蓮地方檢察署
    # (花蓮地方檢察署 may appear in instruction hints listing example sender types — that's OK)
    # Check that the JSON example line uses the fictional value
    assert '"sender": "\u81fa\u5317\u5730\u65b9\u6cd5\u9662"' in source or '"sender":"\\u81fa\\u5317' in source, (
        "FAIL: JSON format example should use fictional sender '\u81fa\u5317\u5730\u65b9\u6cd5\u9662'"
    )
    assert '"name": "\u738b\u5c0f\u660e"' in source, (
        "FAIL: JSON format example should use fictional name '\u738b\u5c0f\u660e'"
    )
    print("PASS: Vision prompt examples use fictional values (not real samples)")


def test_obsidian_index_key_mapping():
    """Test 3: Obsidian index key mapping."""
    sys.path.insert(0, f"{_MAGI_ROOT}/skills/obsidian")

    with open(f"{_MAGI_ROOT}/skills/obsidian/action.py", "r") as f:
        src = f.read()

    # Check that the broken derivation pattern is gone
    assert "nc.replace(f'20_Notes/{source}/'" not in src, (
        "FAIL: old broken note-path derivation still present"
    )
    # Check that note_path_to_state or similar reverse mapping exists
    assert "note_path_to_state" in src or "note_path" in src, (
        "FAIL: no reverse mapping found"
    )
    print("PASS: Obsidian index key mapping fixed")


if __name__ == "__main__":
    tests = [
        ("Vision date validation", test_vision_date_validation),
        ("Vision prompt guardrail", test_vision_prompt_guardrail),
        ("Obsidian index key mapping", test_obsidian_index_key_mapping),
    ]

    results = {}
    for name, fn in tests:
        try:
            fn()
            results[name] = True
        except Exception as e:
            print(f"FAIL: {name} -- {e}")
            results[name] = False

    # Summary
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n{'='*40}")
    print(f"Summary: {passed}/{total} tests passed")
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
    if passed < total:
        sys.exit(1)
