import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus


class EchoAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"Echo: {task.content}",
        )


async def main():
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    agent = EchoAgent(agent_dir=agent_dir, hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
