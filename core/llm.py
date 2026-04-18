import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.5-flash",
}


class LLMInitError(Exception):
    """Raised when LLM provider fails to initialize."""
    pass


def parse_llm_config(settings: dict) -> tuple[Optional[str], Optional[str]]:
    """Parse llm config from agent.yaml settings. Returns (provider, model)."""
    llm = settings.get("llm")
    if llm is None:
        return None, None
    if isinstance(llm, str):
        if llm not in DEFAULT_MODELS:
            raise ValueError(f"Unknown LLM provider: {llm}. Supported: {list(DEFAULT_MODELS.keys())}")
        return llm, DEFAULT_MODELS[llm]
    if isinstance(llm, dict):
        provider = llm["provider"]
        if provider not in DEFAULT_MODELS:
            raise ValueError(f"Unknown LLM provider: {provider}. Supported: {list(DEFAULT_MODELS.keys())}")
        model = llm.get("model", DEFAULT_MODELS[provider])
        return provider, model
    return None, None


class ClaudeProvider:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    async def prompt(self, text: str) -> str:
        """Single prompt -> response."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": text}],
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    async def run(
        self,
        system_prompt: str,
        messages: list[dict],
        tools_schema: list[dict],
        tool_executor: Optional[Callable],
        max_iterations: int = 20,
    ) -> str:
        """Agentic loop with tool calling."""
        messages = list(messages)
        for _ in range(max_iterations):
            kwargs = {
                "model": self.model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": messages,
            }
            if tools_schema:
                kwargs["tools"] = tools_schema
            response = await self.client.messages.create(**kwargs)
            if response.stop_reason == "tool_use":
                tool_calls = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tc in tool_calls:
                    result = await tool_executor(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": str(result),
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                for block in response.content:
                    if block.type == "text":
                        return block.text
                return ""
        return "Error: max iterations reached"


class GeminiProvider:
    def __init__(self, model: str):
        self.model = model

    async def prompt(self, text: str) -> str:
        """Single prompt -> response via Gemini CLI."""
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", text, "-m", self.model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.error(f"Gemini CLI error: {stderr.decode()}")
        return stdout.decode().strip()

    async def run(self, system_prompt, messages, tools_schema, tool_executor, max_iterations=20):
        """Not supported for Gemini CLI."""
        raise NotImplementedError("Gemini provider does not support agentic loop with tool calling")


class LLMClient:
    def __init__(self, provider: ClaudeProvider | GeminiProvider):
        self.provider = provider

    async def prompt(self, text: str) -> str:
        return await self.provider.prompt(text)

    async def run(self, system_prompt, messages, tools_schema, tool_executor, max_iterations=20) -> str:
        return await self.provider.run(
            system_prompt, messages, tools_schema, tool_executor, max_iterations,
        )


async def check_llm_auth(settings: dict) -> tuple[bool, str]:
    """Check if LLM is authenticated. Returns (ok, error_message).
    Checks API key env vars and CLI login status.
    """
    provider_name, model = parse_llm_config(settings)
    if provider_name is None:
        return True, ""  # No LLM configured, not an error

    if provider_name == "claude":
        import os
        # Check API key
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True, ""
        # Check CLI login (claude auth status)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                return True, ""
        except Exception:
            pass
        return False, "Claude 未認證：請設定 ANTHROPIC_API_KEY 或進入容器執行 claude auth login"

    if provider_name == "gemini":
        import os
        # Check API key
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return True, ""
        # Check CLI login
        try:
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", "ping", "-m", model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                return True, ""
            return False, f"Gemini 未認證：請設定 GEMINI_API_KEY 或進入容器執行 gemini auth login"
        except Exception:
            return False, "Gemini CLI 不可用：找不到 gemini 指令或認證失敗"

    return False, f"Unknown provider: {provider_name}"


async def create_llm_client(settings: dict) -> LLMClient:
    """Factory: create LLMClient from agent.yaml settings. Raises LLMInitError on failure."""
    provider_name, model = parse_llm_config(settings)
    if provider_name is None:
        raise LLMInitError("No LLM configured in agent.yaml settings")

    if provider_name == "claude":
        try:
            import anthropic
            import os
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMInitError("Claude API key 未設定：請設定 ANTHROPIC_API_KEY 環境變數")
            client = anthropic.AsyncAnthropic()
            return LLMClient(provider=ClaudeProvider(client=client, model=model))
        except ImportError:
            raise LLMInitError("anthropic SDK 未安裝")
        except LLMInitError:
            raise
        except Exception as e:
            raise LLMInitError(f"Claude 初始化失敗：{e}")

    if provider_name == "gemini":
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", "gemini",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                raise LLMInitError("Gemini CLI 未安裝：找不到 gemini 指令")
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", "ping", "-m", model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                raise LLMInitError(f"Gemini CLI 測試失敗：{stderr.decode().strip()}")
            return LLMClient(provider=GeminiProvider(model=model))
        except LLMInitError:
            raise
        except Exception as e:
            raise LLMInitError(f"Gemini 初始化失敗：{e}")

    raise LLMInitError(f"Unknown provider: {provider_name}")
