import time
from core.models import AgentInfo
from hub.registry import AgentRegistry


def test_register_and_get():
    reg = AgentRegistry(heartbeat_timeout=30)
    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    reg.register(info)
    assert reg.get("weather") is not None
    assert reg.get("weather").url == "http://localhost:8001"


def test_list_online():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    reg.register(AgentInfo(name="b", description="B", url="http://localhost:8002"))
    online = reg.list_online()
    assert len(online) == 2


def test_heartbeat_updates_time():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    old_time = reg._last_heartbeat["a"]
    time.sleep(0.01)
    reg.heartbeat("a")
    assert reg._last_heartbeat["a"] > old_time


def test_expired_agent_not_listed():
    reg = AgentRegistry(heartbeat_timeout=0)  # immediately expires
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    time.sleep(0.01)
    online = reg.list_online()
    assert len(online) == 0


def test_unregister():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    reg.unregister("a")
    assert reg.get("a") is None
