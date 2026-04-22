# tests/test_hub_server.py
import pytest
from hub.server import create_hub_app
from hub.task_manager import TaskManager
from hub.ws_handler import _forward_progress_to_gateway
from core.models import AgentInfo


@pytest.mark.asyncio
async def test_register_agent(aiohttp_client, tmp_db):
    app = create_hub_app(db_path=tmp_db)
    client = await aiohttp_client(app)

    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    resp = await client.post("/register", json=info.to_dict())
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "registered"


@pytest.mark.asyncio
async def test_list_agents(aiohttp_client, tmp_db):
    app = create_hub_app(db_path=tmp_db)
    client = await aiohttp_client(app)

    info = AgentInfo(name="weather", description="查天氣", url="http://localhost:8001")
    await client.post("/register", json=info.to_dict())

    resp = await client.get("/agents")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "weather"


@pytest.mark.asyncio
async def test_progress_reopens_done_task(tmp_db):
    """An agent progress message for a 'done' task means the agent is actually
    working on it again (e.g. job resume after startup cleanup). Hub should
    flip the status back to 'working' so dashboard stats match reality."""
    tm = TaskManager(db_path=tmp_db)
    task = tm.create_task(agent_name="tg", chat_id=100, content="搬移")
    tm.complete_task(task["task_id"])
    assert tm.get_task(task["task_id"])["status"] == "done"

    app = {"task_manager": tm, "gateway_connections": []}
    await _forward_progress_to_gateway(app, {
        "task_id": task["task_id"],
        "message": "繼續搬移任務 job-abc",
    })

    refreshed = tm.get_task(task["task_id"])
    assert refreshed["status"] == "working"
    # Progress text still gets appended to history.
    assert refreshed["conversation_history"][-1]["content"] == "繼續搬移任務 job-abc"


@pytest.mark.asyncio
async def test_progress_leaves_closed_task_alone(tmp_db):
    """Closed is a user-intent terminal state — a stray agent progress must
    not resurrect it. The history append still happens so the stray event is
    visible for debugging, but status is preserved."""
    tm = TaskManager(db_path=tmp_db)
    task = tm.create_task(agent_name="tg", chat_id=100, content="搬移")
    tm.close_task(task["task_id"])

    app = {"task_manager": tm, "gateway_connections": []}
    await _forward_progress_to_gateway(app, {
        "task_id": task["task_id"],
        "message": "should not reopen",
    })

    assert tm.get_task(task["task_id"])["status"] == "closed"
