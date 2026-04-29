"""Liveness scanner for the media table.

Runs as a background coroutine started from agent boot. Every cycle:

1. Build (or resume) a plan file at `tmp/.liveness/<uuid>.json` listing
   every media_id with status='uploaded'.
2. Pop BATCH_SIZE ids, check each against the target chat:
   - target msg gone   → delete the media row
   - caption changed   → update_caption_and_tags (re-extract tags)
   - caption unchanged → no-op (last_updated_at NOT bumped)
3. Atomic-rewrite the plan file with the trimmed remaining list.
4. Repeat until plan is empty, then delete the plan file.
5. sleep(SLEEP_INTERVAL_SECONDS) before the next cycle.

Restart safety: a half-consumed plan file is picked up by the next boot
via locate_or_create_plan. The pop-50 → process → rewrite cycle is
idempotent: re-processing a media_id whose row no longer exists is a
safe no-op via process_one's guard.

The plan dir uses a dotfile prefix so existing tmp/ janitors (orphan
scan, legacy migration) skip it automatically.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from telethon import TelegramClient

from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.media_db import MediaDB

logger = logging.getLogger(__name__)

LIVENESS_DIR = ".liveness"
BATCH_SIZE = 50
SLEEP_INTERVAL_SECONDS = 24 * 3600


def _plan_dir(tmp_root: str) -> str:
    return os.path.join(tmp_root, LIVENESS_DIR)


def load_plan(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_plan(path: str, plan: dict):
    """Atomic-rewrite: write to <path>.tmp then rename. A crash between
    write and rename leaves the previous (good) file in place."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    os.replace(tmp, path)


async def create_plan(media_db: MediaDB, tmp_root: str) -> str:
    """Build a fresh scan plan covering every uploaded media_id."""
    scan_id = str(uuid.uuid4())
    plan = {
        "scan_id": scan_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "remaining": await media_db.list_all_uploaded_ids(),
    }
    path = os.path.join(_plan_dir(tmp_root), f"{scan_id}.json")
    save_plan(path, plan)
    logger.info(
        "Liveness scan %s created with %d media_ids",
        scan_id, len(plan["remaining"]),
    )
    return path


async def locate_or_create_plan(media_db: MediaDB, tmp_root: str) -> str:
    """Return path of the in-progress plan file if one exists; otherwise
    build a new one. This is what makes restart-resume work — a partly
    consumed plan from before reboot picks up where it left off.

    Defensive: if the existing plan file is corrupt (zero-byte / truncated
    JSON from a freak crash before atomic rename), remove it so we don't
    block liveness for 24h on an unparseable file."""
    plan_dir = _plan_dir(tmp_root)
    if os.path.isdir(plan_dir):
        existing = sorted(
            os.path.join(plan_dir, name)
            for name in os.listdir(plan_dir)
            if name.endswith(".json")
        )
        for candidate in existing:
            try:
                load_plan(candidate)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "Liveness plan %s is corrupt (%s), removing", candidate, e,
                )
                try:
                    os.remove(candidate)
                except OSError:
                    pass
                continue
            logger.info("Liveness scan resuming from %s", candidate)
            return candidate
    return await create_plan(media_db, tmp_root)


async def process_one(client: TelegramClient, media_db: MediaDB, media_id: int):
    """Check one media_id against the target chat, applying the diff:
    delete row / update caption+tags / no-op."""
    row = await media_db.get_media(media_id)
    if row is None:
        # Row was removed elsewhere (concurrent on_task_deleted etc.).
        return
    target_msg_id = row.get("target_msg_id")
    if target_msg_id is None:
        return
    try:
        target_entity = await resolve_chat(client, row["target_chat"])
        msg = await client.get_messages(target_entity, ids=target_msg_id)
    except Exception as e:
        logger.warning(
            "Liveness check failed for media %d: %s", media_id, e,
        )
        return
    if msg is None:
        await media_db.delete_media(media_id)
        logger.info("Liveness: media %d deleted (target msg gone)", media_id)
        return
    new_caption = (getattr(msg, "text", None)
                   or getattr(msg, "message", None)
                   or "")
    old_caption = row.get("caption") or ""
    if new_caption != old_caption:
        await media_db.update_caption_and_tags(media_id, new_caption)
        logger.info(
            "Liveness: media %d caption updated", media_id,
        )


async def run_one_scan(
    client: TelegramClient, media_db: MediaDB, tmp_root: str,
):
    """Drain a scan plan to completion. Plan file is deleted on success."""
    path = await locate_or_create_plan(media_db, tmp_root)
    while True:
        plan = load_plan(path)
        remaining = plan.get("remaining") or []
        if not remaining:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            logger.info("Liveness scan %s completed", plan.get("scan_id"))
            return
        batch = remaining[:BATCH_SIZE]
        for media_id in batch:
            await process_one(client, media_db, media_id)
        plan["remaining"] = remaining[BATCH_SIZE:]
        save_plan(path, plan)


async def run_liveness_loop(
    client: TelegramClient, media_db: MediaDB, tmp_root: str,
    interval_seconds: int = SLEEP_INTERVAL_SECONDS,
):
    """Background driver: scan to completion, then sleep `interval_seconds`,
    forever. Sleep is fixed regardless of scan duration (per spec)."""
    while True:
        try:
            await run_one_scan(client, media_db, tmp_root)
        except Exception as e:
            logger.error("Liveness loop error: %s", e, exc_info=True)
        await asyncio.sleep(interval_seconds)
