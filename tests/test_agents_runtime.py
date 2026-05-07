from __future__ import annotations

from api.agents import AgentCoordinator, AgentRuntime, AgentSpec


def test_agent_runtime_responds_and_records_history():
    agent = AgentRuntime(
        spec=AgentSpec(name="alpha", role="analyst"),
        responder=lambda message, **ctx: f"reply:{message}:{ctx.get('tag', '')}",
    )

    response = agent.respond("hello", tag="x")

    assert response.agent_name == "alpha"
    assert response.content == "reply:hello:x"
    assert len(agent.history) == 1


def test_agent_coordinator_dispatches_to_agent_and_team():
    coordinator = AgentCoordinator(name="magi")
    alpha = AgentRuntime(spec=AgentSpec(name="alpha"), responder=lambda message, **ctx: f"alpha:{message}")
    beta = AgentRuntime(spec=AgentSpec(name="beta"), responder=lambda message, **ctx: f"beta:{message}")

    coordinator.register_agent(alpha)
    coordinator.register_agent(beta)
    team = coordinator.create_team("team-1", mission="triage")
    team.register_agent(alpha)
    team.register_agent(beta)

    direct = coordinator.dispatch("hello", agent="beta")
    team_result = coordinator.dispatch("world", team="team-1")

    assert direct.content == "beta:hello"
    assert team_result.content in {"alpha:world", "beta:world"}
