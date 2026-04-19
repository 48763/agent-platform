# tests/test_base_agent.py
import pytest
from aiohttp import web
from core.base_agent import BaseAgent
from core.models import TaskRequest, TaskStatus, AgentResult


class FakeAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest):
        return AgentResult(status=TaskStatus.DONE, message=f"echo: {task.content}")


@pytest.fixture
def agent(tmp_path):
    agent_yaml = tmp_path / "agent.yaml"
    agent_yaml.write_text(
        "name: test-agent\n"
        "description: test\n"
        "route_patterns: ['test']\n"
        "sandbox:\n"
        "  allowed_dirs: []\n"
        "  writable: false\n"
    )
    return FakeAgent(agent_dir=str(tmp_path), hub_url="http://localhost:9000", port=0)


def test_agent_loads_config(agent):
    assert agent.name == "test-agent"
    assert agent.config["description"] == "test"


@pytest.mark.asyncio
async def test_agent_health_endpoint(aiohttp_client, agent):
    app = agent.create_app()
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "test-agent"


@pytest.mark.asyncio
async def test_handle_ws_task(agent):
    """Test _handle_ws_task processes task and sends result via WS."""
    sent_messages = []

    class FakeWS:
        closed = False
        async def send_str(self, msg):
            sent_messages.append(msg)

    agent._ws = FakeWS()

    await agent._handle_ws_task({
        "task_id": "t1",
        "content": "hello",
        "conversation_history": [],
        "chat_id": 42,
    })

    assert len(sent_messages) == 1
    import json
    result = json.loads(sent_messages[0])
    assert result["type"] == "result"
    assert result["task_id"] == "t1"
    assert result["status"] == "done"
    assert result["message"] == "echo: hello"


@pytest.mark.asyncio
async def test_handle_ws_task_error(agent):
    """Test _handle_ws_task handles exceptions gracefully."""
    async def bad_handle(task):
        raise ValueError("boom")

    agent.handle_task = bad_handle
    sent_messages = []

    class FakeWS:
        closed = False
        async def send_str(self, msg):
            sent_messages.append(msg)

    agent._ws = FakeWS()

    await agent._handle_ws_task({
        "task_id": "t2",
        "content": "fail",
    })

    import json
    result = json.loads(sent_messages[0])
    assert result["type"] == "result"
    assert result["status"] == "error"
    assert "boom" in result["message"]


def test_is_cancelled(agent):
    assert not agent.is_cancelled("t1")
    agent._cancelled_tasks.add("t1")
    assert agent.is_cancelled("t1")


@pytest.mark.asyncio
async def test_ws_send_progress(agent):
    sent_messages = []

    class FakeWS:
        closed = False
        async def send_str(self, msg):
            sent_messages.append(msg)

    agent._ws = FakeWS()

    await agent.ws_send_progress("t1", 42, "working...")

    import json
    result = json.loads(sent_messages[0])
    assert result["type"] == "progress"
    assert result["task_id"] == "t1"
    assert result["chat_id"] == 42
    assert result["message"] == "working..."


@pytest.mark.asyncio
async def test_ws_send_result_no_ws(agent):
    """ws_send_result should not raise when WS is None."""
    result = AgentResult(status=TaskStatus.DONE, message="ok")
    await agent.ws_send_result("t1", result)  # should not raise
