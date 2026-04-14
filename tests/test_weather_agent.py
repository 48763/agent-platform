# tests/test_weather_agent.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agents.weather.tools import get_weather


@pytest.mark.asyncio
async def test_get_weather_returns_info():
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="Weather: Taipei\n25°C\nSunny")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("agents.weather.tools.ClientSession", return_value=mock_session):
        result = await get_weather("Taipei")
    assert "Taipei" in result or "25" in result


@pytest.mark.asyncio
async def test_get_weather_handles_error():
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.text = AsyncMock(return_value="error")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("agents.weather.tools.ClientSession", return_value=mock_session):
        result = await get_weather("Taipei")
    assert "錯誤" in result or "error" in result.lower()
