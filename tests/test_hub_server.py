# tests/test_hub_server.py
import pytest
from hub.server import create_hub_app
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
async def test_heartbeat(aiohttp_client, tmp_db):
    app = create_hub_app(db_path=tmp_db)
    client = await aiohttp_client(app)

    info = AgentInfo(name="weather", description="查天氣", url="http://localhost:8001")
    await client.post("/register", json=info.to_dict())

    resp = await client.post("/heartbeat", json={"name": "weather"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_heartbeat_unknown_agent(aiohttp_client, tmp_db):
    app = create_hub_app(db_path=tmp_db)
    client = await aiohttp_client(app)

    resp = await client.post("/heartbeat", json={"name": "nonexistent"})
    assert resp.status == 404


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
