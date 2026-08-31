from __future__ import annotations

from pathlib import Path

import pytest

from magi_v3.a2a_adapter import A2AAdapterError, A2AAdapterPolicy, create_proposal


def test_shipped_a2a_policy_is_disabled_proposal_only():
    policy = A2AAdapterPolicy.load(Path("config/a2a/adapter.json"))
    assert policy.enabled is False
    assert policy.mode == "proposal-only"
    assert policy.writer_access is False
    assert policy.federation_enabled is False
    with pytest.raises(A2AAdapterError, match="disabled"):
        create_proposal(
            policy,
            task_id="task-1",
            remote_url="https://agent.example.test/a2a",
            capability="research",
            payload_digest="a" * 64,
        )


def test_a2a_never_allows_writer_federation_or_whale():
    with pytest.raises(A2AAdapterError, match="writer"):
        A2AAdapterPolicy(enabled=True, writer_access=True)
    with pytest.raises(A2AAdapterError, match="federation"):
        A2AAdapterPolicy(enabled=True, federation_enabled=True)
    with pytest.raises(A2AAdapterError, match="WHALE"):
        A2AAdapterPolicy(enabled=True, allowed_remote_hosts=("100.64" + ".1.2",))


def test_enabled_future_adapter_can_only_create_non_dispatching_proposal():
    policy = A2AAdapterPolicy(enabled=True, allowed_remote_hosts=("agent.example.test",))
    proposal = create_proposal(
        policy,
        task_id="task-1",
        remote_url="https://agent.example.test/a2a",
        capability="read-only-research",
        payload_digest="b" * 64,
    )
    assert proposal["mode"] == "proposal-only"
    assert proposal["dispatch_performed"] is False
    assert proposal["writer_access"] is False
    assert proposal["federation_enabled"] is False
    assert len(proposal["receipt_sha256"]) == 64
