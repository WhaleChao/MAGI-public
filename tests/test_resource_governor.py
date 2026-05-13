from __future__ import annotations

from scripts.ops import resource_governor as rg


def test_import_does_not_load_memory_watchdog():
    assert not hasattr(rg, "memory_watchdog")


def test_classify_normal_resources():
    snap = rg.ResourceSnapshot(
        disk_free_gb=80,
        disk_total_gb=460,
        swap_used_gb=2,
        free_gb=4,
        inactive_gb=6,
        free_plus_inactive_gb=10,
    )
    decision = rg.classify(snap)
    assert decision.ok is True
    assert decision.level == "normal"
    assert decision.actions == []


def test_classify_core_only_when_disk_low():
    snap = rg.ResourceSnapshot(
        disk_free_gb=16,
        disk_total_gb=460,
        swap_used_gb=1,
        free_gb=3,
        inactive_gb=4,
        free_plus_inactive_gb=7,
    )
    decision = rg.classify(snap)
    assert decision.ok is True
    assert decision.level == "core_only"
    assert "business_core_only" in decision.actions
    assert "require_manual_confirmation_for_26b" in decision.actions


def test_classify_critical_when_swap_too_high():
    snap = rg.ResourceSnapshot(
        disk_free_gb=40,
        disk_total_gb=460,
        swap_used_gb=31,
        free_gb=3,
        inactive_gb=4,
        free_plus_inactive_gb=7,
    )
    decision = rg.classify(snap)
    assert decision.ok is False
    assert decision.level == "critical"
    assert "do_not_start_26b" in decision.actions


def test_classify_ignores_stale_swap_when_memory_pressure_is_healthy():
    snap = rg.ResourceSnapshot(
        disk_free_gb=52,
        disk_total_gb=460,
        swap_used_gb=22,
        free_gb=0.5,
        inactive_gb=4.5,
        free_plus_inactive_gb=5,
        memory_free_percent=39,
    )
    decision = rg.classify(snap)
    assert decision.ok is True
    assert decision.level == "normal"
    assert decision.actions == []


def test_classify_ignores_low_inactive_when_memory_pressure_is_healthy():
    snap = rg.ResourceSnapshot(
        disk_free_gb=52,
        disk_total_gb=460,
        swap_used_gb=22,
        free_gb=0.2,
        inactive_gb=3.4,
        free_plus_inactive_gb=3.6,
        memory_free_percent=34,
    )
    decision = rg.classify(snap)
    assert decision.ok is True
    assert decision.level == "normal"


def test_prepare_switch_records_failure_after_cleanup(monkeypatch, tmp_path):
    before = rg.ResourceSnapshot(16, 460, 20, 1, 1, 2)
    after = rg.ResourceSnapshot(16, 460, 20, 1, 1.5, 2.5)
    calls = {"n": 0}

    def fake_collect(path=rg.MAGI_ROOT):
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(rg, "collect_snapshot", fake_collect)
    monkeypatch.setattr(rg, "safe_cleanup", lambda enforce: [{"name": "safe_cleanup", "result": "nothing_to_clean"}])

    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))

    payload = rg.prepare_switch("DAY", 4, enforce=True)
    assert payload["ok"] is False
    assert payload["required_free_gb"] == 4


def test_json_flag_works_before_and_after_subcommand(monkeypatch, capsys):
    snap = rg.ResourceSnapshot(80, 460, 1, 5, 5, 10)
    monkeypatch.setattr(rg, "collect_snapshot", lambda: snap)
    monkeypatch.setattr(rg, "append_metric", lambda decision: None)

    assert rg.main(["--json", "status"]) == 0
    before = capsys.readouterr().out
    assert '"level": "normal"' in before

    assert rg.main(["status", "--json"]) == 0
    after = capsys.readouterr().out
    assert '"level": "normal"' in after


def test_prepare_switch_accepts_json_after_subcommand(monkeypatch, capsys):
    monkeypatch.setattr(
        rg,
        "prepare_switch",
        lambda mode, required_free_gb, enforce: {
            "ok": True,
            "mode": mode,
            "required_free_gb": required_free_gb,
            "cleanup_actions": [],
        },
    )

    assert rg.main(["prepare-switch", "--mode", "DAY", "--required-free-gb", "4", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"mode": "DAY"' in out
