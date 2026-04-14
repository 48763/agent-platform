import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.llm import LLMClient


@pytest.mark.asyncio
async def test_simple_text_response():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "台北今天 25°C"
    mock_response.content = [mock_text_block]
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    llm = LLMClient(client=mock_client, model="claude-sonnet-4-20250514")
    result = await llm.run(
        system_prompt="你是助手",
        messages=[{"role": "user", "content": "天氣如何"}],
        tools_schema=[],
        tool_executor=None,
    )
    assert result == "台北今天 25°C"


@pytest.mark.asyncio
async def test_tool_call_loop():
    mock_client = MagicMock()

    # First response: tool call
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "tool_1"
    tool_block.name = "get_weather"
    tool_block.input = {"city": "Taipei"}

    first_response = MagicMock()
    first_response.stop_reason = "tool_use"
    first_response.content = [tool_block]

    # Second response: final text
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "台北晴天 25°C"

    second_response = MagicMock()
    second_response.stop_reason = "end_turn"
    second_response.content = [text_block]

    mock_client.messages.create = AsyncMock(
        side_effect=[first_response, second_response]
    )

    async def executor(name, args):
        assert name == "get_weather"
        return "Taipei: 25°C, sunny"

    llm = LLMClient(client=mock_client, model="claude-sonnet-4-20250514")
    result = await llm.run(
        system_prompt="你是助手",
        messages=[{"role": "user", "content": "台北天氣"}],
        tools_schema=[{"name": "get_weather", "description": "get weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
        tool_executor=executor,
    )
    assert result == "台北晴天 25°C"
    assert mock_client.messages.create.call_count == 2
