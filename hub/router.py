import re
from core.models import AgentInfo
from hub.registry import AgentRegistry

FALLBACK_PRIORITY_THRESHOLD = 0


class Router:
    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def match_by_keyword(self, message: str) -> AgentInfo | None:
        for agent in self.registry.list_online():
            if agent.priority < FALLBACK_PRIORITY_THRESHOLD:
                continue
            for pattern in agent.route_patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    return agent
        return None
