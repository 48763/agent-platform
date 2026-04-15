# agents/claude_code/__main__.py
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from agents.claude_code.cli_session import CLISession

logger = logging.getLogger(__name__)


class ClaudeCodeAgent(BaseAgent):
    def __init__(self, hub_url: str, port: int = 0, work_dir: str = "/workspace"):
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(agent_dir=agent_dir, hub_url=hub_url, port=port)
        self.work_dir = work_dir
        self.cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
        # Map task_id → active CLI session
        self._sessions: dict[str, CLISession] = {}

    async def handle_task(self, task: TaskRequest) -> AgentResult:
        task_id = task.task_id

        # Check if there's an existing session for this task (multi-turn)
        session = self._sessions.get(task_id)

        try:
            if session and session.is_running:
                # Continue existing session — send user's response
                user_input = task.conversation_history[-1]["content"] if task.conversation_history else task.content
                event = await session.send_input(user_input)
            else:
                # New session
                session = CLISession(work_dir=self.work_dir, cli_path=self.cli_path)
                self._sessions[task_id] = session
                event = await session.start(task.content)

            return self._event_to_result(task_id, event)

        except Exception as e:
            logger.error(f"Claude Code agent error: {e}")
            await self._cleanup_session(task_id)
            return AgentResult(status=TaskStatus.ERROR, message=f"執行失敗: {e}")

    def _event_to_result(self, task_id: str, event: dict) -> AgentResult:
        event_type = event.get("type")

        if event_type == "done":
            # Clean up session
            asyncio.create_task(self._cleanup_session(task_id))
            message = event.get("message", "任務完成")
            return AgentResult(status=TaskStatus.DONE, message=message)

        elif event_type == "need_approval":
            return AgentResult(
                status=TaskStatus.NEED_APPROVAL,
                message=event.get("message", "需要你的同意"),
                action=event.get("action", ""),
                options=["允許", "拒絕"],
            )

        elif event_type == "need_input":
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=event.get("message", "需要更多資訊"),
            )

        else:
            asyncio.create_task(self._cleanup_session(task_id))
            return AgentResult(
                status=TaskStatus.ERROR,
                message=event.get("message", "未知狀態"),
            )

    async def _cleanup_session(self, task_id: str):
        session = self._sessions.pop(task_id, None)
        if session:
            await session.kill()


async def main():
    logging.basicConfig(level=logging.INFO)
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))
    work_dir = os.environ.get("WORK_DIR", "/workspace")

    agent = ClaudeCodeAgent(hub_url=hub_url, port=port, work_dir=work_dir)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
