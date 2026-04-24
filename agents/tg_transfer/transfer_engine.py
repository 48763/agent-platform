import os
import logging
import asyncio
import mimetypes
import uuid
from typing import Callable, Optional, Any
from telethon import TelegramClient, utils as tl_utils
from telethon.tl.types import (
    DocumentAttributeVideo, DocumentAttributeFilename,
    InputMediaUploadedDocument, InputMediaUploadedPhoto, InputSingleMedia,
)
from telethon.tl.functions.messages import (
    UploadMediaRequest, SendMultiMediaRequest,
)
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.hasher import (
    compute_sha256, compute_phash, compute_phash_video, hamming_distance,
    download_thumb_and_phash,
)
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.tag_extractor import extract_tags
from agents.tg_transfer.media_utils import ffprobe_metadata, extract_video_thumb
from agents.tg_transfer.byte_budget import ByteBudget

logger = logging.getLogger(__name__)


class OverSizeLimit(Exception):
    """Raised mid-download when the user's live size_limit_mb has been lowered
    below the number of bytes already pulled. Caller marks the message as
    'skipped' (not 'failed') so retry logic doesn't fire."""


def _derive_upload_ext(message) -> str:
    """Return the file extension from Telethon metadata (message.file.ext).
    Telethon resolves this from DocumentAttributeFilename or mime_type,
    covering all media types. Falls back to empty string if unavailable."""
    ext = getattr(getattr(message, "file", None), "ext", None)
    if isinstance(ext, str) and ext.startswith(".") and len(ext) > 1:
        return ext
    return ""


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

    def _download_request_size(self) -> int:
        """Download chunk size. Premium accounts can safely pull 1 MiB chunks
        (Telegram's per-request max) for ~2x throughput; non-premium stays at
        512 KiB to match Telethon's usual default for large files.

        Upload chunk size is NOT tuned here: Telethon 1.43.x `send_file` does
        not forward `part_size_kb` to `upload_file`, so any value we'd pass is
        silently dropped. Upload already uses Telethon's file-size-based
        auto-sizing, which is close to optimal for our workloads.
        """
        if getattr(self.client, "premium_account", False):
            return 1024 * 1024
        return 512 * 1024

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
                async for chunk in self.client.iter_download(
                    message, offset=offset, request_size=self._download_request_size(),
                ):
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
        """Check if message type should be skipped (sticker, poll, voice,
        text-only).

        Text-only is skipped by policy: this tool exists to migrate media,
        and historically we also forwarded bare text — but that produced a
        lot of noise (chatty chats would replay entire conversations in the
        target). Skipping them keeps the target clean; users who want text
        mirroring can copy/paste manually.
        """
        if message.sticker:
            return True
        if message.poll:
            return True
        if message.voice:
            return True
        # Pure text (no media attached) — covers both text messages the user
        # typed and messages that only carry a caption-less string.
        if not message.media and getattr(message, "text", None):
            return True
        return False

    async def transfer_single(self, source_entity, target_entity, message,
                               target_chat: str = "", source_chat: str = "",
                               job_id: str = None,
                               skip_pre_dedup: bool = False) -> dict:
        """Transfer a single message. Returns {"ok": bool, "dedup": bool, "similar": list | None}.

        `skip_pre_dedup`: bypass Phase 4 thumb-phash check. Used by the Phase 5
        dedup resolver when the user explicitly marks an ambiguous source as
        "different" — otherwise we'd just re-park it in pending_dedup forever.
        """
        if message.media and not self.should_skip(message):
            return await self._transfer_media(
                target_entity, message, target_chat=target_chat,
                source_chat=source_chat, job_id=job_id,
                skip_pre_dedup=skip_pre_dedup,
            )
        # Text-only / sticker / poll / voice all fall here: should_skip is the
        # single source of truth. run_batch marks these 'skipped' before even
        # calling us, so hitting this branch usually means a direct caller
        # (e.g. resume path) fed us a skip-eligible message — treat as skip.
        if self.should_skip(message):
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
                ext = _derive_upload_ext(msg)
                dest = os.path.join(self.tmp_dir, f"{base}{ext}")
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

            # Atomic check: all must succeed and have real content
            if any(p is None for p in paths):
                return False
            if any(not os.path.exists(p) or os.path.getsize(p) == 0 for p in paths):
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

            # Build per-file attributes + thumbs for videos in album
            per_file_attrs = []
            per_file_thumbs = []
            for msg, path in zip(effective_messages, effective_paths):
                file_type = self._detect_file_type(msg)
                attrs = None
                thumb = None
                if file_type == "video":
                    meta = await ffprobe_metadata(path)
                    if not meta:
                        meta = _meta_from_message(msg)
                    if meta:
                        attrs = [DocumentAttributeVideo(
                            duration=meta["duration"],
                            w=meta["width"],
                            h=meta["height"],
                            supports_streaming=True,
                        )]
                    thumb_dest = os.path.join(
                        self.tmp_dir,
                        f"{msg.id}_{uuid.uuid4().hex[:8]}.athumb.jpg",
                    )
                    artefacts.append(thumb_dest)
                    thumb = await self._download_tg_thumb(msg, thumb_dest)
                    # Same fallback as single-transfer path: if the source
                    # message has no TG thumb (e.g. "send as file" video),
                    # extract a frame locally so the album preview still
                    # renders with a real poster.
                    if not thumb:
                        thumb = await extract_video_thumb(path, thumb_dest)
                per_file_attrs.append(attrs)
                per_file_thumbs.append(thumb)

            # Manual album upload — bypasses Telethon's send_file(list=...),
            # which silently drops per-file `attributes` and `thumb` inside
            # `_send_album` (it calls `_file_to_media` without them). For
            # small videos the TG server auto-probes uploaded MP4s and
            # compensates; for large uploads (~200MB+) the probe fails,
            # leaving the bogus DocumentAttributeVideo(0, 1, 1) that Telethon
            # synthesises → target renders as 0:00 with no preview. Sending
            # each file via UploadMediaRequest with our own attributes/thumb
            # makes the per-file metadata actually reach the server.
            result = await self._upload_album_manual(
                target_entity, effective_messages, effective_paths,
                per_file_attrs, per_file_thumbs, caption,
            )

            # Mark uploaded per file. _upload_album_manual returns a list of
            # Message-like objects (one per media) produced from TG's Updates.
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

    async def _upload_album_manual(self, target_entity, effective_messages,
                                    effective_paths, per_file_attrs,
                                    per_file_thumbs, caption):
        """Manually assemble an album upload so per-file attributes + thumbs
        actually reach Telegram. See `transfer_album` for why we can't use
        `client.send_file(file=list, attributes=..., thumb=...)` here.

        Returns a list of Message-like objects (one per uploaded file), each
        with at least `.id` set — same contract as the old `send_file(list)`
        return value, so `media_db.mark_uploaded` keeps working.
        """
        single_media = []
        for msg, path, attrs, thumb_path in zip(
            effective_messages, effective_paths,
            per_file_attrs, per_file_thumbs,
        ):
            file_type = self._detect_file_type(msg)
            file_handle = await self.client.upload_file(path)

            if file_type == "photo":
                uploaded = InputMediaUploadedPhoto(file=file_handle)
            else:
                mime, _ = mimetypes.guess_type(path)
                if not mime:
                    mime = "video/mp4" if file_type == "video" \
                        else "application/octet-stream"
                doc_attrs = list(attrs) if attrs else []
                # Always include filename — plays nicely with TG UI and with
                # downloaders that key off DocumentAttributeFilename.
                name = (
                    getattr(getattr(msg, "file", None), "name", None)
                    or os.path.basename(path)
                )
                doc_attrs.append(DocumentAttributeFilename(file_name=name))
                thumb_handle = None
                if thumb_path:
                    thumb_handle = await self.client.upload_file(thumb_path)
                uploaded = InputMediaUploadedDocument(
                    file=file_handle, mime_type=mime,
                    attributes=doc_attrs, thumb=thumb_handle,
                )

            # Turn the uploaded media into a server-side reference via
            # UploadMediaRequest — same shape Telethon itself uses in
            # `_send_album`. This is where the server probe would normally
            # run; by passing our own attributes/thumb we don't depend on
            # that probe succeeding for large files.
            r = await self.client(UploadMediaRequest(
                peer=target_entity, media=uploaded,
            ))
            if file_type == "photo":
                final_media = tl_utils.get_input_media(r.photo)
            else:
                final_media = tl_utils.get_input_media(
                    r.document, supports_streaming=True,
                )

            single_media.append(InputSingleMedia(
                media=final_media,
                message=caption or "",
            ))
            # Only the first file carries the caption in an album (matches
            # Telethon's _send_album behaviour — subsequent files get '').
            caption = ""

        result = await self.client(SendMultiMediaRequest(
            peer=target_entity, multi_media=single_media,
        ))
        random_ids = [m.random_id for m in single_media]
        try:
            return self.client._get_response_message(
                random_ids, result, target_entity,
            )
        except Exception:
            # Fallback: synthesise minimal Message stubs so media_db still
            # sees a non-None `.id` per sent file. This should only hit in
            # tests or if Telethon's internal helper is refactored.
            logger.warning(
                "_get_response_message failed; returning stub Message list",
                exc_info=True,
            )
            sent = []
            for update in getattr(result, "updates", []) or []:
                msg = getattr(update, "message", None)
                if msg is not None and getattr(msg, "id", None) is not None:
                    sent.append(msg)
            return sent

    async def _transfer_media(self, target_entity, message, target_chat: str = "",
                               source_chat: str = "", job_id: str = None,
                               skip_pre_dedup: bool = False) -> dict:
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
        # Phase 4: cross-source thumb dedup. Only a tiny thumb (not the full
        # file) is downloaded, so this is a pure saving when the target
        # already has the content. Phase 5 "different" resolutions bypass this
        # step — otherwise the user-arbitrated upload would re-park itself in
        # pending_dedup on the same thumb collision.
        if skip_pre_dedup:
            pre = {"hit": False}
        else:
            pre = await self._pre_dedup_by_thumb(
                message, target_chat=target_chat, source_chat=source_chat,
                job_id=job_id,
            )
        if pre.get("hit"):
            if pre.get("dedup"):
                return {"ok": True, "dedup": True, "similar": None}
            if pre.get("ambiguous"):
                return {
                    "ok": False, "dedup": False, "similar": None,
                    "ambiguous": True,
                }
        # Flat layout: per-message filenames share one directory to keep
        # filesystem metadata churn (mkdir/rmtree per message) minimal.
        base = f"{message.id}_{uuid.uuid4().hex[:8]}"
        ext = _derive_upload_ext(message)
        media_path = os.path.join(self.tmp_dir, f"{base}{ext}")
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
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return {"ok": False, "dedup": False, "similar": None}

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

            # Attach TG original thumbnail (largest variant) so preview shows.
            # When the source is a "send as file" video it arrives with no TG
            # thumb attached — fall back to an ffmpeg-extracted frame so the
            # in-feed preview still renders instead of appearing as a blank
            # document tile. Photos keep TG-only behaviour (they always have
            # thumbs) so we don't pay ffmpeg cost unnecessarily.
            thumb_path = await self._download_tg_thumb(message, thumb_path_target)
            if not thumb_path and file_type == "video":
                thumb_path = await extract_video_thumb(path, thumb_path_target)
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

    async def _pre_dedup_by_thumb(
        self, message, target_chat: str, source_chat: str, job_id: str | None,
    ) -> dict:
        """Cross-source dedup before any full-file download.

        Downloads only the TG-attached thumbnail, computes its phash, and
        compares against the target-chat thumb index built by /index_target.

        Return shape (mirrors the rest of transfer_engine's result dicts):
          {"hit": False}                       → no thumb candidate; fall
                                                 through to the normal
                                                 download → sha256/phash path.
          {"hit": True, "dedup": True}         → strict match: caption,
                                                 file_size, duration all
                                                 agreed with a candidate row,
                                                 which we just upgraded to
                                                 trust='full'. Caller marks
                                                 message as skipped.
          {"hit": True, "ambiguous": True}     → thumb_phash hit but metadata
                                                 disagreed. Row queued in
                                                 pending_dedup for Phase 5
                                                 user resolution.

        Only photos and videos carry useful thumbnails in our scan index, so
        documents/voice/sticker short-circuit with `hit=False` immediately.
        """
        if not self.media_db:
            return {"hit": False}

        file_type = self._detect_file_type(message)
        # Docs/audio have no reliable thumb index from /index_target → can't
        # do thumb dedup. Fall through to normal path (which at least still
        # does sha256 dedup after download).
        if file_type not in ("photo", "video"):
            return {"hit": False}

        thumb_phash = await download_thumb_and_phash(self.client, message)
        if not thumb_phash:
            return {"hit": False}

        candidates = await self.media_db.find_by_thumb_phash(
            thumb_phash, target_chat,
        )
        if not candidates:
            return {"hit": False}

        # Strict-match criteria per spec: caption + file_size + duration must
        # ALL agree with a candidate. Thumb collisions are real (re-encoded
        # frames, similar photos), so we refuse to auto-skip on thumb alone.
        src_caption = getattr(message, "message", None) or getattr(
            message, "text", None,
        )
        src_file = getattr(message, "file", None)
        src_size = getattr(src_file, "size", None) if src_file else None
        src_duration = (
            getattr(src_file, "duration", None) if src_file else None
        )

        for cand in candidates:
            if (cand.get("caption") == src_caption
                    and cand.get("file_size") == src_size
                    and cand.get("duration") == src_duration):
                # Auto-skip: upgrade the candidate from thumb_only → full with
                # verified_by='metadata'. We deliberately leave sha256/phash
                # NULL — we never downloaded the file.
                await self.media_db.upgrade_thumb_to_full(
                    cand["media_id"], verified_by="metadata",
                )
                return {"hit": True, "dedup": True}

        # Thumb matched but at least one field disagreed. Queue for user
        # arbitration at end of batch (Phase 5).
        await self.media_db.insert_pending_dedup(
            job_id=job_id, source_chat=source_chat, source_msg_id=int(message.id),
            candidate_target_msg_ids=[
                c["target_msg_id"] for c in candidates if c.get("target_msg_id")
            ],
            reason="thumb_match_metadata_mismatch",
        )
        return {"hit": True, "ambiguous": True}

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
        """Classify media kind, including documents whose MIME is video/* or
        that carry a DocumentAttributeVideo. Telegram users often "send as
        file" to avoid re-encoding — those arrive with message.video=None and
        only message.document set. Missing that case was the root cause of
        grey previews / 0:00 duration / wrong aspect ratio for transferred
        videos: without the video classification the upload path never probed
        ffmpeg metadata nor attached DocumentAttributeVideo, so TG rendered
        the file as a generic document tile."""
        if message.photo:
            return "photo"
        if message.video:
            return "video"
        doc = getattr(message, "document", None)
        if doc is not None:
            mime = getattr(doc, "mime_type", "") or ""
            if mime.startswith("video/"):
                return "video"
            for a in getattr(doc, "attributes", None) or []:
                if isinstance(a, DocumentAttributeVideo):
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
                    # Only process when we hit the first PENDING in the group
                    pending_in_group = [r for r in group_rows if r["status"] == "pending"]
                    if not pending_in_group or pending_in_group[0]["message_id"] != message_id:
                        # Already handled as part of group (or no pending left)
                        if not pending_in_group:
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
                        elif result.get("ambiguous"):
                            # Phase 4: thumb hit target but metadata differed.
                            # Parked in pending_dedup; Phase 5 will surface a
                            # batch-end summary asking the user to arbitrate.
                            # Not 'pending', so get_next_pending won't retry.
                            await self.db.mark_message(
                                job_id, message_id, "ambiguous",
                            )
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
