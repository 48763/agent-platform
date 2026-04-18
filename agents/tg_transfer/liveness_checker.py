import asyncio
import logging
from telethon import TelegramClient
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.chat_resolver import resolve_chat

logger = logging.getLogger(__name__)


async def check_batch(
    client: TelegramClient, media_db: MediaDB, media_list: list[dict]
) -> tuple[int, int]:
    """Check a batch of media for liveness. Returns (deleted_count, checked_count)."""
    deleted = 0
    checked = 0
    for media in media_list:
        try:
            entity = await resolve_chat(client, media["target_chat"])
            msg = await client.get_messages(entity, ids=media["target_msg_id"])
            if msg is None:
                await media_db.delete_media(media["media_id"])
                deleted += 1
                logger.info(f"Media {media['media_id']} dead, deleted")
            else:
                await media_db.update_last_checked(media["media_id"])
            checked += 1
        except Exception as e:
            logger.error(f"Liveness check failed for media {media['media_id']}: {e}")
            checked += 1
    return deleted, checked


async def run_liveness_loop(
    client: TelegramClient, media_db: MediaDB, interval_hours: int = 24
):
    """Background loop that periodically checks media liveness."""
    interval_secs = interval_hours * 3600
    while True:
        try:
            stale = await media_db.get_stale_media(max_age_hours=interval_hours, limit=50)
            if stale:
                deleted, checked = await check_batch(client, media_db, stale)
                logger.info(f"Liveness check: {checked} checked, {deleted} deleted")
        except Exception as e:
            logger.error(f"Liveness loop error: {e}")
        await asyncio.sleep(interval_secs)
