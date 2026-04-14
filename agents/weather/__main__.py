import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from core.llm import LLMClient
from core.tool_registry import collect_tools, tools_to_schema
from agents.weather import tools as weather_tools
from agents.weather.prompts import SYSTEM_PROMPT
import anthropic


class WeatherAgent(BaseAgent):
    def __init__(self, hub_url: str, port: int = 0):
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(agent_dir=agent_dir, hub_url=hub_url, port=port)

        client = anthropic.AsyncAnthropic()
        self.llm = LLMClient(client=client, model="claude-sonnet-4-20250514")
        self.tools = collect_tools(weather_tools)
        self.tools_schema = tools_to_schema(self.tools)
        self._tool_map = {t._tool_meta["name"]: t for t in self.tools}

    async def handle_task(self, task: TaskRequest) -> AgentResult:
        async def execute_tool(name: str, args: dict) -> str:
            func = self._tool_map.get(name)
            if func is None:
                return f"Unknown tool: {name}"
            return await func(**args)

        try:
            result = await self.llm.run(
                system_prompt=SYSTEM_PROMPT,
                messages=task.conversation_history or [{"role": "user", "content": task.content}],
                tools_schema=self.tools_schema,
                tool_executor=execute_tool,
            )
            return AgentResult(status=TaskStatus.DONE, message=result)
        except Exception as e:
            return AgentResult(status=TaskStatus.ERROR, message=f"處理失敗: {e}")


async def main():
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))
    agent = WeatherAgent(hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
