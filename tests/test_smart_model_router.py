from __future__ import annotations

from api.model_router import ModelSpec, ResourceView, choose_model_for_request


def _resource(
    *,
    level: str = "normal",
    disk: float = 120,
    swap: float = 2,
    free: float = 12,
) -> ResourceView:
    return ResourceView(
        ok=True,
        level=level,
        disk_free_gb=disk,
        swap_used_gb=swap,
        free_plus_inactive_gb=free,
        memory_free_percent=50,
    )


def _registry() -> dict[str, ModelSpec]:
    return {
        "gemma-4-e4b-it-4bit": ModelSpec(
            id="gemma-4-e4b-it-4bit",
            provider="omlx",
            origin="google",
            tier="stable_local",
            tasks=("general", "summary", "translate", "legal_analysis", "tc_review", "vision"),
            safe_context_tokens=8192,
        ),
        "gemma-4-26b-a4b-it-4bit": ModelSpec(
            id="gemma-4-26b-a4b-it-4bit",
            provider="omlx",
            origin="google",
            tier="heavy_local_moe",
            tasks=("summary", "translate", "legal_analysis"),
            safe_context_tokens=8192,
            gates={
                "min_disk_free_gb": 70,
                "min_free_plus_inactive_gb": 8,
                "max_swap_used_gb": 20,
                "allowed_resource_levels": ["normal"],
                "require_model_live": True,
            },
        ),
    }


def test_quality_task_uses_26b_when_live_and_safe(monkeypatch):
    monkeypatch.setenv("MAGI_ROUTER_26B_MIN_DISK_GB", "70")
    decision = choose_model_for_request(
        task_type="legal_analysis",
        prompt="法律分析" * 2000,
        force_quality=True,
        active_models=("gemma-4-26b-a4b-it-4bit",),
        resource=_resource(),
        registry=_registry(),
    )
    assert decision.selected_model == "gemma-4-26b-a4b-it-4bit"
    assert decision.tier == "heavy_local_moe"
    assert not decision.blocked_reasons


def test_quality_task_falls_back_when_disk_is_low(monkeypatch):
    monkeypatch.setenv("MAGI_ROUTER_26B_MIN_DISK_GB", "70")
    decision = choose_model_for_request(
        task_type="translate",
        prompt="翻譯" * 2000,
        force_quality=True,
        active_models=("gemma-4-26b-a4b-it-4bit", "gemma-4-e4b-it-4bit"),
        resource=_resource(level="throttle", disk=44, swap=3, free=10),
        registry=_registry(),
    )
    assert decision.selected_model == "gemma-4-e4b-it-4bit"
    assert "disk_free<70GB" in decision.blocked_reasons
    assert "resource_level=throttle" in decision.blocked_reasons
    assert decision.should_queue is True


def test_quality_task_falls_back_when_26b_is_not_live():
    decision = choose_model_for_request(
        task_type="summary",
        prompt="摘要" * 2000,
        force_quality=True,
        active_models=("gemma-4-e4b-it-4bit",),
        resource=_resource(),
        registry=_registry(),
    )
    assert decision.selected_model == "gemma-4-e4b-it-4bit"
    assert "26b_not_live" in decision.blocked_reasons


def test_routine_task_stays_on_e4b_even_when_26b_is_live():
    decision = choose_model_for_request(
        task_type="general",
        prompt="你好",
        active_models=("gemma-4-26b-a4b-it-4bit", "gemma-4-e4b-it-4bit"),
        resource=_resource(),
        registry=_registry(),
    )
    assert decision.selected_model == "gemma-4-e4b-it-4bit"
    assert decision.tier == "stable_local"


def test_embedding_keeps_embedding_model():
    decision = choose_model_for_request(
        task_type="embedding",
        prompt="檢索",
        active_models=("gemma-4-e4b-it-4bit",),
        resource=_resource(),
        registry=_registry(),
    )
    assert decision.selected_model == "modernbert-embed-4bit"


def test_china_model_request_is_not_selected():
    decision = choose_model_for_request(
        task_type="general",
        prompt="測試",
        requested_model="qwen2.5-32b-instruct",
        active_models=("qwen2.5-32b-instruct", "gemma-4-e4b-it-4bit"),
        resource=_resource(),
        registry=_registry(),
    )
    assert "qwen" not in decision.selected_model.lower()
    assert decision.selected_model == "gemma-4-e4b-it-4bit"
