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
    # Premium flag drives chunk-size tuning for faster up/download.
    # Attach to client so downstream components (TransferEngine) can branch
    # without needing to call get_me() again.
    try:
        me = await client.get_me()
        client.premium_account = bool(getattr(me, "premium", False))
    except Exception as e:
        logger.warning(f"Premium check failed, assuming non-premium: {e}")
        client.premium_account = False
    logger.info(
        f"Telethon client connected (premium={client.premium_account})"
    )
    return client
