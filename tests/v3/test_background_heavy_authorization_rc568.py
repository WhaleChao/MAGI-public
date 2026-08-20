from __future__ import annotations

import json

from skills.bridge import nim_heavy


def _contract(*, model: str = nim_heavy.NIM_HEAVY_TARGET_MODEL, **overrides):
    contract = {
        "job_id": "nightly-judgment-summary",
        "task_type": "judgment_summary",
        "source_class": "public_judgment",
        "provider": "nvidia_nim",
        "model": model,
        "daily_budget": 3,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    contract.update(overrides)
    return contract


def _prepare(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")
    # Keep this contract test independent from earlier dotenv-loading tests.
    monkeypatch.setenv("NVIDIA_NIM_MODEL", nim_heavy.NIM_HEAVY_TARGET_MODEL)
    monkeypatch.setattr(nim_heavy, "_get_daily_count", lambda: 0)
    monkeypatch.setattr(nim_heavy, "_incr_daily_count", lambda: 1)
    monkeypatch.setattr(nim_heavy, "_cb_can_call", lambda: (True, ""))
    monkeypatch.setattr(nim_heavy, "_log_usage", lambda _payload: None)


def test_background_heavy_requires_complete_job_bound_contract(monkeypatch):
    _prepare(monkeypatch)
    called = []
    monkeypatch.setattr(nim_heavy.requests, "post", lambda *args, **kwargs: called.append(True))
    result = nim_heavy.run_nim_chat(
        prompt="公開裁判理由",
        task_type="judgment_summary",
        data_classification="public_judgment",
        privacy_profile="public_judgment",
        heavy=True,
    )
    assert result["error"] == "background_heavy_authorization_missing"
    assert called == []


def test_background_heavy_rejects_mismatched_or_expired_contract(monkeypatch):
    _prepare(monkeypatch)
    result = nim_heavy.run_nim_chat(
        prompt="公開裁判理由",
        task_type="judgment_summary",
        data_classification="public_judgment",
        privacy_profile="public_judgment",
        heavy=True,
        background_heavy_authorized=_contract(task_type="batch_summary"),
    )
    assert result["error"] == "background_heavy_authorization_task_mismatch"

    result = nim_heavy.run_nim_chat(
        prompt="公開裁判理由",
        task_type="judgment_summary",
        data_classification="public_judgment",
        privacy_profile="public_judgment",
        heavy=True,
        background_heavy_authorized=_contract(expires_at="2000-01-01T00:00:00+00:00"),
    )
    assert result["error"] == "background_heavy_authorization_expired"


def test_background_heavy_uses_only_named_env_contract(monkeypatch):
    contract = _contract()
    monkeypatch.setenv("NVIDIA_NIM_BACKGROUND_AUTHORIZATIONS", json.dumps({"nightly-judgment-summary": contract}))
    assert nim_heavy.background_heavy_authorization("nightly-judgment-summary") == contract
    assert nim_heavy.background_heavy_authorization("other-job") is None


def test_credential_guard_blocks_cookie_bearer_and_oauth_without_blocking_case_number(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")
    for credential in (
        "Cookie: session=abcdef123456",
        "Authorization: Bearer abcdef123456",
        "oauth_token=abcdef123456",
    ):
        result = nim_heavy.run_nim_chat(prompt=credential, task_type="general")
        assert result["error"] == "credential_blocked"
    assert nim_heavy._contains_credentials("王大明，115年度訴字第123號") is False
