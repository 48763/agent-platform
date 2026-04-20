import os
import logging
import asyncio
import uuid
from typing import Callable, Optional, Any
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.hasher import compute_sha256, compute_phash, compute_phash_video, hamming_distance
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.tag_extractor import extract_tags
from agents.tg_transfer.media_utils import ffprobe_metadata
from agents.tg_transfer.byte_budget import ByteBudget

logger = logging.getLogger(__name__)


class OverSizeLimit(Exception):
    """Raised mid-download when the user's live size_limit_mb has been lowered
    below the number of bytes already pulled. Caller marks the message as
    'skipped' (not 'failed') so retry logic doesn't fire."""


def _meta_from_message(message) -> dict | None:
    """Fallback video metadata from telethon Message.file (uploader-reported)."""
    f = getattr(message, "file", None)
    if not f:
        return None
    width = getattr(f, "width", None) or 0
    height = getattr(f, "height", None) or 0
    duration = getattr(f, "duration", None) or 0
    if not width or not height:
        return None
    return {"duration": int(duration), "width": int(width), "height": int(height)}


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
        byte_budget: ByteBudget | None = None,
    ):
        self.client = client
        self.db = db
        self.tmp_dir = tmp_dir
        self.retry_limit = retry_limit
        self.progress_interval = progress_interval
        self.media_db = media_db
        self.phash_threshold = phash_threshold
        # Optional global byte budget. When set, every _download_with_resume
        # reserves the remaining file bytes from it before streaming, so
        # concurrent downloads can't exceed the configured cap (e.g. 1GB).
        self.byte_budget = byte_budget
        self._cancelled: set[str] = set()

    def cancel_job(self, job_id: str):
        self._cancelled.add(job_id)

    async def _size_limit_bytes(self) -> int:
        """Current per-message byte cap. Read from DB on every call so the
        user can change it live while a batch is running. 0 = no limit."""
        raw = await self.db.get_config("size_limit_mb")
        if not raw:
            return 0
        try:
            mb = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(mb, 0) * 1024 * 1024

    @staticmethod
    def _declared_size(message) -> int:
        """TG-reported file size (bytes), 0 if unknown. Used for pre-download
        policy checks — never a ground truth, but good enough to skip."""
        f = getattr(message, "file", None)
        if not f:
            return 0
        size = getattr(f, "size", None)
        return int(size) if isinstance(size, int) else 0

    async def _album_over_limit(self, messages: list) -> bool:
        """True if the SUM of declared file sizes in the album exceeds the
        current size_limit_mb. An album is atomic — one file over the
        allowance fails the whole group."""
        limit = await self._size_limit_bytes()
        if limit <= 0:
            return False
        total = sum(self._declared_size(m) for m in messages)
        return total > limit

    async def _download_with_resume(
        self, message, fresh_dest: str,
        job_id: str | None = None, message_id: int | None = None,
        flush_bytes: int = 64 * 1024 * 1024,
    ) -> str:
        """Download `message`'s media to disk, resuming from a previous
        partial download if one is recorded. Updates `job_messages.partial_path`
        + `downloaded_bytes` every `flush_bytes` so a crash mid-stream can
        resume close to the last flush.

        If no job_id/message_id is given, behaves as a plain streaming
        download to `fresh_dest`.
        """
        # Decide where to write + what offset to start at.
        dest = fresh_dest
        offset = 0
        if job_id is not None and message_id is not None:
            row = await self.db.get_message(job_id, message_id)
            stored_path = (row or {}).get("partial_path")
            stored_bytes = (row or {}).get("downloaded_bytes") or 0
            if stored_path and os.path.exists(stored_path):
                dest = stored_path
                actual = os.path.getsize(dest)
                # Trust whichever is smaller — disk wins if OS lost bytes,
                # DB wins if the file has extra unflushed tail from a crash.
                offset = min(actual, stored_bytes)
                if actual > offset:
                    with open(dest, "rb+") as f:
                        f.truncate(offset)

        if job_id is not None and message_id is not None:
            await self.db.set_partial(job_id, message_id, dest, offset)

        # Figure out how many bytes this stream will actually pull. Used to
        # reserve from the byte_budget when present so total concurrent in-
        # flight bytes stay under the cap.
        reserve = 0
        if self.byte_budget is not None:
            file_obj = getattr(message, "file", None)
            total_size = getattr(file_obj, "size", None) if file_obj else None
            if isinstance(total_size, int) and total_size > 0:
                reserve = max(total_size - offset, 0)

        mode = "ab" if offset > 0 else "wb"
        downloaded = offset
        since_flush = 0

        async def _stream():
            nonlocal downloaded, since_flush
            with open(dest, mode) as f:
                async for chunk in self.client.iter_download(message, offset=offset):
                    f.write(chunk)
                    downloaded += len(chunk)
                    since_flush += len(chunk)
                    if since_flush >= flush_bytes:
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                        if job_id is not None and message_id is not None:
                            await self.db.set_partial(
                                job_id, message_id, dest, downloaded,
                            )
                        since_flush = 0
                        # Live threshold check: if user lowered the limit
                        # since start, bail out here instead of pulling more.
                        live_limit = await self._size_limit_bytes()
                        if live_limit > 0 and downloaded > live_limit:
                            raise OverSizeLimit(
                                f"downloaded {downloaded}B exceeds live limit {live_limit}B"
                            )
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

        if self.byte_budget is not None:
            async with self.byte_budget.slot(reserve):
                await _stream()
        else:
            await _stream()

        if job_id is not None and message_id is not None:
            await self.db.set_partial(job_id, message_id, dest, downloaded)
        return dest

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

    async def transfer_album(self, target_entity, messages: list,
                              target_chat: str = "", source_chat: str = "",
                              job_id: str = None) -> bool:
        """Transfer a media group (album) as a single album.
        Atomic: if any download fails, nothing is uploaded.

        Also writes one media_db row per file (status=uploaded on success) so
        dashboard stats reflect real transfer count.
        """
        os.makedirs(self.tmp_dir, exist_ok=True)

        caption = None
        for msg in messages:
            if msg.text and not caption:
                caption = msg.text

        artefacts: list[str] = []
        media_ids: list[int] = []

        try:
            # Parallel download into flat tmp_dir with unique per-file names.
            planned_paths = []
            for msg in messages:
                base = f"{msg.id}_{uuid.uuid4().hex[:8]}"
                dest = os.path.join(self.tmp_dir, f"{base}.dat")
                planned_paths.append(dest)
                artefacts.append(dest)

            # Use resumable streaming per file when we have a job binding, so a
            # restart picks up each file from its last 64MB flush. Falls back
            # to atomic download when job_id is missing (single-transfer flow).
            if job_id is not None:
                async def _safe(m, d):
                    try:
                        return await self._download_with_resume(
                            m, d, job_id=job_id, message_id=m.id,
                        )
                    except Exception as e:
                        # Preserve legacy "download fail → False" contract;
                        # partial state stays in DB for the next retry.
                        logger.warning(f"Resume download failed for msg {m.id}: {e}")
                        return None
                download_tasks = [
                    _safe(msg, dest) for msg, dest in zip(messages, planned_paths)
                ]
            else:
                download_tasks = [
                    self.client.download_media(msg, file=dest)
                    for msg, dest in zip(messages, planned_paths)
                ]
            paths = await asyncio.gather(*download_tasks)

            # Atomic check: all must succeed
            if any(p is None for p in paths):
                return False

            # Track any paths Telethon wrote elsewhere (e.g. when file arg was
            # ignored, or resume used a stored partial_path) so we still clean
            # them up.
            for p in paths:
                if p not in artefacts:
                    artefacts.append(p)

            file_paths = list(paths)

            # Per-file dedup + upsert. Option B: if a file is already uploaded
            # to this target (sha256 or phash-similar), drop that file from
            # the album and send the remaining files.
            effective_messages: list = []
            effective_paths: list[str] = []

            if self.media_db:
                all_phashes = None  # lazy-load
                for msg, path in zip(messages, file_paths):
                    sha256 = compute_sha256(path)
                    file_type = self._detect_file_type(msg)
                    phash = None
                    if file_type == "photo":
                        phash = compute_phash(path)
                    elif file_type == "video":
                        frame_path = os.path.join(
                            self.tmp_dir,
                            f"{msg.id}_{uuid.uuid4().hex[:8]}.frame.jpg",
                        )
                        artefacts.append(frame_path)
                        phash = await compute_phash_video(path, frame_path)

                    # sha256 dedup
                    if await self.media_db.find_by_sha256(sha256, target_chat):
                        continue
                    # phash similarity
                    if phash:
                        if all_phashes is None:
                            all_phashes = await self.media_db.get_all_phashes()
                        if any(
                            hamming_distance(phash, row["phash"]) <= self.phash_threshold
                            for row in all_phashes
                        ):
                            continue

                    file_size = os.path.getsize(path) if os.path.exists(path) else None
                    media_id = await self.media_db.upsert_pending(
                        sha256=sha256, phash=phash, file_type=file_type,
                        file_size=file_size, caption=msg.text or "",
                        source_chat=source_chat, source_msg_id=msg.id,
                        target_chat=target_chat, job_id=job_id,
                    )
                    if media_id is None:
                        # Race: uploaded row slipped in between check and upsert
                        continue
                    media_ids.append(media_id)
                    effective_messages.append(msg)
                    effective_paths.append(path)
            else:
                effective_messages = list(messages)
                effective_paths = list(file_paths)

            # All files were dedup'd → nothing to send, still counts as success
            if not effective_paths:
                return True

            result = await self.client.send_file(
                target_entity, effective_paths, caption=caption,
            )

            # Mark uploaded per file. send_file for a list returns a list of
            # Message objects (one per media); fall back gracefully if TG
            # returned a single object or something unexpected.
            if self.media_db and media_ids and result:
                sent_list = list(result) if isinstance(result, (list, tuple)) else [result]
                for idx, mid in enumerate(media_ids):
                    sent = sent_list[idx] if idx < len(sent_list) else None
                    sent_id = getattr(sent, "id", None) if sent else None
                    if sent_id:
                        await self.media_db.mark_uploaded(mid, sent_id)
                        msg = effective_messages[idx]
                        tags = extract_tags(msg.text)
                        if tags:
                            await self.media_db.add_tags(mid, tags)

            # Upload delivered → wipe partial state for every message in the
            # group so future runs don't try to resume already-delivered files.
            if job_id is not None:
                for msg in messages:
                    try:
                        await self.db.clear_partial(job_id, msg.id)
                    except Exception:
                        pass
            return True
        except Exception:
            if self.media_db and media_ids:
                for mid in media_ids:
                    try:
                        await self.media_db.mark_failed(mid)
                    except Exception:
                        pass
            raise
        finally:
            for p in artefacts:
                try:
                    if p and os.path.exists(p):
                        os.unlink(p)
                except Exception:
                    logger.debug(f"Failed to unlink tmp file {p}", exc_info=True)

    async def _transfer_media(self, target_entity, message, target_chat: str = "",
                               source_chat: str = "", job_id: str = None) -> dict:
        """Download and re-upload a single media message.
        Returns: {"ok": bool, "dedup": bool, "similar": list | None}
        """
        os.makedirs(self.tmp_dir, exist_ok=True)
        # Size-limit gate: reject oversize messages before spending any
        # bandwidth. Caller marks the result as 'skipped', not 'failed'.
        limit = await self._size_limit_bytes()
        if limit > 0:
            declared = self._declared_size(message)
            if declared > limit:
                return {
                    "ok": False, "dedup": False, "similar": None,
                    "over_limit": True,
                }
        # Flat layout: per-message filenames share one directory to keep
        # filesystem metadata churn (mkdir/rmtree per message) minimal.
        base = f"{message.id}_{uuid.uuid4().hex[:8]}"
        media_path = os.path.join(self.tmp_dir, f"{base}.dat")
        frame_path = os.path.join(self.tmp_dir, f"{base}.frame.jpg")
        thumb_path_target = os.path.join(self.tmp_dir, f"{base}.thumb.jpg")
        artefacts = [media_path, frame_path, thumb_path_target]
        media_id = None

        try:
            # Resumable download: if a partial exists in job_messages, pick
            # up from downloaded_bytes; else stream fresh into media_path.
            if job_id is not None:
                path = await self._download_with_resume(
                    message, media_path, job_id=job_id, message_id=message.id,
                )
            else:
                path = await self.client.download_media(message, file=media_path)
            if not path:
                return {"ok": False, "dedup": False, "similar": None}
            if path != media_path:
                artefacts.append(path)

            # Compute hashes
            sha256 = compute_sha256(path)
            file_type = self._detect_file_type(message)
            phash = None
            if file_type == "video":
                phash = await compute_phash_video(path, frame_path)
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

                # Upsert pending media record (revives failed/skipped rows)
                caption = message.text or ""
                file_size = os.path.getsize(path) if os.path.exists(path) else None
                media_id = await self.media_db.upsert_pending(
                    sha256=sha256, phash=phash, file_type=file_type,
                    file_size=file_size, caption=caption,
                    source_chat=source_chat, source_msg_id=message.id,
                    target_chat=target_chat, job_id=job_id,
                )
                if media_id is None:
                    # Race with another job that just uploaded this content
                    return {"ok": True, "dedup": True, "similar": None}

            # Build upload kwargs
            upload_kwargs = {"caption": message.text}

            # Add video metadata if applicable
            if file_type == "video":
                meta = await ffprobe_metadata(path)
                if not meta:
                    meta = _meta_from_message(message)
                if meta:
                    upload_kwargs["attributes"] = [DocumentAttributeVideo(
                        duration=meta["duration"],
                        w=meta["width"],
                        h=meta["height"],
                        supports_streaming=True,
                    )]
                    upload_kwargs["supports_streaming"] = True

            # Attach TG original thumbnail (largest variant) so preview shows
            thumb_path = await self._download_tg_thumb(message, thumb_path_target)
            if thumb_path:
                upload_kwargs["thumb"] = thumb_path
                if thumb_path != thumb_path_target:
                    artefacts.append(thumb_path)

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

            # Upload succeeded → wipe partial state so a future rerun doesn't
            # try to resume a file that was already delivered.
            if job_id is not None:
                try:
                    await self.db.clear_partial(job_id, message.id)
                except Exception:
                    pass

            return {"ok": True, "dedup": False, "similar": None}
        except OverSizeLimit:
            # Live threshold fired mid-download — treat as policy skip, not
            # retryable failure. Clean up media_db row so stats reflect reality.
            if self.media_db and media_id:
                try:
                    await self.media_db.mark_failed(media_id)
                except Exception:
                    pass
            return {
                "ok": False, "dedup": False, "similar": None, "over_limit": True,
            }
        except Exception as e:
            if self.media_db and media_id:
                try:
                    await self.media_db.mark_failed(media_id)
                except Exception:
                    pass
            raise
        finally:
            for p in artefacts:
                try:
                    if p and os.path.exists(p):
                        os.unlink(p)
                except Exception:
                    logger.debug(f"Failed to unlink tmp file {p}", exc_info=True)

    async def _download_tg_thumb(self, message, thumb_path: str) -> str | None:
        """Download the largest available TG-provided thumbnail for a message
        to the given path. Returns None if the message has no thumbs or the
        download fails."""
        thumbs = None
        doc = getattr(message, "document", None)
        if doc is not None:
            thumbs = getattr(doc, "thumbs", None)
        if not thumbs:
            return None
        try:
            path = await self.client.download_media(message, file=thumb_path, thumb=-1)
            return path or None
        except Exception as e:
            logger.debug(f"Thumb download failed for msg {getattr(message, 'id', '?')}: {e}")
            return None

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
            # Check cancel
            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                await self.db.update_job_status(job_id, "cancelled")
                return "cancelled"

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
                        # Album sum-size gate: if total > size_limit_mb,
                        # skip the entire group without downloading (atomic).
                        if await self._album_over_limit(messages):
                            status = "skipped"
                        else:
                            ok = await self.transfer_album(
                                target_entity, messages,
                                target_chat=job["target_chat"],
                                source_chat=job["source_chat"],
                                job_id=job_id,
                            )
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
                        # Live size-limit gate: re-read config for every
                        # message so user threshold changes take effect
                        # immediately on the next message.
                        limit = await self._size_limit_bytes()
                        declared = self._declared_size(msg)
                        if limit > 0 and declared > limit:
                            await self.db.mark_message(
                                job_id, message_id, "skipped",
                                error="over size limit",
                            )
                            processed += 1
                            continue

                        job = await self.db.get_job(job_id)
                        result = await self.transfer_single(
                            source_entity, target_entity, msg,
                            target_chat=job["target_chat"],
                            source_chat=job["source_chat"],
                            job_id=job_id,
                        )
                        if result["dedup"]:
                            await self.db.mark_message(job_id, message_id, "skipped")
                        elif result.get("over_limit"):
                            # Exceeded size_limit_mb → skip, don't fail/retry.
                            await self.db.mark_message(
                                job_id, message_id, "skipped",
                                error="over size limit",
                            )
                        elif result["similar"]:
                            # In batch mode, skip similar (no interactive prompt)
                            await self.db.mark_message(job_id, message_id, "skipped")
                        else:
                            await self.db.mark_message(
                                job_id, message_id, "success" if result["ok"] else "failed"
                            )
                    processed += 1

            except OverSizeLimit as e:
                # Fired mid-download because the user lowered the live limit.
                # Mark skipped (no retry), including all album siblings.
                logger.info(f"Oversize during stream for msg {message_id}: {e}")
                if grouped_id:
                    group_rows = await self.db.get_grouped_messages(job_id, grouped_id)
                    for gr in group_rows:
                        await self.db.mark_message(
                            job_id, gr["message_id"], "skipped",
                            error="over size limit",
                        )
                    processed += len(group_rows)
                else:
                    await self.db.mark_message(
                        job_id, message_id, "skipped",
                        error="over size limit",
                    )
                    processed += 1
                continue

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
