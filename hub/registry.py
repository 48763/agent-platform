import time
from core.models import AgentInfo


class AgentRegistry:
    def __init__(self, heartbeat_timeout: int = 30):
        self._agents: dict[str, AgentInfo] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._registered_at: dict[str, float] = {}
        self._disabled: set[str] = set()
        self._stats: dict[str, dict] = {}  # name → {tasks, success, errors, total_ms}
        self._errors: dict[str, str] = {}  # name → error message
        self._unauthenticated: dict[str, str] = {}  # name → auth error message
        self._heartbeat_timeout = heartbeat_timeout

    def register(self, info: AgentInfo, auth_status: str = None, auth_error: str = None) -> None:
        self._agents[info.name] = info
        self._last_heartbeat[info.name] = time.time()
        self._errors.pop(info.name, None)  # Clear any previous error
        if auth_status == "unauthenticated":
            self._unauthenticated[info.name] = auth_error or "LLM 未認證"
        else:
            self._unauthenticated.pop(info.name, None)
        if info.name not in self._registered_at:
            self._registered_at[info.name] = time.time()
        if info.name not in self._stats:
            self._stats[info.name] = {"tasks": 0, "success": 0, "errors": 0, "total_ms": 0}

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
        if name in self._disabled:
            return None
        if name in self._unauthenticated:
            return None
        return self._agents[name]

    def list_online(self) -> list[AgentInfo]:
        online = [
            info for name, info in self._agents.items()
            if self._is_alive(name) and name not in self._disabled and name not in self._unauthenticated
        ]
        return sorted(online, key=lambda a: a.priority, reverse=False)

    def list_all(self) -> list[dict]:
        """List all agents with rich status info."""
        now = time.time()
        result = []
        for name, info in self._agents.items():
            alive = self._is_alive(name)
            disabled = name in self._disabled
            last_hb = self._last_heartbeat.get(name, 0)
            reg_at = self._registered_at.get(name, 0)
            stats = self._stats.get(name, {"tasks": 0, "success": 0, "errors": 0, "total_ms": 0})
            avg_ms = round(stats["total_ms"] / stats["tasks"]) if stats["tasks"] > 0 else 0

            if name in self._errors:
                status = "error"
            elif name in self._unauthenticated:
                status = "unauthenticated"
            elif disabled:
                status = "disabled"
            elif alive:
                status = "online"
            else:
                status = "offline"

            result.append({
                **info.to_dict(),
                "status": status,
                "error": self._errors.get(name) or self._unauthenticated.get(name),
                "last_heartbeat": last_hb,
                "registered_at": reg_at,
                "uptime_seconds": round(now - reg_at) if reg_at else 0,
                "stats": {
                    "total_tasks": stats["tasks"],
                    "success": stats["success"],
                    "errors": stats["errors"],
                    "avg_response_ms": avg_ms,
                },
            })
        # Add agents that only have error state (never successfully registered)
        for name, error in self._errors.items():
            if name not in self._agents:
                result.append({
                    "name": name,
                    "description": "",
                    "url": "",
                    "route_patterns": [],
                    "capabilities": [],
                    "priority": 0,
                    "status": "error",
                    "error": error,
                    "last_heartbeat": 0,
                    "registered_at": 0,
                    "uptime_seconds": 0,
                    "stats": {"total_tasks": 0, "success": 0, "errors": 0, "avg_response_ms": 0},
                })
        return sorted(result, key=lambda a: a.get("priority", 0), reverse=False)

    def record_task_result(self, name: str, success: bool, duration_ms: int = 0):
        if name not in self._stats:
            self._stats[name] = {"tasks": 0, "success": 0, "errors": 0, "total_ms": 0}
        self._stats[name]["tasks"] += 1
        if success:
            self._stats[name]["success"] += 1
        else:
            self._stats[name]["errors"] += 1
        self._stats[name]["total_ms"] += duration_ms

    def disable(self, name: str):
        self._disabled.add(name)

    def enable(self, name: str):
        self._disabled.discard(name)

    def register_error(self, name: str, error: str) -> None:
        """Record an agent that failed to start."""
        self._errors[name] = error

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def _is_alive(self, name: str) -> bool:
        last = self._last_heartbeat.get(name, 0)
        return (time.time() - last) < self._heartbeat_timeout
