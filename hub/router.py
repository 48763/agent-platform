import re
from typing import Callable, Optional
from core.models import AgentInfo
from hub.registry import AgentRegistry

# Agents with negative priority are fallback-only (skipped in keyword matching)
FALLBACK_PRIORITY_THRESHOLD = 0


class Router:
    def __init__(
        self,
        registry: AgentRegistry,
        llm_fallback: Optional[Callable] = None,
    ):
        self.registry = registry
        self.llm_fallback = llm_fallback

    def match_by_keyword(self, message: str) -> AgentInfo | None:
        for agent in self.registry.list_online():
            if agent.priority < FALLBACK_PRIORITY_THRESHOLD:
                continue
            for pattern in agent.route_patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    return agent
        return None

    def _get_default_agent(self) -> AgentInfo | None:
        for agent in reversed(self.registry.list_online()):
            if agent.priority < FALLBACK_PRIORITY_THRESHOLD:
                return agent
        return None

    async def route(self, message: str) -> AgentInfo | None:
        # 1. Keyword match (skip fallback agents)
        agent = self.match_by_keyword(message)
        if agent is not None:
            return agent

        # 2. LLM fallback (Gemini CLI)
        if self.llm_fallback is not None:
            online = [a for a in self.registry.list_online()
                      if a.priority >= FALLBACK_PRIORITY_THRESHOLD]
            if online:
                agent_name = await self.llm_fallback(message, online)
                if agent_name:
                    return self.registry.get(agent_name)

        # 3. Default agent (negative priority, e.g. echo-agent)
        return self._get_default_agent()
