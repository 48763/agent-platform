import re
from typing import Callable, Optional
from core.models import AgentInfo
from hub.registry import AgentRegistry


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
            for pattern in agent.route_patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    return agent
        return None

    async def route(self, message: str) -> AgentInfo | None:
        # Try keyword match first
        agent = self.match_by_keyword(message)
        if agent is not None:
            return agent

        # Fall back to LLM
        if self.llm_fallback is not None:
            online = self.registry.list_online()
            if not online:
                return None
            agent_name = await self.llm_fallback(message, online)
            if agent_name:
                return self.registry.get(agent_name)

        return None
