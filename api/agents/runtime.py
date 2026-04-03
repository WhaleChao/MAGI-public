from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from api.agents.models import AgentMessage, AgentResponse, AgentSpec, TeamSpec


Responder = Callable[[str], str | dict[str, Any] | AgentResponse]


@dataclass(slots=True)
class AgentRuntime:
    spec: AgentSpec
    responder: Responder | None = None
    history: deque[AgentMessage] = field(default_factory=lambda: deque(maxlen=200))

    def respond(self, message: str, **context: Any) -> AgentResponse:
        if self.responder is None:
            content = f"{self.spec.name}: {message}"
        else:
            content = self.responder(message, **context)
        if isinstance(content, AgentResponse):
            response = content
        elif isinstance(content, dict):
            response = AgentResponse(
                agent_name=self.spec.name,
                content=str(content.get("content") or content.get("text") or ""),
                metadata=dict(content.get("metadata") or {}),
            )
        else:
            response = AgentResponse(agent_name=self.spec.name, content=str(content))
        self.history.append(AgentMessage(sender=self.spec.name, content=response.content))
        return response


@dataclass(slots=True)
class TeamRuntime:
    spec: TeamSpec
    agents: dict[str, AgentRuntime] = field(default_factory=dict)
    _round_robin_index: int = 0

    def register_agent(self, agent: AgentRuntime) -> AgentRuntime:
        self.agents[agent.spec.name] = agent
        if agent.spec.name not in self.spec.members:
            self.spec.members.append(agent.spec.name)
        return agent

    def list_agents(self) -> list[str]:
        return list(self.agents.keys())

    def dispatch(self, message: str, target: str | None = None, **context: Any) -> AgentResponse:
        if target and target in self.agents:
            return self.agents[target].respond(message, **context)
        if not self.agents:
            raise KeyError("No agents registered")
        ordered = list(self.agents.values())
        agent = ordered[self._round_robin_index % len(ordered)]
        self._round_robin_index += 1
        return agent.respond(message, **context)


@dataclass(slots=True)
class AgentCoordinator:
    name: str = "magi"
    teams: dict[str, TeamRuntime] = field(default_factory=dict)
    agents: dict[str, AgentRuntime] = field(default_factory=dict)

    def register_agent(self, agent: AgentRuntime) -> AgentRuntime:
        self.agents[agent.spec.name] = agent
        return agent

    def create_team(self, name: str, mission: str = "", members: list[str] | None = None) -> TeamRuntime:
        team = TeamRuntime(spec=TeamSpec(name=name, mission=mission, members=list(members or [])))
        self.teams[name] = team
        return team

    def add_agent_to_team(self, team_name: str, agent: AgentRuntime) -> AgentRuntime:
        team = self.teams.get(team_name)
        if team is None:
            team = self.create_team(team_name)
        self.register_agent(agent)
        team.register_agent(agent)
        return agent

    def dispatch(self, message: str, *, team: str | None = None, agent: str | None = None, **context: Any) -> AgentResponse:
        if agent:
            runtime = self.agents.get(agent)
            if runtime is None:
                raise KeyError(f"Unknown agent: {agent}")
            return runtime.respond(message, **context)
        if team:
            team_runtime = self.teams.get(team)
            if team_runtime is None:
                raise KeyError(f"Unknown team: {team}")
            return team_runtime.dispatch(message, **context)
        if self.agents:
            first = next(iter(self.agents.values()))
            return first.respond(message, **context)
        raise KeyError("No agents registered")
