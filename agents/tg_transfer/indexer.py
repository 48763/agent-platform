"""Target-chat indexer.

Builds a thumb-level index over a target TG chat so later transfers from a
new source can skip files already in target without downloading them. Only
the TG-attached thumbnail is fetched (small, no quota pressure) — full
sha256/phash stays unknown until a dedup hit is cross-validated later.

Progress/resume model:
  - last scanned msg_id is stored in TransferDB config as
    `last_scanned_msg_id_<chat>`.
  - scan always asks iter_messages for `id > last_scanned_msg_id`, so
    interrupted scans simply pick up where they stopped and no rescan is
    needed for incremental syncs.
  - For chats with > 1000 messages, a caller-supplied async callback is
    fired every 10% so the bot can post "N/M" updates to the user.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from .hasher import download_thumb_and_phash

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None]]

_PROGRESS_STEP_THRESHOLD = 1000  # only fire progress when total exceeds this


def _detect_file_type(msg) -> Optional[str]:
    """Return a short kind tag the rest of the agent uses, or None for
    text-only / unsupported messages."""
    if getattr(msg, "photo", None) is not None:
        return "photo"
    if getattr(msg, "video", None) is not None:
        return "video"
    if getattr(msg, "voice", None) is not None:
        return "voice"
    if getattr(msg, "sticker", None) is not None:
        return "sticker"
    if getattr(msg, "document", None) is not None:
        return "document"
    return None


class TargetIndexer:
    def __init__(self, client, tdb, mdb):
        self.client = client
        self.tdb = tdb
        self.mdb = mdb

    def _config_key(self, target_chat: str) -> str:
        return f"last_scanned_msg_id_{target_chat}"

    async def scan_target(
        self,
        target_chat: str,
        total_hint: Optional[int] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> dict:
        """Walk `target_chat` newer-than-last-scanned and insert thumb-only
        rows for every media message. Returns {'scanned': int,
        'inserted': int}. Progress callback is only invoked when
        `total_hint` > 1000 (see module doc)."""
        key = self._config_key(target_chat)
        last = int(await self.tdb.get_config(key) or 0)

        fire_progress = (
            progress_cb is not None
            and total_hint is not None
            and total_hint > _PROGRESS_STEP_THRESHOLD
        )
        step = max(1, total_hint // 10) if fire_progress else None
        next_report_at = step if fire_progress else None

        scanned = 0
        inserted = 0
        highest = last

        async for msg in self.client.iter_messages(
            target_chat, min_id=last, reverse=True,
        ):
            scanned += 1
            highest = max(highest, int(msg.id))

            ftype = _detect_file_type(msg)
            if ftype is None:
                # Text-only / unhandled — never write to media table.
                pass
            else:
                thumb_phash = await download_thumb_and_phash(self.client, msg)
                file_obj = getattr(msg, "file", None)
                file_size = getattr(file_obj, "size", None) if file_obj else None
                duration = (
                    getattr(file_obj, "duration", None) if file_obj else None
                )
                caption = getattr(msg, "message", None) or getattr(
                    msg, "text", None
                )
                await self.mdb.insert_thumb_record(
                    thumb_phash=thumb_phash,
                    file_type=ftype,
                    file_size=file_size,
                    caption=caption,
                    duration=duration,
                    target_chat=target_chat,
                    target_msg_id=int(msg.id),
                )
                inserted += 1

            if fire_progress and scanned >= next_report_at:
                try:
                    await progress_cb(scanned, total_hint)
                except Exception as e:
                    # Progress reporting must never break the scan.
                    logger.warning(f"progress_cb raised: {e}")
                next_report_at += step

        # Persist last_scanned even on empty scans so incremental sync
        # doesn't keep re-reading the same (empty) tail each time.
        if highest > last:
            await self.tdb.set_config(key, str(highest))

        return {"scanned": scanned, "inserted": inserted}
