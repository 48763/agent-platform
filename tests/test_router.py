import pytest
from unittest.mock import AsyncMock, MagicMock
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
    router = Router(registry=reg, llm_fallback=None)
    agent = router.match_by_keyword("台北天氣如何")
    assert agent is not None
    assert agent.name == "weather"


def test_keyword_match_second_agent():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = router.match_by_keyword("幫我 review 這個 PR")
    assert agent is not None
    assert agent.name == "code-review"


def test_keyword_no_match():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = router.match_by_keyword("幫我訂機票")
    assert agent is None


@pytest.mark.asyncio
async def test_route_with_keyword():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = await router.route("今天天氣好嗎")
    assert agent.name == "weather"


@pytest.mark.asyncio
async def test_route_falls_back_to_llm():
    reg = make_registry_with_agents()

    async def mock_fallback(message, agents):
        return "weather"

    router = Router(registry=reg, llm_fallback=mock_fallback)
    agent = await router.route("會不會下雨啊")
    assert agent is not None
    assert agent.name == "weather"


@pytest.mark.asyncio
async def test_route_returns_none_when_no_match():
    reg = make_registry_with_agents()

    async def mock_fallback(message, agents):
        return None

    router = Router(registry=reg, llm_fallback=mock_fallback)
    agent = await router.route("幫我訂機票")
    assert agent is None
