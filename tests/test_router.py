# tests/test_router.py
from core.models import AgentInfo
from hub.registry import AgentRegistry
from hub.router import Router


def make_registry_with_agents():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣|weather|氣溫"],
    ))
    reg.register(AgentInfo(
        name="code-review",
        description="Code review",
        url="http://localhost:8002",
        route_patterns=["review|PR|code review"],
    ))
    return reg


def test_keyword_match():
    reg = make_registry_with_agents()
    router = Router(registry=reg)
    agent = router.match_by_keyword("台北天氣如何")
    assert agent is not None
    assert agent.name == "weather"


def test_keyword_match_second_agent():
    reg = make_registry_with_agents()
    router = Router(registry=reg)
    agent = router.match_by_keyword("幫我 review 這個 PR")
    assert agent is not None
    assert agent.name == "code-review"


def test_keyword_no_match():
    reg = make_registry_with_agents()
    router = Router(registry=reg)
    agent = router.match_by_keyword("幫我訂機票")
    assert agent is None
