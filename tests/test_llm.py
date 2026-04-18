import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.llm import parse_llm_config, LLMClient, ClaudeProvider, GeminiProvider


class TestParseLLMConfig:
    def test_string_claude(self):
        provider, model = parse_llm_config({"llm": "claude"})
        assert provider == "claude"
        assert "claude" in model

    def test_string_gemini(self):
        provider, model = parse_llm_config({"llm": "gemini"})
        assert provider == "gemini"
        assert "gemini" in model

    def test_dict_with_model(self):
        provider, model = parse_llm_config({"llm": {"provider": "claude", "model": "claude-haiku-4-5-20251001"}})
        assert provider == "claude"
        assert model == "claude-haiku-4-5-20251001"

    def test_dict_without_model_uses_default(self):
        provider, model = parse_llm_config({"llm": {"provider": "gemini"}})
        assert provider == "gemini"
        assert "gemini" in model

    def test_no_llm_config(self):
        provider, model = parse_llm_config({})
        assert provider is None
        assert model is None

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            parse_llm_config({"llm": "openai"})


class TestClaudeProvider:
    @pytest.mark.asyncio
    async def test_prompt(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hello")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        provider = ClaudeProvider(client=mock_client, model="test-model")
        result = await provider.prompt("say hi")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_run_agentic_loop(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="done")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        provider = ClaudeProvider(client=mock_client, model="test-model")
        result = await provider.run(
            system_prompt="test", messages=[{"role": "user", "content": "hi"}],
            tools_schema=[], tool_executor=None,
        )
        assert result == "done"


class TestGeminiProvider:
    @pytest.mark.asyncio
    async def test_prompt(self):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"hello world", b""))
            proc.returncode = 0
            mock_exec.return_value = proc

            provider = GeminiProvider(model="gemini-2.5-flash")
            result = await provider.prompt("say hi")
            assert result == "hello world"

    @pytest.mark.asyncio
    async def test_run_raises_not_implemented(self):
        provider = GeminiProvider(model="gemini-2.5-flash")
        with pytest.raises(NotImplementedError):
            await provider.run(
                system_prompt="test", messages=[], tools_schema=[], tool_executor=None,
            )


class TestLLMClient:
    @pytest.mark.asyncio
    async def test_prompt_delegates_to_provider(self):
        mock_provider = AsyncMock()
        mock_provider.prompt = AsyncMock(return_value="result")
        client = LLMClient(provider=mock_provider)
        result = await client.prompt("test")
        assert result == "result"
