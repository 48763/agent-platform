# hub/gemini_fallback.py
import asyncio
import logging
import os
from pathlib import Path
from core.models import AgentInfo

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.environ.get("PROMPTS_DIR", "/data/prompts")
GEMINI_FAST_MODEL = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
GEMINI_DEFAULT_MODEL = os.environ.get("GEMINI_DEFAULT_MODEL", "gemini-2.5-pro")


def _load_prompt(filename: str, default: str) -> str:
    path = Path(PROMPTS_DIR) / filename
    if path.exists():
        return path.read_text().strip()
    return default


async def gemini_unified_route(
    message: str,
    active_tasks: list[dict],
    agents: list[AgentInfo],
) -> dict:
    """Single flash call to decide: CONTINUE task_id, ROUTE agent_name, or CHAT.

    Returns:
        {"action": "continue", "task_id": "xxx"}
        {"action": "route", "agent_name": "xxx"}
        {"action": "chat"}
        {"action": "error"}
    """
    # Build task summaries
    task_lines = []
    for t in active_tasks:
        history = t["conversation_history"]
        last_msg = history[-1]["content"] if history else ""
        agent = t["agent_name"]
        if agent == "_hub":
            agent = "Hub 閒聊"
        task_lines.append(f"- {t['task_id']}: [{agent}] 最後訊息: {last_msg[:80]}")
    tasks_text = "\n".join(task_lines) if task_lines else "（無）"

    # Build agent list
    agent_lines = [f"- {a.name}: {a.description}" for a in agents]
    agents_text = "\n".join(agent_lines) if agent_lines else "（無）"

    template = _load_prompt("gemini_unified_router.txt", (
        "你是一個訊息路由器。根據使用者的新訊息，判斷該如何處理。\n"
        "只回覆一行，格式如下：\n"
        "- 如果是接續某個進行中的對話：CONTINUE <task_id>\n"
        "- 如果是新任務要交給某個 agent：ROUTE <agent_name>\n"
        "- 如果都不是（閒聊、一般問題）：CHAT\n\n"
        "進行中的對話：\n{tasks_text}\n\n"
        "可用的 agent：\n{agents_text}\n\n"
        "使用者新訊息：{message}\n\n"
        "只回覆一行，不要解釋。"
    ))
    prompt = template.format(
        tasks_text=tasks_text,
        agents_text=agents_text,
        message=message,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt, "-m", GEMINI_FAST_MODEL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.error(f"Gemini unified route failed: {stderr.decode()}")

        result = stdout.decode().strip()
        logger.info(f"Gemini unified route result: {result}")
        return _parse_route_result(result, active_tasks, agents)

    except asyncio.TimeoutError:
        logger.error("Gemini unified route timed out")
        return {"action": "error"}
    except Exception:
        logger.exception("Gemini unified route error")
        return {"action": "error"}


def _parse_route_result(result: str, tasks: list[dict], agents: list[AgentInfo]) -> dict:
    result = result.strip()

    if result.startswith("CONTINUE"):
        parts = result.split(maxsplit=1)
        if len(parts) == 2:
            task_id = parts[1].strip()
            valid_ids = {t["task_id"] for t in tasks}
            if task_id in valid_ids:
                return {"action": "continue", "task_id": task_id}
            # Try partial match
            for tid in valid_ids:
                if tid in task_id or task_id in tid:
                    return {"action": "continue", "task_id": tid}
        # If we have tasks but couldn't parse, default to most recent
        if tasks:
            return {"action": "continue", "task_id": tasks[0]["task_id"]}

    elif result.startswith("ROUTE"):
        parts = result.split(maxsplit=1)
        if len(parts) == 2:
            agent_name = parts[1].strip()
            valid_names = {a.name for a in agents}
            if agent_name in valid_names:
                return {"action": "route", "agent_name": agent_name}
            for name in valid_names:
                if name in agent_name or agent_name in name:
                    return {"action": "route", "agent_name": name}

    elif "CHAT" in result.upper():
        return {"action": "chat"}

    # Default to chat if unparseable
    return {"action": "chat"}


class GeminiChat:
    """Long-running Gemini CLI process for Hub chat replies."""

    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        """Start the Gemini interactive process."""
        prompt_file = Path(PROMPTS_DIR) / "gemini_default_reply.txt"
        system_prompt = None
        if prompt_file.exists():
            system_prompt = prompt_file.read_text().strip()

        cmd = ["gemini", "-m", GEMINI_DEFAULT_MODEL]
        if system_prompt:
            cmd.extend(["-i", system_prompt])

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("GeminiChat process started")

    async def reply(self, message: str) -> str | None:
        """Send a message and get reply. Falls back to -p mode if interactive fails."""
        # Use -p mode for reliability (interactive mode output parsing is fragile)
        template = _load_prompt("gemini_default_reply.txt", (
            "你是一個通用助手。請用繁體中文簡潔回覆以下使用者訊息。\n\n"
            "使用者訊息: {message}"
        ))
        prompt = template.format(message=message)

        try:
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", prompt, "-m", GEMINI_DEFAULT_MODEL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                logger.error(f"GeminiChat reply failed: {stderr.decode()}")
            result = stdout.decode().strip()
            return result if result else None
        except Exception:
            logger.exception("GeminiChat reply error")
            return None

    async def reply_with_context(self, conversation_history: list[dict]) -> str | None:
        """Reply with conversation context."""
        context = "\n".join(
            f"{'使用者' if m['role'] == 'user' else '助手'}: {m['content']}"
            for m in conversation_history[-8:]
        )
        template = _load_prompt("gemini_default_reply.txt", (
            "你是一個通用助手。請用繁體中文簡潔回覆。\n\n"
            "對話紀錄：\n{message}"
        ))
        prompt = template.format(message=context)

        try:
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", prompt, "-m", GEMINI_DEFAULT_MODEL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            result = stdout.decode().strip()
            return result if result else None
        except Exception:
            logger.exception("GeminiChat context reply error")
            return None
