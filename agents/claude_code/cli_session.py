# agents/claude_code/cli_session.py
import asyncio
import json
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class CLISession:
    """Manages a long-running Claude Code CLI process with JSON streaming."""

    def __init__(self, work_dir: str, cli_path: str = "claude"):
        self.work_dir = work_dir
        self.cli_path = cli_path
        self.process: asyncio.subprocess.Process | None = None
        self._buffer = ""

    async def start(self, prompt: str) -> dict:
        """Start a new CLI session with initial prompt, return first meaningful event."""
        self.process = await asyncio.create_subprocess_exec(
            self.cli_path,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.work_dir,
        )
        return await self._read_until_actionable()

    async def send_input(self, user_input: str) -> dict:
        """Send user input to the running CLI process."""
        if self.process is None or self.process.stdin is None:
            return {"type": "error", "message": "CLI process not running"}

        self.process.stdin.write(f"{user_input}\n".encode())
        await self.process.stdin.drain()
        return await self._read_until_actionable()

    async def _read_until_actionable(self) -> dict:
        """Read JSON events until we find one that needs user attention or task is done."""
        assistant_messages = []

        while True:
            line = await self._read_line()
            if line is None:
                # Process ended
                if assistant_messages:
                    return {
                        "type": "done",
                        "message": "\n".join(assistant_messages),
                    }
                return {"type": "done", "message": "任務完成"}

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            if event_type == "assistant":
                # Extract text from assistant message
                msg = event.get("message", {})
                content = msg.get("content", [])
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            assistant_messages.append(text)

            elif event_type == "tool_use":
                # Tool being used — check if it needs approval
                tool_name = event.get("name", "")
                tool_input = event.get("input", {})
                logger.info(f"Tool use: {tool_name}")

            elif event_type == "tool_result":
                # Tool completed
                pass

            elif event_type == "permission_request":
                # CLI is asking for permission — this is what we forward to TG
                tool_name = event.get("tool_name", "unknown")
                description = event.get("description", "")
                return {
                    "type": "need_approval",
                    "message": f"Claude 想要執行: {tool_name}\n{description}",
                    "action": tool_name,
                }

            elif event_type == "result":
                result_text = event.get("result", "")
                if assistant_messages and not result_text:
                    result_text = "\n".join(assistant_messages)
                return {
                    "type": "done",
                    "message": result_text or "任務完成",
                    "cost_usd": event.get("total_cost_usd"),
                }

    async def _read_line(self) -> str | None:
        """Read a single line from stdout, return None if process ended."""
        if self.process is None or self.process.stdout is None:
            return None

        try:
            line = await asyncio.wait_for(
                self.process.stdout.readline(),
                timeout=300,  # 5 min max wait
            )
            if not line:
                return None
            return line.decode().strip()
        except asyncio.TimeoutError:
            logger.warning("CLI read timed out")
            await self.kill()
            return None

    async def kill(self):
        """Kill the CLI process."""
        if self.process:
            try:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
            self.process = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None
