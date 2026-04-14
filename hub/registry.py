import time
from core.models import AgentInfo


class AgentRegistry:
    def __init__(self, heartbeat_timeout: int = 30):
        self._agents: dict[str, AgentInfo] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._heartbeat_timeout = heartbeat_timeout

    def register(self, info: AgentInfo) -> None:
        self._agents[info.name] = info
        self._last_heartbeat[info.name] = time.time()

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)
        self._last_heartbeat.pop(name, None)

    def heartbeat(self, name: str) -> bool:
        if name not in self._agents:
            return False
        self._last_heartbeat[name] = time.time()
        return True

    def get(self, name: str) -> AgentInfo | None:
        if name not in self._agents:
            return None
        if not self._is_alive(name):
            return None
        return self._agents[name]

    def list_online(self) -> list[AgentInfo]:
        return [info for name, info in self._agents.items() if self._is_alive(name)]

    def _is_alive(self, name: str) -> bool:
        last = self._last_heartbeat.get(name, 0)
        return (time.time() - last) < self._heartbeat_timeout
