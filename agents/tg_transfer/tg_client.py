import os
import logging
from telethon import TelegramClient

logger = logging.getLogger(__name__)


async def create_client(session_path: str) -> TelegramClient:
    """Create and start a Telethon client.

    Requires env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH.
    First run requires interactive login (phone + code).
    Subsequent runs use the persisted session file.
    """
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    logger.info("Telethon client connected")
    return client
