# tests/test_base_agent.py
import pytest
from aiohttp import web
from core.base_agent import BaseAgent
from core.models import TaskRequest, TaskStatus


class FakeAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest):
        from core.models import AgentResult
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
async def test_agent_task_endpoint(aiohttp_client, agent):
    app = agent.create_app()
    client = await aiohttp_client(app)

    task = TaskRequest(task_id="t1", content="hello")
    resp = await client.post("/task", json=task.to_dict())
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "done"
    assert data["message"] == "echo: hello"


@pytest.mark.asyncio
async def test_agent_health_endpoint(aiohttp_client, agent):
    app = agent.create_app()
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "test-agent"
