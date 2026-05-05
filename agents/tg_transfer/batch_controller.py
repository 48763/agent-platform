"""Batch lifecycle controller for the tg_transfer agent.

Owns the three background coroutines:
- _run_batch_background — main batch transfer driver
- _run_defer_scan_background — `/batch --skip-dedup` scan
- _run_process_deferred_background — `/process_deferred` drain

Plus the spawn_* helpers wrapping each in an error-boundary that:
1. logs the exception
2. marks the job 'failed' in TransferDB
3. sends AgentResult(ERROR, ...) to the user via agent.ws_send_result

Without this boundary, an uncaught exception silently kills the
asyncio.Task and leaves the job stuck 'running' with no user
notification.

The controller does NOT own _pending_jobs / _current_chat_id /
_batch_message_cache — those stay on the agent because cancel /
dedup-response handlers also touch them. The controller reads them
via the agent reference.
"""
import asyncio
import logging

from core.models import AgentResult, TaskStatus
from agents.tg_transfer.ambiguous import format_ambiguous_summary
from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.hasher import download_thumb_and_phash

logger = logging.getLogger(__name__)


class BatchController:
    def __init__(self, agent):
        self.agent = agent
        self._bg_tasks: dict[str, asyncio.Task] = {}

    # -- spawn API ----------------------------------------------------------

    def spawn_batch(self, task_id, job_id, job, source_entity, target_entity, chat_id):
        wrapped = self._wrap_with_error_boundary(
            self._run_batch_background(
                task_id, job_id, job, source_entity, target_entity, chat_id,
            ),
            task_id=task_id, job_id=job_id, chat_id=chat_id,
        )
        bg = asyncio.create_task(wrapped)
        self._bg_tasks[task_id] = bg
        return bg

    def spawn_defer_scan(self, task_id, job_id, job, messages, chat_id):
        wrapped = self._wrap_with_error_boundary(
            self._run_defer_scan_background(
                task_id, job_id, job, messages, chat_id,
            ),
            task_id=task_id, job_id=job_id, chat_id=chat_id,
        )
        bg = asyncio.create_task(wrapped)
        self._bg_tasks[task_id] = bg
        return bg

    def spawn_process_deferred(
        self, task_id, job_id, source_chat, target_chat, rows, chat_id,
    ):
        wrapped = self._wrap_with_error_boundary(
            self._run_process_deferred_background(
                task_id, job_id, source_chat, target_chat, rows, chat_id,
            ),
            task_id=task_id, job_id=job_id, chat_id=chat_id,
        )
        bg = asyncio.create_task(wrapped)
        self._bg_tasks[task_id] = bg
        return bg

    def get_task(self, task_id):
        return self._bg_tasks.get(task_id)

    def remove_task(self, task_id):
        return self._bg_tasks.pop(task_id, None)

    # -- error boundary -----------------------------------------------------

    async def _wrap_with_error_boundary(self, coro, task_id, job_id, chat_id):
        """Catch uncaught exceptions, mark job failed, notify user.
        CancelledError propagates without side effect."""
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Background job %s (task %s) crashed: %s",
                job_id, task_id, e, exc_info=True,
            )
            try:
                await self.agent.db.update_job_status(job_id, "failed")
            except Exception as db_err:
                logger.warning(
                    "Failed to mark job %s as failed: %s", job_id, db_err,
                )
            try:
                await self.agent.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.ERROR,
                    message=f"批量任務失敗：{e}",
                ))
            except Exception as ws_err:
                logger.warning(
                    "Failed to notify user of job %s failure: %s",
                    job_id, ws_err,
                )
            raise
        finally:
            self._bg_tasks.pop(task_id, None)
            self.agent._batch_message_cache.pop(job_id, None)

    # -- background coroutines ---------------------------------------------

    async def _run_batch_background(
        self, task_id, job_id, job, source_entity, target_entity, chat_id,
    ):
        """Run batch transfer and report via WS."""
        async def report_fn(text):
            await self.agent.ws_send_progress(task_id, chat_id, text)

        keep_pending_binding = False
        try:
            status = await self.agent.engine.run_batch(
                job_id, source_entity, target_entity, report_fn,
            )
            progress = await self.agent.db.get_progress(job_id)

            if status == "paused":
                await self.agent.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.NEED_INPUT,
                    message=f"搬移暫停\n"
                            f"進度：{progress['success']}/{progress['total']}\n"
                            f"請選擇：重試 / 跳過 / 一律跳過",
                ))
            elif status == "cancelled":
                await self.agent.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.DONE,
                    message=f"搬移已取消\n"
                            f"成功：{progress['success']} 則\n"
                            f"跳過：{progress['skipped']} 則",
                ))
            else:
                # Phase 5: before declaring done, surface any pending_dedup rows
                # parked by Phase 4. The user arbitrates which ambiguous source
                # messages are the same as target candidates (skip) vs. truly
                # different (upload). Job stays bound to the task so the reply
                # routes back here.
                pending = await self.agent.media_db.list_pending_dedup_by_job(job_id)
                if pending:
                    await self.agent.db.update_job_status(job_id, "awaiting_dedup")
                    summary = format_ambiguous_summary(pending)
                    await self.agent.ws_send_result(task_id, AgentResult(
                        status=TaskStatus.NEED_INPUT,
                        message=(
                            f"搬移完成（待確認 {len(pending)} 則歧異）\n"
                            f"成功：{progress['success']} 則｜"
                            f"跳過：{progress['skipped']} 則｜"
                            f"失敗：{progress['failed']} 則\n\n"
                            + summary
                        ),
                    ))
                    keep_pending_binding = True
                else:
                    await self.agent.ws_send_result(task_id, AgentResult(
                        status=TaskStatus.DONE,
                        message=f"搬移完成\n"
                                f"來源：{job['source_chat']}\n"
                                f"目標：{job['target_chat']}\n"
                                f"成功：{progress['success']} 則\n"
                                f"跳過：{progress['skipped']} 則\n"
                                f"失敗：{progress['failed']} 則",
                    ))
        except Exception as e:
            logger.error(f"Batch transfer error: {e}", exc_info=True)
            await self.agent.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"搬移失敗：{e}",
            ))
        finally:
            if not keep_pending_binding:
                self.agent._pending_jobs.pop(task_id, None)

    async def _run_defer_scan_background(
        self, task_id, job_id, job, messages, chat_id,
    ):
        """Walk the source messages, compute thumb_phash for photo/video,
        record metadata into deferred_dedup. No comparison, no upload — those
        happen later via /process_deferred.
        """
        await self.agent.db.update_job_status(job_id, "running")
        try:
            recorded = 0
            skipped = 0
            for msg in messages:
                if self.agent.engine._cancelled and job_id in self.agent.engine._cancelled:
                    self.agent.engine._cancelled.discard(job_id)
                    await self.agent.db.update_job_status(job_id, "cancelled")
                    await self.agent.ws_send_result(task_id, AgentResult(
                        status=TaskStatus.DONE,
                        message=f"延後掃描已取消（已記錄 {recorded} 則）",
                    ))
                    return

                if self.agent.engine.should_skip(msg):
                    await self.agent.db.mark_message(job_id, msg.id, "skipped")
                    skipped += 1
                    continue

                ftype = self.agent.engine._detect_file_type(msg) if msg.media else None
                if not ftype and not msg.text:
                    await self.agent.db.mark_message(job_id, msg.id, "skipped")
                    skipped += 1
                    continue

                # Photo/video → fetch thumb + phash. Documents/audio → skip
                # phash; we still record metadata so process_deferred can do
                # caption-based reasoning.
                thumb_phash = None
                if ftype in ("photo", "video"):
                    try:
                        thumb_phash = await download_thumb_and_phash(
                            self.agent.tg_client, msg,
                        )
                    except Exception as e:
                        # A bad thumb shouldn't fail the whole row — just
                        # record without phash and let process_deferred fall
                        # back to full-file path.
                        logger.warning(f"defer scan thumb failed for {msg.id}: {e}")

                file_obj = getattr(msg, "file", None)
                file_size = getattr(file_obj, "size", None) if file_obj else None
                duration = getattr(file_obj, "duration", None) if file_obj else None
                caption = getattr(msg, "message", None) or getattr(msg, "text", None)

                await self.agent.media_db.insert_deferred_dedup(
                    source_chat=job["source_chat"],
                    source_msg_id=int(msg.id),
                    target_chat=job["target_chat"],
                    thumb_phash=thumb_phash,
                    file_type=ftype,
                    file_size=file_size,
                    caption=caption,
                    duration=duration,
                    grouped_id=int(msg.grouped_id) if msg.grouped_id else None,
                )
                await self.agent.db.mark_message(job_id, msg.id, "success")
                recorded += 1

                if recorded % self.agent.engine.progress_interval == 0:
                    await self.agent.ws_send_progress(
                        task_id, chat_id,
                        f"延後掃描進度：{recorded}/{len(messages)}",
                    )

            await self.agent.db.update_job_status(job_id, "completed")
            await self.agent.ws_send_result(task_id, AgentResult(
                status=TaskStatus.DONE,
                message=(
                    f"延後掃描完成\n"
                    f"來源：{job['source_chat']}\n"
                    f"目標：{job['target_chat']}\n"
                    f"已記錄：{recorded} 則｜跳過：{skipped} 則\n\n"
                    f"請執行 /process_deferred 進行比對與上傳"
                ),
            ))
        except Exception as e:
            logger.error(f"Defer scan error: {e}", exc_info=True)
            await self.agent.db.update_job_status(job_id, "failed")
            await self.agent.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"延後掃描失敗：{e}",
            ))
        finally:
            self.agent._pending_jobs.pop(task_id, None)

    async def _run_process_deferred_background(
        self, task_id, job_id, source_chat, target_chat, rows, chat_id,
    ):
        """Background driver for /process_deferred. Mirrors _run_batch_background
        in shape: report progress via WS, surface Phase 5 summary on finish."""
        await self.agent.db.update_job_status(job_id, "running")
        keep_pending_binding = False
        try:
            source_entity = await resolve_chat(self.agent.tg_client, source_chat)
            target_entity = await resolve_chat(self.agent.tg_client, target_chat)
            uploaded = skipped = ambiguous = failed = 0

            for row in rows:
                src_msg_id = int(row["source_msg_id"])
                thumb_phash = row.get("thumb_phash")

                try:
                    if thumb_phash:
                        candidates = await self.agent.media_db.find_by_thumb_phash(
                            thumb_phash, target_chat,
                        )
                    else:
                        candidates = []

                    matched_cand = None
                    for cand in candidates:
                        if (cand.get("caption") == row.get("caption")
                                and cand.get("file_size") == row.get("file_size")
                                and cand.get("duration") == row.get("duration")):
                            matched_cand = cand
                            break

                    if matched_cand:
                        # Strict match → confirm dedup, upgrade thumb_only row,
                        # don't upload.
                        await self.agent.media_db.upgrade_thumb_to_full(
                            matched_cand["media_id"], verified_by="metadata",
                        )
                        await self.agent.db.mark_message(job_id, src_msg_id, "skipped")
                        skipped += 1
                    elif candidates:
                        # Thumb hit but metadata disagreed → park for Phase 5.
                        await self.agent.media_db.insert_pending_dedup(
                            job_id=job_id, source_chat=source_chat,
                            source_msg_id=src_msg_id,
                            candidate_target_msg_ids=[
                                c["target_msg_id"] for c in candidates
                                if c.get("target_msg_id")
                            ],
                            reason="thumb_match_metadata_mismatch",
                        )
                        await self.agent.db.mark_message(job_id, src_msg_id, "ambiguous")
                        ambiguous += 1
                    else:
                        # No candidate → upload. skip_pre_dedup so we don't
                        # re-do the thumb lookup we just performed manually.
                        msg = await self.agent.tg_client.get_messages(
                            source_entity, ids=src_msg_id,
                        )
                        if msg is None:
                            await self.agent.db.mark_message(
                                job_id, src_msg_id, "failed",
                                error="message deleted",
                            )
                            failed += 1
                        else:
                            result = await self.agent.engine.transfer_single(
                                source_entity, target_entity, msg,
                                target_chat=target_chat,
                                source_chat=source_chat,
                                job_id=job_id,
                                skip_pre_dedup=True,
                                task_id=task_id,
                            )
                            if result.get("ok"):
                                await self.agent.db.mark_message(
                                    job_id, src_msg_id, "success",
                                )
                                uploaded += 1
                            elif result.get("dedup"):
                                await self.agent.db.mark_message(
                                    job_id, src_msg_id, "skipped",
                                )
                                skipped += 1
                            else:
                                await self.agent.db.mark_message(
                                    job_id, src_msg_id, "failed",
                                )
                                failed += 1
                finally:
                    # Drop the deferred row regardless of outcome — failed
                    # uploads stay tracked via job_messages, not here.
                    await self.agent.media_db.delete_deferred_dedup(int(row["id"]))

            pending = await self.agent.media_db.list_pending_dedup_by_job(job_id)
            if pending:
                await self.agent.db.update_job_status(job_id, "awaiting_dedup")
                summary = format_ambiguous_summary(pending)
                await self.agent.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.NEED_INPUT,
                    message=(
                        f"延後比對完成（待確認 {len(pending)} 則歧異）\n"
                        f"上傳：{uploaded}｜跳過：{skipped}｜"
                        f"歧異：{ambiguous}｜失敗：{failed}\n\n"
                        + summary
                    ),
                ))
                keep_pending_binding = True
            else:
                await self.agent.db.update_job_status(job_id, "completed")
                await self.agent.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.DONE,
                    message=(
                        f"延後比對完成\n"
                        f"來源：{source_chat}\n"
                        f"目標：{target_chat}\n"
                        f"上傳：{uploaded}｜跳過：{skipped}｜失敗：{failed}"
                    ),
                ))
        except Exception as e:
            logger.error(f"process_deferred error: {e}", exc_info=True)
            await self.agent.db.update_job_status(job_id, "failed")
            await self.agent.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"延後比對失敗：{e}",
            ))
        finally:
            if not keep_pending_binding:
                self.agent._pending_jobs.pop(task_id, None)
