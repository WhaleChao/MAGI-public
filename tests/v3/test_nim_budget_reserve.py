from __future__ import annotations


def _base_env(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-synthetic")
    monkeypatch.setenv("NVIDIA_NIM_DAILY_BUDGET", "500")
    monkeypatch.setenv("NVIDIA_NIM_INTERACTIVE_RESERVE", "25")


def test_background_nim_stops_before_interactive_reserve(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    _base_env(monkeypatch)
    monkeypatch.setattr(nim_heavy, "_get_daily_count", lambda: 475)
    result = nim_heavy.run_nim_chat(
        prompt="synthetic",
        task_type="judgment_summary",
        data_classification="synthetic",
    )

    assert result["success"] is False
    assert str(result["error"]).startswith("nim_background_budget_reserved:475/475")


def test_interactive_nim_can_use_reserved_capacity(monkeypatch) -> None:
    from skills.bridge import nim_heavy

    _base_env(monkeypatch)
    monkeypatch.setattr(nim_heavy, "_get_daily_count", lambda: 475)
    monkeypatch.setattr(nim_heavy, "_cb_can_call", lambda: (False, "synthetic_stop_after_budget_gate"))
    result = nim_heavy.run_nim_chat(
        prompt="synthetic",
        task_type="legal_drafting",
        data_classification="synthetic",
    )

    assert result["success"] is False
    assert result["error"] == "synthetic_stop_after_budget_gate"
