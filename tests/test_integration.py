# tests/test_integration.py
import pytest
import asyncio
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from unittest.mock import AsyncMock, patch
from hub.server import create_hub_app
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus


class EchoAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        return AgentResult(status=TaskStatus.DONE, message=f"echo: {task.content}")


@pytest.mark.asyncio
async def test_full_flow_hub_to_agent(tmp_path):
    """Test: Hub receives message -> routes to agent -> returns result"""

    # 1. Create agent config
    agent_dir = tmp_path / "echo"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: echo-agent\n"
        "description: Echo messages back\n"
        "route_patterns:\n"
        "  - 'echo|測試'\n"
        "sandbox:\n"
        "  allowed_dirs: []\n"
    )

    # 2. Start hub
    hub_app = create_hub_app()
    hub_server = TestServer(hub_app)
    await hub_server.start_server()
    hub_url = f"http://localhost:{hub_server.port}"

    # 3. Start echo agent
    agent = EchoAgent(agent_dir=str(agent_dir), hub_url=hub_url, port=0)
    agent_app = agent.create_app()
    agent_server = TestServer(agent_app)
    await agent_server.start_server()
    agent_url = f"http://localhost:{agent_server.port}"

    try:
        # 4. Register agent with hub
        async with ClientSession() as session:
            await session.post(f"{hub_url}/register", json={
                "name": "echo-agent",
                "description": "Echo messages back",
                "url": agent_url,
                "route_patterns": ["echo|測試"],
            })

            # 5. Dispatch message through hub
            async with session.post(f"{hub_url}/dispatch", json={
                "message": "echo hello",
                "chat_id": 1,
            }) as resp:
                result = await resp.json()

        assert result["status"] == "done"
        assert result["message"] == "echo: echo hello"
    finally:
        await hub_server.close()
        await agent_server.close()


@pytest.mark.asyncio
async def test_multi_turn_flow(tmp_path):
    """Test: Agent asks for input -> user responds -> agent completes"""

    class AskAgent(BaseAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._call_count = 0

        async def handle_task(self, task: TaskRequest) -> AgentResult:
            self._call_count += 1
            if self._call_count == 1:
                return AgentResult(
                    status=TaskStatus.NEED_INPUT,
                    message="哪個城市？",
                )
            last_msg = task.conversation_history[-1]["content"]
            return AgentResult(
                status=TaskStatus.DONE,
                message=f"天氣: {last_msg} 25°C",
            )

    agent_dir = tmp_path / "ask"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: ask-agent\n"
        "description: Asks then answers\n"
        "route_patterns:\n"
        "  - '天氣'\n"
        "sandbox:\n"
        "  allowed_dirs: []\n"
    )

    hub_app = create_hub_app()
    hub_server = TestServer(hub_app)
    await hub_server.start_server()
    hub_url = f"http://localhost:{hub_server.port}"

    agent = AskAgent(agent_dir=str(agent_dir), hub_url=hub_url, port=0)
    agent_app = agent.create_app()
    agent_server = TestServer(agent_app)
    await agent_server.start_server()
    agent_url = f"http://localhost:{agent_server.port}"

    try:
        async with ClientSession() as session:
            # Register
            await session.post(f"{hub_url}/register", json={
                "name": "ask-agent",
                "description": "Asks then answers",
                "url": agent_url,
                "route_patterns": ["天氣"],
            })

            # First message
            async with session.post(f"{hub_url}/dispatch", json={
                "message": "查天氣", "chat_id": 42,
            }) as resp:
                r1 = await resp.json()
            assert r1["status"] == "need_input"
            assert r1["message"] == "哪個城市？"

            # User responds
            async with session.post(f"{hub_url}/dispatch", json={
                "message": "台北", "chat_id": 42,
            }) as resp:
                r2 = await resp.json()
            assert r2["status"] == "done"
            assert "台北" in r2["message"]
    finally:
        await hub_server.close()
        await agent_server.close()
