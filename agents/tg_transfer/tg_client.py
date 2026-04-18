import os
import logging
from telethon import TelegramClient

logger = logging.getLogger(__name__)


async def create_client(session_path: str) -> TelegramClient:
    """Create and start a Telethon client.

    Requires TELEGRAM_API_ID and TELEGRAM_API_HASH env vars.
    First run requires interactive login (phone + code).
    Subsequent runs use the persisted session file.
    """
    api_id_str = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")

    if not api_id_str or not api_hash:
        raise RuntimeError(
            "Telegram 未設定：請設定 TELEGRAM_API_ID 和 TELEGRAM_API_HASH"
        )

    client = TelegramClient(session_path, int(api_id_str), api_hash)
    await client.start()
    logger.info("Telethon client connected")
    return client
