import os
import shutil
import logging
import asyncio
from typing import Callable, Optional, Any
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.hasher import compute_sha256, compute_phash, compute_phash_video, hamming_distance
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.tag_extractor import extract_tags
from agents.tg_transfer.media_utils import ffprobe_metadata

logger = logging.getLogger(__name__)


class TransferEngine:
    def __init__(
        self,
        client: TelegramClient,
        db: TransferDB,
        tmp_dir: str = "/tmp/tg_transfer",
        retry_limit: int = 3,
        progress_interval: int = 20,
        media_db: MediaDB = None,
        phash_threshold: int = 10,
    ):
        self.client = client
        self.db = db
        self.tmp_dir = tmp_dir
        self.retry_limit = retry_limit
        self.progress_interval = progress_interval
        self.media_db = media_db
        self.phash_threshold = phash_threshold

    def should_skip(self, message) -> bool:
        """Check if message type should be skipped (sticker, poll, voice)."""
        if message.sticker:
            return True
        if message.poll:
            return True
        if message.voice:
            return True
        return False

    async def transfer_single(self, source_entity, target_entity, message,
                               target_chat: str = "", source_chat: str = "",
                               job_id: str = None) -> dict:
        """Transfer a single message. Returns {"ok": bool, "dedup": bool, "similar": list | None}."""
        if message.media and not self.should_skip(message):
            return await self._transfer_media(
                target_entity, message, target_chat=target_chat,
                source_chat=source_chat, job_id=job_id,
            )
        elif message.text and not message.media:
            await self.client.send_message(target_entity, message.text)
            return {"ok": True, "dedup": False, "similar": None}
        elif self.should_skip(message):
            return {"ok": False, "dedup": False, "similar": None}
        return {"ok": True, "dedup": False, "similar": None}

    async def transfer_album(self, target_entity, messages: list) -> bool:
        """Transfer a media group (album) as a single album."""
        job_dir = os.path.join(self.tmp_dir, "album")
        os.makedirs(job_dir, exist_ok=True)

        files = []
        caption = None
        try:
            for msg in messages:
                path = await self.client.download_media(msg, file=job_dir)
                if path:
                    files.append(path)
                if msg.text and not caption:
                    caption = msg.text

            if files:
                await self.client.send_file(
                    target_entity, files, caption=caption
                )
                return True
            return False
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

    async def _transfer_media(self, target_entity, message, target_chat: str = "",
                               source_chat: str = "", job_id: str = None) -> dict:
        """Download and re-upload a single media message.
        Returns: {"ok": bool, "dedup": bool, "similar": list | None}
        """
        job_dir = os.path.join(self.tmp_dir, str(message.id))
        os.makedirs(job_dir, exist_ok=True)
        media_id = None

        try:
            path = await self.client.download_media(message, file=job_dir)
            if not path:
                return {"ok": False, "dedup": False, "similar": None}

            # Compute hashes
            sha256 = compute_sha256(path)
            file_type = self._detect_file_type(message)
            phash = None
            if file_type == "video":
                phash = await compute_phash_video(path, job_dir)
            elif file_type == "photo":
                phash = compute_phash(path)

            # Check dedup if media_db available
            if self.media_db:
                existing = await self.media_db.find_by_sha256(sha256, target_chat)
                if existing:
                    return {"ok": True, "dedup": True, "similar": None}

                # Check pHash similarity
                if phash:
                    all_phashes = await self.media_db.get_all_phashes()
                    similar = []
                    for row in all_phashes:
                        dist = hamming_distance(phash, row["phash"])
                        if dist <= self.phash_threshold:
                            similar.append({**row, "distance": dist})
                    if similar:
                        return {"ok": False, "dedup": False, "similar": similar}

                # Insert pending media record
                caption = message.text or ""
                file_size = os.path.getsize(path) if os.path.exists(path) else None
                media_id = await self.media_db.insert_media(
                    sha256=sha256, phash=phash, file_type=file_type,
                    file_size=file_size, caption=caption,
                    source_chat=source_chat, source_msg_id=message.id,
                    target_chat=target_chat, job_id=job_id,
                )

            # Build upload kwargs
            upload_kwargs = {"caption": message.text}

            # Add video metadata if applicable
            if file_type == "video":
                meta = await ffprobe_metadata(path)
                if meta:
                    upload_kwargs["attributes"] = [DocumentAttributeVideo(
                        duration=meta["duration"],
                        w=meta["width"],
                        h=meta["height"],
                        supports_streaming=True,
                    )]
                    upload_kwargs["supports_streaming"] = True

            # Upload
            result = await self.client.send_file(
                target_entity, path, **upload_kwargs
            )

            # Record success
            if self.media_db and media_id and result:
                target_msg_id = result.id if hasattr(result, "id") else None
                if target_msg_id:
                    await self.media_db.mark_uploaded(media_id, target_msg_id)
                    tags = extract_tags(message.text)
                    if tags:
                        await self.media_db.add_tags(media_id, tags)

            return {"ok": True, "dedup": False, "similar": None}
        except Exception as e:
            if self.media_db and media_id:
                try:
                    await self.media_db.delete_media(media_id)
                except Exception:
                    pass
            raise
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

    @staticmethod
    def _detect_file_type(message) -> str:
        if message.photo:
            return "photo"
        if message.video:
            return "video"
        return "document"

    async def run_batch(
        self,
        job_id: str,
        source_entity,
        target_entity,
        report_fn: Callable[[str], Any],
    ) -> str:
        """Run a batch transfer job. Returns final status: 'completed', 'paused', or 'failed'.

        report_fn: async callback to send progress/error messages to user.
        """
        await self.db.update_job_status(job_id, "running")
        job = await self.db.get_job(job_id)
        processed = 0

        while True:
            msg_row = await self.db.get_next_pending(job_id)
            if msg_row is None:
                break  # all done

            message_id = msg_row["message_id"]
            grouped_id = msg_row["grouped_id"]

            try:
                # Handle album group
                if grouped_id:
                    group_rows = await self.db.get_grouped_messages(job_id, grouped_id)
                    # Only process when we hit the first pending in the group
                    if group_rows[0]["message_id"] != message_id:
                        # Already handled as part of group
                        await self.db.mark_message(job_id, message_id, "success")
                        processed += 1
                        continue

                    messages = []
                    for gr in group_rows:
                        msg = await self.client.get_messages(source_entity, ids=gr["message_id"])
                        if msg:
                            messages.append(msg)

                    if messages and not self.should_skip(messages[0]):
                        ok = await self.transfer_album(target_entity, messages)
                        status = "success" if ok else "failed"
                    elif messages and self.should_skip(messages[0]):
                        status = "skipped"
                    else:
                        status = "failed"

                    for gr in group_rows:
                        await self.db.mark_message(job_id, gr["message_id"], status)
                    processed += len(group_rows)
                else:
                    # Single message
                    msg = await self.client.get_messages(source_entity, ids=message_id)
                    if msg is None:
                        await self.db.mark_message(job_id, message_id, "failed", error="message deleted")
                        processed += 1
                        continue

                    if self.should_skip(msg):
                        await self.db.mark_message(job_id, message_id, "skipped")
                    else:
                        job = await self.db.get_job(job_id)
                        result = await self.transfer_single(
                            source_entity, target_entity, msg,
                            target_chat=job["target_chat"],
                            source_chat=job["source_chat"],
                            job_id=job_id,
                        )
                        if result["dedup"]:
                            await self.db.mark_message(job_id, message_id, "skipped")
                        elif result["similar"]:
                            # In batch mode, skip similar (no interactive prompt)
                            await self.db.mark_message(job_id, message_id, "skipped")
                        else:
                            await self.db.mark_message(
                                job_id, message_id, "success" if result["ok"] else "failed"
                            )
                    processed += 1

            except Exception as e:
                logger.error(f"Transfer error for msg {message_id}: {e}")
                await self.db.increment_retry(job_id, message_id)
                msg_row = await self.db.get_message(job_id, message_id)

                if msg_row["retry_count"] >= self.retry_limit:
                    # Check auto_skip
                    job = await self.db.get_job(job_id)
                    if job["auto_skip"]:
                        await self.db.mark_message(job_id, message_id, "skipped", error=str(e))
                        processed += 1
                        continue

                    # Pause and ask user
                    await self.db.mark_message(job_id, message_id, "failed", error=str(e))
                    await self.db.update_job_status(job_id, "paused")
                    progress = await self.db.get_progress(job_id)
                    await report_fn(
                        f"訊息 #{message_id} 失敗（已重試 {self.retry_limit} 次）\n"
                        f"錯誤：{e}\n"
                        f"進度：{progress['success']}/{progress['total']}\n\n"
                        f"請選擇：重試 / 跳過 / 一律跳過"
                    )
                    return "paused"
                else:
                    # Reset to pending for next retry
                    await self.db.reset_message(job_id, message_id)
                    continue

            # Progress report
            if processed > 0 and processed % self.progress_interval == 0:
                progress = await self.db.get_progress(job_id)
                await report_fn(
                    f"進度：{progress['success'] + progress['skipped']}/{progress['total']} "
                    f"（成功 {progress['success']}，跳過 {progress['skipped']}）"
                )

        # Completed
        await self.db.update_job_status(job_id, "completed")
        return "completed"
