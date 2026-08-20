from __future__ import annotations

from api.model_router import ResourceView, choose_model_for_request


def test_quality_task_without_heavy_opt_in_queues_local_deep_when_26b_not_live():
    decision = choose_model_for_request(
        task_type="legal_analysis", prompt="分析爭點", active_models=("gemma-4-e4b-it-4bit",), resource=ResourceView(level="normal")
    )
    assert decision.local_deep_queue is True
    assert decision.should_queue is True
    assert decision.provider == "omlx"
    assert decision.cloud_heavy is False
    assert "request-thread switch" in decision.reason


def test_quality_task_uses_live_26b_locally_without_heavy_opt_in():
    decision = choose_model_for_request(
        task_type="legal_analysis", prompt="分析爭點", active_models=("gemma-4-26b-a4b-it-4bit",), resource=ResourceView(level="normal", disk_free_gb=100, free_plus_inactive_gb=20, swap_used_gb=0)
    )
    assert decision.local_deep_queue is False
    assert decision.provider == "omlx"
    assert "26b" in decision.selected_model.lower()
