# tests/test_dispatch.py
import pytest
from unittest.mock import AsyncMock, patch
from hub.server import create_hub_app
from core.models import AgentInfo


@pytest.mark.asyncio
async def test_dispatch_routes_to_agent(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    # Register a mock agent
    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    await client.post("/register", json=info.to_dict())

    # Mock the HTTP call to the agent
    mock_result = {"status": "done", "message": "台北 25°C"}
    with patch("hub.server.send_task_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_result
        resp = await client.post("/dispatch", json={"message": "台北天氣", "chat_id": 0})

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "done"
    assert data["message"] == "台北 25°C"


@pytest.mark.asyncio
async def test_dispatch_no_agent(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    resp = await client.post("/dispatch", json={"message": "訂機票", "chat_id": 0})
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "error"
