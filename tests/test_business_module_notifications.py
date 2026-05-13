from __future__ import annotations

import sys
import types
import pytest


def test_business_live_check_notifies_system_topic(monkeypatch):
    from scripts.ops import business_module_live_check

    calls = []

    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *args, **kwargs: calls.append((args, kwargs)) or {"telegram": True}
    )
    monkeypatch.setenv("MAGI_BUSINESS_LIVE_CHECK_NOTIFY", "1")
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)

    business_module_live_check._notify("📋 業務三模組 LIVE/健康檢查\n✅ laf_portal_live: 二階段 0")

    assert calls
    assert calls[0][1]["topic_key"] == "check"
    assert calls[0][1]["source"] == "business_module_live_check"


def test_business_live_check_help_does_not_run_live_checks(monkeypatch):
    from scripts.ops import business_module_live_check

    monkeypatch.setattr(
        business_module_live_check,
        "_laf_portal_live",
        lambda: pytest.fail("--help should not start LAF portal live scan"),
    )

    with pytest.raises(SystemExit) as exc:
        business_module_live_check.main(["--help"])

    assert exc.value.code == 0


def test_business_live_check_accepts_json_compat_flag():
    from scripts.ops import business_module_live_check

    args = business_module_live_check._parse_args(["--json", "--skip-laf-live"])

    assert args.json is True
    assert args.skip_laf_live is True
