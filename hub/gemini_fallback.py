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


async def gemini_route(message: str, agents: list[AgentInfo]) -> str | None:
    """Use Gemini CLI to determine which agent should handle a message."""
    agent_descriptions = "\n".join(
        f"- {a.name}: {a.description}" for a in agents
    )

    template = _load_prompt("gemini_router.txt", (
        "根據以下使用者訊息，從可用的 agent 中選擇最適合處理的一個。\n"
        "只回覆 agent 的 name，不要其他文字。\n"
        "如果沒有適合的 agent，回覆 NONE。\n\n"
        "可用的 agent:\n{agent_descriptions}\n\n"
        "使用者訊息: {message}"
    ))
    prompt = template.format(agent_descriptions=agent_descriptions, message=message)

    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt, "-m", GEMINI_FAST_MODEL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.error(f"Gemini route failed (exit {proc.returncode}): {stderr.decode()}")
        result = stdout.decode().strip()

        if result == "NONE" or not result:
            return None

        valid_names = {a.name for a in agents}
        if result in valid_names:
            return result

        for name in valid_names:
            if name in result:
                return name

        logger.warning(f"Gemini returned unknown agent: {result}")
        return None

    except asyncio.TimeoutError:
        logger.error("Gemini CLI timed out")
        return None
    except FileNotFoundError:
        logger.error("Gemini CLI not found")
        return None
    except Exception as e:
        logger.error(f"Gemini fallback error: {e}")
        return None


async def gemini_is_continuation(message: str, last_topic: str) -> bool:
    """Use Gemini flash to determine if a message continues the previous topic."""
    prompt = (
        f"判斷以下新訊息是否在接續上一個話題。只回覆 YES 或 NO。\n\n"
        f"上一個話題摘要: {last_topic}\n"
        f"新訊息: {message}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt, "-m", GEMINI_FAST_MODEL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        result = stdout.decode().strip().upper()
        return "YES" in result
    except Exception:
        logger.exception("Gemini continuation check error")
        return False


async def gemini_default_reply(message: str) -> str | None:
    """Use Gemini CLI to directly reply when no agent can handle the message."""
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
            logger.error(f"Gemini default reply failed (exit {proc.returncode}): {stderr.decode()}")
        result = stdout.decode().strip()
        return result if result else None

    except Exception:
        logger.exception("Gemini default reply error")
        return None
