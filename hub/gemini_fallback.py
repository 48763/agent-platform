# hub/gemini_fallback.py
import asyncio
import json
import logging
from core.models import AgentInfo

logger = logging.getLogger(__name__)


async def gemini_route(message: str, agents: list[AgentInfo]) -> str | None:
    """Use Gemini CLI to determine which agent should handle a message."""
    agent_descriptions = "\n".join(
        f"- {a.name}: {a.description}" for a in agents
    )

    prompt = (
        f"根據以下使用者訊息，從可用的 agent 中選擇最適合處理的一個。\n"
        f"只回覆 agent 的 name，不要其他文字。\n"
        f"如果沒有適合的 agent，回覆 NONE。\n\n"
        f"可用的 agent:\n{agent_descriptions}\n\n"
        f"使用者訊息: {message}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = stdout.decode().strip()

        if result == "NONE" or not result:
            return None

        # Verify the returned name is a valid agent
        valid_names = {a.name for a in agents}
        if result in valid_names:
            return result

        # Try to find a partial match
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
