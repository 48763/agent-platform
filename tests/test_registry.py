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


def test_register_error():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register_error("broken-agent", "LLM 不可用：找不到 gemini CLI")
    agents = reg.list_all()
    broken = [a for a in agents if a["name"] == "broken-agent"]
    assert len(broken) == 1
    assert broken[0]["status"] == "error"
    assert broken[0]["error"] == "LLM 不可用：找不到 gemini CLI"


def test_register_clears_error():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register_error("recover-agent", "some error")
    info = AgentInfo(name="recover-agent", description="test", url="http://localhost:8000")
    reg.register(info)
    agents = reg.list_all()
    agent = [a for a in agents if a["name"] == "recover-agent"][0]
    # No WS connection after registration, so status is offline (not error)
    assert agent["status"] == "offline"
    assert agent.get("error") is None


def test_error_agent_not_in_online_list():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register_error("err-agent", "broken")
    online = reg.list_online()
    names = [a.name for a in online]
    assert "err-agent" not in names


def test_unauthenticated_agent_not_routable():
    reg = AgentRegistry(heartbeat_timeout=30)
    info = AgentInfo(name="unauth-agent", description="test", url="http://localhost:8000")
    reg.register(info, auth_status="unauthenticated", auth_error="Claude 未認證")
    assert reg.get("unauth-agent") is None
    online = reg.list_online()
    assert len(online) == 0


def test_unauthenticated_shows_in_list_all():
    reg = AgentRegistry(heartbeat_timeout=30)
    info = AgentInfo(name="unauth-agent", description="test", url="http://localhost:8000")
    reg.register(info, auth_status="unauthenticated", auth_error="Gemini 未認證")
    agents = reg.list_all()
    agent = [a for a in agents if a["name"] == "unauth-agent"][0]
    assert agent["status"] == "unauthenticated"
    assert agent["error"] == "Gemini 未認證"


def test_reregister_authenticated_clears_unauth():
    reg = AgentRegistry(heartbeat_timeout=30)
    info = AgentInfo(name="recover", description="test", url="http://localhost:8000")
    reg.register(info, auth_status="unauthenticated", auth_error="not logged in")
    assert reg.get("recover") is None
    # Re-register without auth issue
    reg.register(info)
    assert reg.get("recover") is not None
