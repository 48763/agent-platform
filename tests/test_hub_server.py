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
async def test_task_statuses_endpoint(aiohttp_client, tmp_db):
    """Agent polls this after WS reconnect to filter out closed tasks before
    re-spawning resume jobs. Missing task_ids come back as 'missing' so the
    agent can treat them the same as closed (don't resume)."""
    app = create_hub_app(db_path=tmp_db)
    client = await aiohttp_client(app)

    tm: TaskManager = app["task_manager"]
    t_open = tm.create_task(agent_name="tg", chat_id=1, content="a")
    t_closed = tm.create_task(agent_name="tg", chat_id=1, content="b")
    tm.close_task(t_closed["task_id"])

    resp = await client.post("/task_statuses", json={
        "task_ids": [t_open["task_id"], t_closed["task_id"], "nope"],
    })
    assert resp.status == 200
    data = await resp.json()
    assert data["statuses"][t_open["task_id"]] == "working"
    assert data["statuses"][t_closed["task_id"]] == "closed"
    assert data["statuses"]["nope"] == "missing"


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
async def test_progress_on_closed_task_cancels_agent_and_drops_message(tmp_db):
    """Closed is a user-intent terminal state. If an agent still sends progress
    (usually because it restarted while the user had closed the task out of
    band, so the earlier CANCEL never landed), hub must:
      - keep status 'closed'
      - NOT append the progress to history (no ghost '繼續搬移' lines)
      - NOT fan out to gateway (no ghost Telegram message)
      - send CANCEL back to the agent so it stops the underlying job
    Otherwise the user sees '繼續任務' appear on a task they already closed
    and can't tell whether work is actually still happening."""
    tm = TaskManager(db_path=tmp_db)
    task = tm.create_task(agent_name="tg", chat_id=100, content="搬移")
    tm.close_task(task["task_id"])

    sent = []
    class _FakeWS:
        async def send_str(self, s):
            sent.append(s)
    class _FakeRegistry:
        def get_ws(self, name):
            assert name == "tg"
            return _FakeWS()

    app = {
        "task_manager": tm,
        "gateway_connections": [],
        "registry": _FakeRegistry(),
    }
    await _forward_progress_to_gateway(app, {
        "task_id": task["task_id"],
        "message": "繼續搬移任務 job-abc",
    })

    refreshed = tm.get_task(task["task_id"])
    assert refreshed["status"] == "closed"
    # No history append — the ghost line must not show on the dashboard.
    assert all(
        "繼續搬移任務" not in m.get("content", "")
        for m in refreshed["conversation_history"]
    )
    # Exactly one CANCEL sent to the agent.
    assert len(sent) == 1
    import json as _json
    parsed = _json.loads(sent[0])
    assert parsed["type"] == "cancel"
    assert parsed["task_id"] == task["task_id"]


@pytest.mark.asyncio
async def test_handle_task_delete_sends_task_deleted_to_agent(tmp_path):
    """Deleting a conversation must push TASK_DELETED to the bound agent
    so the agent can rmtree its task-scoped cache and DB rows.
    TASK_DELETED is sent unconditionally (regardless of task status);
    CANCEL is only sent for in-flight states."""
    import json
    from aiohttp.test_utils import make_mocked_request
    from hub.task_manager import TaskManager
    from hub.dashboard import handle_task_delete

    tm = TaskManager(db_path=str(tmp_path / "tasks.db"))
    task = tm.create_task(
        agent_name="tg_transfer", chat_id=12345, content="batch x to y",
    )

    sent = []

    class FakeWS:
        async def send_str(self, s):
            sent.append(s)

    class FakeRegistry:
        def get_ws(self, name):
            return FakeWS()

    app = {"task_manager": tm, "registry": FakeRegistry()}

    request = make_mocked_request(
        "POST", f"/dashboard/task/{task['task_id']}/delete",
        match_info={"task_id": task["task_id"]},
        app=app,
    )
    resp = await handle_task_delete(request)
    assert resp.status == 200

    decoded = [json.loads(s) for s in sent]
    assert any(
        m.get("type") == "task_deleted" and m.get("task_id") == task["task_id"]
        for m in decoded
    ), f"TASK_DELETED not in sent: {decoded}"
