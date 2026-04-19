# tests/test_dispatch.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from hub.server import create_hub_app
from core.models import AgentInfo


@pytest.mark.asyncio
async def test_dispatch_routes_to_agent_no_ws(aiohttp_client, tmp_db):
    """When agent has no WS connection, dispatch returns 'Agent 已離線'."""
    app = create_hub_app(db_path=tmp_db, use_gemini_fallback=False)
    client = await aiohttp_client(app)

    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    await client.post("/register", json=info.to_dict())

    resp = await client.post("/dispatch", json={"message": "台北天氣", "chat_id": 0})
    assert resp.status == 200
    data = await resp.json()
    # Agent registered but no WS connection → offline
    assert data["status"] == "error"
    assert "離線" in data["message"]


@pytest.mark.asyncio
async def test_dispatch_no_agent(aiohttp_client, tmp_db):
    app = create_hub_app(db_path=tmp_db, use_gemini_fallback=False)
    client = await aiohttp_client(app)

    resp = await client.post("/dispatch", json={"message": "訂機票", "chat_id": 0})
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "error"
