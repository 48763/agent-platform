# tests/test_cli.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from hub.cli import dispatch_message


@pytest.mark.asyncio
async def test_dispatch_to_agent():
    mock_router = AsyncMock()
    mock_router.route = AsyncMock(return_value=MagicMock(
        name="weather",
        url="http://localhost:8001",
    ))

    mock_response = {"status": "done", "message": "台北 25°C 晴天"}

    with patch("hub.cli.send_task_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_response
        result = await dispatch_message(
            message="台北天氣",
            router=mock_router,
            task_manager=MagicMock(),
        )
    assert result["message"] == "台北 25°C 晴天"


@pytest.mark.asyncio
async def test_dispatch_no_agent_found():
    mock_router = AsyncMock()
    mock_router.route = AsyncMock(return_value=None)

    result = await dispatch_message(
        message="幫我訂機票",
        router=mock_router,
        task_manager=MagicMock(),
    )
    assert result["status"] == "error"
    assert "無法處理" in result["message"]
