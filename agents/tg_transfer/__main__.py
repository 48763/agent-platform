import asyncio
import json
import os
import re
import shutil
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aiohttp import web
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from agents.tg_transfer.parser import (
    parse_tg_link, detect_forward, classify_intent, parse_threshold,
    parse_index_target_chat, ai_classify_command, has_skip_dedup_flag,
)
from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.tg_client import create_client
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.indexer import TargetIndexer
from agents.tg_transfer.byte_budget import ByteBudget
from agents.tg_transfer.search import format_search_results, format_similar_results
from agents.tg_transfer.ambiguous import (
    format_ambiguous_summary, parse_ambiguous_reply,
)
from agents.tg_transfer.hasher import (
    compute_phash, hamming_distance, PHASH_AVAILABLE, download_thumb_and_phash,
)
from agents.tg_transfer.liveness_checker import run_liveness_loop
from agents.tg_transfer.dashboard import create_tg_dashboard_handler

logger = logging.getLogger(__name__)

_TARGET_RE = re.compile(r"(?:改成|設定為?|set\s+to)\s*(@\w+|https?://t\.me/\S+)", re.IGNORECASE)


class TGTransferAgent(BaseAgent):
    def __init__(self, hub_url: str, port: int = 0):
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(agent_dir=agent_dir, hub_url=hub_url, port=port)
        self.db: TransferDB = None
        self.media_db: MediaDB = None
        self.tg_client = None
        self.engine: TransferEngine = None
        self._pending_jobs: dict[str, str] = {}  # task_id → job_id
        self._bg_tasks: dict[str, asyncio.Task] = {}  # task_id → batch bg task
        self._search_state: dict[str, dict] = {}  # task_id → {keyword, page}
        self._current_chat_id: dict[str, int] = {}  # task_id → chat_id
        self._awaiting_target: dict[str, dict] = {}  # task_id → {chat, message_id}

    async def _init_services(self):
        data_dir = os.environ.get("DATA_DIR", "/data/tg_transfer")
        os.makedirs(data_dir, exist_ok=True)

        self.db = TransferDB(os.path.join(data_dir, "transfer.db"))
        await self.db.init()

        # Load default_target_chat from yaml if not in DB yet
        settings = self.config.get("settings", {})
        yaml_target = settings.get("default_target_chat", "")
        if yaml_target and not await self.db.get_config("default_target_chat"):
            await self.db.set_config("default_target_chat", yaml_target)

        session_name = settings.get("telethon_session", "tg_transfer")
        session_dir = os.environ.get("SESSION_DIR", data_dir)
        session_path = os.path.join(session_dir, session_name)
        self.tg_client = await create_client(session_path)

        # Media DB
        self.media_db = MediaDB(os.path.join(data_dir, "transfer.db"))
        await self.media_db.init()

        # Global byte budget: caps total in-flight download bytes across all
        # concurrent transfers. Default 1 GiB; configurable via
        # settings.byte_budget_mb.
        budget_mb = int(settings.get("byte_budget_mb", 1024))
        byte_budget = ByteBudget(capacity=budget_mb * 1024 * 1024)

        self.engine = TransferEngine(
            client=self.tg_client,
            db=self.db,
            tmp_dir=os.path.join(data_dir, "tmp"),
            retry_limit=settings.get("retry_limit", 3),
            progress_interval=settings.get("progress_interval", 20),
            media_db=self.media_db,
            phash_threshold=settings.get("phash_threshold", 10),
            byte_budget=byte_budget,
        )

        # Start liveness checker
        interval = settings.get("liveness_check_interval", 24)
        asyncio.create_task(run_liveness_loop(self.tg_client, self.media_db, interval))

        # Resume is triggered by on_ws_connected(), not here,
        # because WS must be up before we can send progress/result.

    async def on_ws_connected(self):
        """Resume interrupted jobs on every WS connect.

        Runs every time (not just first connect) so hub restarts or WS flaps
        that silently killed a `_run_batch_background` coroutine can recover.
        Re-spawn is guarded by a liveness check on the tracked asyncio.Task so
        healthy jobs aren't double-spawned. Paused-job user reminders only fire
        on the first connect to avoid spamming on every reconnect.

        Orphan scan runs once on first connect — it's idempotent but only
        useful when the agent has just (re)started.
        """
        first_connect = not getattr(self, '_resumed', False)
        self._resumed = True
        if first_connect:
            await self._migrate_legacy_tmp_layout()
        await self._resume_interrupted_jobs(first_connect=first_connect)
        if first_connect:
            await self._scan_orphan_task_dirs()

    def _spawn_batch_bg(self, task_id: str, job_id: str, job: dict,
                         source_entity, target_entity, chat_id: int) -> asyncio.Task:
        """Start a batch background coroutine and record it for liveness checks."""
        bg = asyncio.create_task(
            self._run_batch_background(
                task_id, job_id, job, source_entity, target_entity, chat_id
            )
        )
        self._bg_tasks[task_id] = bg
        return bg

    async def _fetch_hub_task_statuses(self, task_ids: list[str]) -> dict[str, str]:
        """Ask hub for the current status of each task_id.

        On any failure (hub down, network hiccup), return {} — caller falls back
        to the old behavior of resuming and letting a PROGRESS→CANCEL round-trip
        stop the job. The pre-filter is an optimization, not a correctness gate.
        """
        if not task_ids:
            return {}
        try:
            from aiohttp import ClientSession
            async with ClientSession() as session:
                async with session.post(
                    f"{self.hub_url}/task_statuses",
                    json={"task_ids": task_ids},
                    timeout=5,
                ) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json()
                    return data.get("statuses") or {}
        except Exception as e:
            logger.warning(f"Hub task_statuses query failed: {e}")
            return {}

    async def _resume_interrupted_jobs(self, first_connect: bool = True):
        """Re-attach running/paused jobs to their original TG task.

        - Running jobs: re-spawn if no live background task is tracking them.
        - Paused jobs: always keep the in-memory binding (so retry/skip replies
          route correctly); only send the reminder message on the first connect.

        Before spawning anything, ask the hub for each task's current status.
        Tasks the user has already closed/archived (or that hub has no record
        of) get their job marked `cancelled` and are skipped — otherwise we'd
        briefly download bytes for a closed task before the hub's CANCEL round-
        trip stopped us.
        """
        jobs = await self.db.get_resumable_jobs()
        task_ids = [j["task_id"] for j in jobs if j.get("task_id")]
        hub_statuses = await self._fetch_hub_task_statuses(task_ids)
        for job in jobs:
            task_id = job.get("task_id")
            chat_id = job.get("chat_id")
            if not task_id or not chat_id:
                logger.warning(
                    f"Job {job['job_id']} has no task_id/chat_id binding, cannot resume"
                )
                continue

            hub_status = hub_statuses.get(task_id)
            # Only pre-filter when we actually got an answer from the hub —
            # empty hub_statuses (hub unreachable) must NOT trigger cancels.
            if hub_status in ("closed", "archived", "missing"):
                logger.info(
                    f"Skipping resume of job {job['job_id']}: "
                    f"hub task {task_id} status={hub_status}"
                )
                await self.db.update_job_status(job["job_id"], "cancelled")
                continue

            if job["status"] == "running":
                bg = self._bg_tasks.get(task_id)
                if bg is not None and not bg.done():
                    # Still actively running in this process — nothing to do.
                    continue
                try:
                    source_entity = await resolve_chat(self.tg_client, job["source_chat"])
                    target_entity = await resolve_chat(self.tg_client, job["target_chat"])
                except Exception as e:
                    logger.error(f"Resume {job['job_id']} resolve failed: {e}")
                    continue
                self._pending_jobs[task_id] = job["job_id"]
                self._current_chat_id[task_id] = chat_id
                await self.ws_send_progress(
                    task_id, chat_id, f"繼續搬移任務 {job['job_id']}"
                )
                self._spawn_batch_bg(
                    task_id, job["job_id"], job, source_entity, target_entity, chat_id
                )
            elif job["status"] == "paused":
                self._pending_jobs[task_id] = job["job_id"]
                self._current_chat_id[task_id] = chat_id
                if first_connect:
                    await self.ws_send_progress(
                        task_id, chat_id,
                        f"服務重啟，關於任務 {job['job_id']}，請回覆：重試 / 跳過 / 一律跳過",
                    )
            elif job["status"] == "awaiting_dedup":
                # Phase 5: queue already shown to user. Just restore the
                # in-memory binding so the eventual reply routes back to
                # _handle_dedup_response. Re-sending the summary on every
                # reconnect would spam the user with duplicate lists.
                self._pending_jobs[task_id] = job["job_id"]
                self._current_chat_id[task_id] = chat_id

    async def handle_task(self, task: TaskRequest) -> AgentResult:
        if self._init_error:
            return AgentResult(
                status=TaskStatus.ERROR,
                message=f"Agent 初始化失敗，無法處理任務：{self._init_error}",
            )
        self._current_chat_id[task.task_id] = task.chat_id
        try:
            return await self._dispatch(task)
        except Exception as e:
            logger.error(f"handle_task error: {e}", exc_info=True)
            return AgentResult(status=TaskStatus.ERROR, message=f"執行失敗：{e}")

    def on_cancel(self, task_id: str):
        if task_id in self._pending_jobs:
            job_id = self._pending_jobs[task_id]
            self.engine.cancel_job(job_id)

    def on_task_deleted(self, task_id: str):
        """Hub deleted this conversation. Schedule async cleanup of the
        task-scoped cache directory and DB rows. Synchronous-from-WS-loop
        wrapper around _on_task_deleted_async."""
        asyncio.create_task(self._on_task_deleted_async(task_id))

    async def _on_task_deleted_async(self, task_id: str):
        # Cancel any live background coroutine first so it doesn't write
        # into a directory we're about to remove.
        bg = self._bg_tasks.pop(task_id, None)
        if bg is not None and not bg.done():
            bg.cancel()
            # Await the cancellation to guarantee the bg coroutine has fully
            # stopped before we proceed to remove its files.
            try:
                await bg
            except (asyncio.CancelledError, Exception):
                # CancelledError is expected; other exceptions inside the bg task
                # were already that task's problem to log. We just need to be sure
                # it's stopped before we touch its files.
                pass
        # Mark cancelled so any in-flight engine.run_batch loop bails out.
        # Look up the job_id for this task so engine.cancel_job is keyed
        # correctly (engine cancels by job_id, not task_id).
        job_id = self._pending_jobs.pop(task_id, None)
        if job_id:
            self.engine.cancel_job(job_id)

        # Drop other in-memory bindings.
        self._current_chat_id.pop(task_id, None)
        self._search_state.pop(task_id, None)
        self._awaiting_target.pop(task_id, None)

        # Remove DB rows. Errors here are non-fatal; orphan scan on next
        # startup will retry.
        try:
            await self.db.delete_jobs_by_task(task_id)
        except Exception as e:
            logger.warning(f"delete_jobs_by_task({task_id}) failed: {e}")

        # Remove the per-task cache directory.
        task_dir = os.path.join(self.engine.tmp_dir, task_id)
        try:
            shutil.rmtree(task_dir)
        except FileNotFoundError:
            pass  # already gone, nothing to do
        except Exception as e:
            logger.warning(f"rmtree({task_dir}) failed (will retry on orphan scan): {e}")

        logger.info(f"Cleaned up cache + DB for deleted task {task_id}")

    async def _migrate_legacy_tmp_layout(self):
        """One-shot migration from the flat tmp/ layout to per-task subdirs.

        Pre-Task-3, downloads landed directly in tmp_dir as `{msg}_{uuid}.ext`.
        After Task 3, every file is under `tmp/{task_id}/`. Root-level files
        from the old layout can't be reliably re-attributed to a task, so we:

        1. Remove every regular file at the root of tmp_dir.
        2. Reset all job_messages.partial_path / downloaded_bytes — those
           rows referenced absolute paths under the flat layout that we just
           wiped, so resume must re-download from byte 0.
        3. Drop a `.migrated_v2` flag file so subsequent boots skip this.

        Subdirectories are left alone — they're either pre-existing manual
        creations (rare) or the new per-task layout (after a partial deploy).
        """
        tmp_root = self.engine.tmp_dir
        if not os.path.isdir(tmp_root):
            os.makedirs(tmp_root, exist_ok=True)
        flag = os.path.join(tmp_root, ".migrated_v2")
        if os.path.exists(flag):
            return

        removed = 0
        for entry in os.listdir(tmp_root):
            if entry.startswith("."):
                continue
            full = os.path.join(tmp_root, entry)
            if os.path.isfile(full):
                try:
                    os.remove(full)
                    removed += 1
                except Exception as e:
                    logger.warning(f"Legacy migration: failed to remove {full}: {e}")

        try:
            reset_rows = await self.db.clear_all_partials()
        except Exception as e:
            logger.warning(f"Legacy migration: clear_all_partials failed: {e}")
            reset_rows = 0

        try:
            with open(flag, "w") as f:
                f.write("v2\n")
        except Exception as e:
            logger.warning(f"Legacy migration: failed to write flag: {e}")

        logger.info(
            f"Legacy migration done: removed {removed} root-level files, "
            f"reset {reset_rows} partial-download rows"
        )

    async def _scan_orphan_task_dirs(self):
        """Remove tmp/{task_id}/ directories whose task_id has no active job.

        Rationale: hub may have deleted a conversation while this agent was
        offline, so we never received TASK_DELETED. On startup, anything
        not pointed at by an active job in the DB is dead weight — clear it.

        Only operates on direct subdirectories of tmp_dir. Ignores files at
        the root level (those are handled by the legacy migration in
        Task 8) and dotfiles (e.g. .migrated_v2 flag)."""
        tmp_root = self.engine.tmp_dir
        if not os.path.isdir(tmp_root):
            return
        try:
            active_ids = await self.db.get_active_task_ids()
        except Exception as e:
            logger.warning(f"get_active_task_ids failed during orphan scan: {e}")
            return
        for entry in os.listdir(tmp_root):
            if entry.startswith("."):
                continue
            full = os.path.join(tmp_root, entry)
            if not os.path.isdir(full):
                continue
            if entry in active_ids:
                continue
            try:
                shutil.rmtree(full, ignore_errors=True)
                logger.info(f"Orphan scan removed {full}")
            except Exception as e:
                logger.warning(f"Orphan scan failed to remove {full}: {e}")

    async def _dispatch(self, task: TaskRequest) -> AgentResult:
        content = task.content
        metadata = {}
        if task.conversation_history:
            metadata = task.conversation_history[-1].get("metadata", {})

        # Awaiting target: user previously sent a link but no target was
        # configured. They're now replying with the target chat.
        if task.task_id in self._awaiting_target:
            return await self._handle_target_reply(task)

        # Threshold change must be accepted even while a job is in-flight,
        # otherwise the user can't lower the limit mid-run. Check BEFORE
        # routing to _handle_paused_response.
        if classify_intent(content) == "threshold":
            return await self._handle_threshold(content)

        # Check if this is a response to a paused job
        if task.task_id in self._pending_jobs:
            return await self._handle_paused_response(task)

        # Check for forwarded message
        fwd = detect_forward(content, metadata)
        if fwd:
            return await self._handle_single(task, fwd.chat, fwd.message_id)

        # Classify intent
        intent = classify_intent(content)

        if intent == "single_transfer":
            link = parse_tg_link(content)
            return await self._handle_single(task, link.chat, link.message_id)

        if intent == "config":
            return await self._handle_config(content)

        if intent == "stats":
            return await self._handle_stats()

        if intent == "index_target":
            return await self._handle_index_target(task, content)

        if intent == "process_deferred":
            return await self._handle_process_deferred(task)

        if intent == "page":
            return await self._handle_page(task)

        if intent == "search":
            return await self._handle_search(task)

        # Regex fell through to 'batch'. Before committing to the batch path,
        # let the LLM take a pass — fuzzy phrasing like "我不想要超過 500 的檔案"
        # should route to threshold, not batch. None = LLM couldn't classify.
        ai_cmd = await ai_classify_command(content, getattr(self, "llm", None))
        if ai_cmd:
            routed = await self._route_ai_command(task, ai_cmd)
            if routed is not None:
                return routed

        # Batch — use AI to parse
        return await self._handle_batch_request(task)

    async def _route_ai_command(self, task: TaskRequest, ai_cmd: dict):
        """Dispatch a command that the LLM fuzzy-classifier produced. Returns
        an AgentResult, or None to indicate the caller should fall through to
        the regex-path default."""
        intent = ai_cmd.get("intent")
        params = ai_cmd.get("params") or {}
        if intent == "threshold":
            mb = params.get("mb")
            if isinstance(mb, int) and mb >= 0:
                await self.db.set_config("size_limit_mb", str(mb))
                if mb == 0:
                    return AgentResult(status=TaskStatus.DONE, message="已取消大小門檻")
                return AgentResult(
                    status=TaskStatus.DONE,
                    message=f"大小門檻已改為 {mb} MB（下一則訊息起生效）",
                )
            # LLM picked threshold but gave us no usable number — fall back to
            # the regex handler so it can ask the user for a value.
            return await self._handle_threshold(task.content)
        if intent == "config":
            return await self._handle_config(task.content)
        if intent == "stats":
            return await self._handle_stats()
        if intent == "search":
            return await self._handle_search(task)
        # 'batch' — let the normal batch path handle it.
        return None

    async def _handle_single(self, task: TaskRequest, chat_id, message_id: int) -> AgentResult:
        target_chat = await self.db.get_config("default_target_chat")
        if not target_chat:
            self._awaiting_target[task.task_id] = {
                "chat": chat_id, "message_id": message_id,
            }
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="請指定目標（群組/頻道/用戶/bot），例如 @my_backup\n"
                        "回覆後會自動設為預設目標並開始轉存",
            )

        source_entity = await self.tg_client.get_entity(chat_id)
        target_entity = await resolve_chat(self.tg_client, target_chat)

        msg = await self.tg_client.get_messages(source_entity, ids=message_id)
        if msg is None:
            return AgentResult(status=TaskStatus.ERROR, message="找不到該訊息，可能已被刪除")

        # Check for album
        if msg.grouped_id:
            nearby = await self.tg_client.get_messages(
                source_entity, ids=range(message_id - 10, message_id + 10)
            )
            album_msgs = [m for m in nearby if m and m.grouped_id == msg.grouped_id]
            album_msgs.sort(key=lambda m: m.id)

            if self.engine.should_skip(album_msgs[0]):
                return AgentResult(status=TaskStatus.DONE, message="已跳過（不支援的訊息類型）")

            # Respect size_limit_mb for direct album links too.
            if await self.engine._album_over_limit(album_msgs):
                return AgentResult(
                    status=TaskStatus.DONE, message="已跳過（超過大小門檻）",
                )

            ok = await self.engine.transfer_album(
                target_entity, album_msgs,
                task_id=task.task_id,
            )
            count = len(album_msgs)
        else:
            if self.engine.should_skip(msg):
                return AgentResult(status=TaskStatus.DONE, message="已跳過（不支援的訊息類型）")
            result = await self.engine.transfer_single(
                source_entity, target_entity, msg,
                target_chat=target_chat, source_chat=str(chat_id), job_id=None,
                task_id=task.task_id,
            )
            if result.get("over_limit"):
                return AgentResult(
                    status=TaskStatus.DONE, message="已跳過（超過大小門檻）",
                )
            if result["similar"]:
                text = format_similar_results(result["similar"])
                return AgentResult(status=TaskStatus.NEED_INPUT, message=text)
            if result["dedup"]:
                return AgentResult(status=TaskStatus.DONE, message="已存在相同媒體，跳過")
            ok = result["ok"]
            count = 1

        if ok:
            return AgentResult(status=TaskStatus.DONE, message=f"已轉存 {count} 則訊息到 {target_chat}")
        return AgentResult(status=TaskStatus.ERROR, message="轉存失敗")

    async def _handle_target_reply(self, task: TaskRequest) -> AgentResult:
        """User replied with a target chat after we asked for one."""
        pending = self._awaiting_target.pop(task.task_id)
        target_chat = task.content.strip()
        try:
            await resolve_chat(self.tg_client, target_chat)
        except Exception:
            # Put state back so user can retry
            self._awaiting_target[task.task_id] = pending
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=f"找不到「{target_chat}」，請確認名稱後重新輸入",
            )
        await self.db.set_config("default_target_chat", target_chat)
        return await self._handle_single(task, pending["chat"], pending["message_id"])

    async def _handle_threshold(self, content: str) -> AgentResult:
        """Change the global per-message / album-sum size threshold.
        Takes effect on the NEXT message in any running batch (engine reads
        size_limit_mb from DB config on every message)."""
        mb = parse_threshold(content)
        if mb is None:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="請提供數字和單位，例如：「門檻改成 200MB」或「限制 1GB」",
            )
        await self.db.set_config("size_limit_mb", str(mb))
        if mb == 0:
            return AgentResult(status=TaskStatus.DONE, message="已取消大小門檻")
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"大小門檻已改為 {mb} MB（下一則訊息起生效）",
        )

    async def _handle_config(self, content: str) -> AgentResult:
        m = _TARGET_RE.search(content)
        if m:
            target = m.group(1)
            await self.db.set_config("default_target_chat", target)
            return AgentResult(status=TaskStatus.DONE, message=f"預設目標已設為 {target}")
        return AgentResult(
            status=TaskStatus.NEED_INPUT,
            message="請告訴我目標，例如：「預設目標改成 @name」（群組/頻道/用戶/bot 皆可）",
        )

    async def _handle_search(self, task: TaskRequest) -> AgentResult:
        """Handle keyword or image search."""
        content = task.content
        metadata = {}
        if task.conversation_history:
            metadata = task.conversation_history[-1].get("metadata", {})

        # Check if user sent an image (for image search)
        if metadata.get("has_photo"):
            return await self._handle_image_search(task, metadata)

        # Keyword search — strip search trigger words
        keyword = re.sub(r"(搜尋|查詢|search|找)\s*", "", content, flags=re.IGNORECASE).strip()
        if not keyword:
            return AgentResult(status=TaskStatus.NEED_INPUT, message="請輸入搜尋關鍵字")

        page_size = self.config.get("settings", {}).get("search_page_size", 10)
        results, total = await self.media_db.search_keyword(keyword, page=1, page_size=page_size)
        text = format_search_results(results, total, page=1, page_size=page_size)

        if total > page_size:
            self._search_state[task.task_id] = {"keyword": keyword, "page": 1}

        return AgentResult(status=TaskStatus.DONE, message=text)

    async def _handle_image_search(self, task: TaskRequest, metadata: dict) -> AgentResult:
        """Handle image-based similar search."""
        if not PHASH_AVAILABLE:
            return AgentResult(status=TaskStatus.DONE, message="pHash 不可用，僅支援關鍵字搜尋")

        photo_path = metadata.get("photo_path")
        if not photo_path:
            return AgentResult(status=TaskStatus.DONE, message="無法取得圖片")

        phash = compute_phash(photo_path)
        if not phash:
            return AgentResult(status=TaskStatus.DONE, message="無法計算圖片 hash")

        threshold = self.config.get("settings", {}).get("phash_threshold", 10)
        # Photo-only candidates so we don't try to int(csv, 16) a video's
        # multi-frame phash (would raise ValueError) and don't surface
        # cross-format false positives.
        all_phashes = await self.media_db.get_all_phashes(file_type="photo")
        similar = []
        for row in all_phashes:
            dist = hamming_distance(phash, row["phash"])
            if dist <= threshold:
                similar.append({**row, "distance": dist})
        similar.sort(key=lambda x: x["distance"])

        text = format_similar_results(similar)
        return AgentResult(status=TaskStatus.DONE, message=text)

    async def _handle_page(self, task: TaskRequest) -> AgentResult:
        """Handle pagination for search results."""
        state = self._search_state.get(task.task_id)
        if not state:
            return AgentResult(status=TaskStatus.DONE, message="沒有進行中的搜尋")

        content = task.content.strip().lower()
        page_size = self.config.get("settings", {}).get("search_page_size", 10)

        if "下一頁" in content or "next" in content:
            state["page"] += 1
        elif "上一頁" in content or "prev" in content:
            state["page"] = max(1, state["page"] - 1)

        results, total = await self.media_db.search_keyword(
            state["keyword"], page=state["page"], page_size=page_size
        )
        text = format_search_results(results, total, page=state["page"], page_size=page_size)
        return AgentResult(status=TaskStatus.DONE, message=text)

    async def _handle_index_target(self, task: TaskRequest, content: str):
        """Run a thumb-only scan over the target chat and update media_db.
        Returns None so the task stays active — progress and completion are
        pushed via ws_send_progress / ws_send_result.
        """
        chat = parse_index_target_chat(content)
        if chat is None:
            chat = await self.db.get_config("default_target_chat")
        if not chat:
            return AgentResult(
                status=TaskStatus.ERROR,
                message="沒有指定目標對話，且未設定 default_target_chat。\n"
                        "用法：/index_target @your_target",
            )

        task_id = task.task_id
        chat_id = self._current_chat_id.get(task_id, 0)

        await self.ws_send_progress(
            task_id, chat_id, f"開始索引目標：{chat}"
        )
        asyncio.create_task(
            self._run_index_background(task_id, chat_id, chat)
        )
        return None

    async def _run_index_background(
        self, task_id: str, chat_id: int, target_chat: str,
    ):
        try:
            entity = await resolve_chat(self.tg_client, target_chat)
            total = 0
            try:
                total = await self.tg_client.get_messages(entity, limit=0).total
            except Exception:
                # `total` is best-effort — scan still works, we just can't
                # produce a percentage. iter_messages will eventually drain.
                total = None

            async def progress_cb(scanned, total_n):
                await self.ws_send_progress(
                    task_id, chat_id,
                    f"索引中：{scanned}/{total_n}",
                )

            indexer = TargetIndexer(
                client=self.tg_client, tdb=self.db, mdb=self.media_db,
            )
            stats = await indexer.scan_target(
                target_chat, total_hint=total, progress_cb=progress_cb,
            )
            await self.ws_send_result(task_id, AgentResult(
                status=TaskStatus.DONE,
                message=(
                    f"索引完成：{target_chat}\n"
                    f"掃描 {stats['scanned']} 則，新增/更新 "
                    f"{stats['inserted']} 筆 thumb 記錄"
                ),
            ))
        except Exception as e:
            logger.error(f"index_target error: {e}", exc_info=True)
            await self.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"索引失敗：{e}",
            ))

    async def _handle_stats(self) -> AgentResult:
        """Return media stats as text."""
        stats = await self.media_db.get_stats()
        lines = [
            f"儲存媒體：{stats['total_media']} 筆",
            f"標籤總數：{stats['total_tags']} 個",
        ]
        if stats["tag_counts"]:
            lines.append("\n標籤統計：")
            for name, count in stats["tag_counts"][:20]:
                lines.append(f"  #{name} — {count} 筆")
        return AgentResult(status=TaskStatus.DONE, message="\n".join(lines))

    async def _handle_batch_request(self, task: TaskRequest) -> AgentResult:
        """Parse batch command with AI, return estimate for confirmation.

        If the user appended `--skip-dedup` (or 中文 alias), we'll create the
        job with mode='defer_scan' instead of 'batch'. The confirm path then
        routes to a thumb-only scan that records metadata into deferred_dedup,
        and `/process_deferred` does the actual upload/dedup later.
        """
        content = task.content
        defer_mode = has_skip_dedup_flag(content)
        parsed = await self._ai_parse_batch(content)
        if not parsed:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="我沒有理解你的搬移指令，可以再說一次嗎？\n"
                        "例如：「把 @source 的內容搬到 @target」（來源可以是群組/頻道/用戶/bot）\n"
                        "或：「搬移 @source 最近 100 則到 @target」",
            )

        source = parsed["source"]
        target = parsed.get("target") or await self.db.get_config("default_target_chat")
        if not target:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="請指定目標（群組/頻道/用戶/bot），或先設定預設目標",
            )

        source_entity = await resolve_chat(self.tg_client, source)
        filter_type = parsed.get("filter_type", "all")
        filter_value = parsed.get("filter_value")

        # Count messages
        count = await self._count_messages(source_entity, filter_type, filter_value)

        # Defer-scan mode skips the historic-job dedup count — the whole
        # point is to record everything and resolve later via /process_deferred.
        already_done: set[int] = set()
        if not defer_mode:
            already_done = await self.db.get_transferred_message_ids(source, target, media_db=self.media_db)
        new_count = count - len(already_done) if already_done else count

        # Create job but don't start yet
        job_id = await self.db.create_job(
            source_chat=source,
            target_chat=target,
            mode="defer_scan" if defer_mode else "batch",
            filter_type=filter_type,
            filter_value=json.dumps(parsed.get("filter_value_raw")) if parsed.get("filter_value_raw") else None,
            task_id=task.task_id,
            chat_id=task.chat_id,
        )
        self._pending_jobs[task.task_id] = job_id

        if defer_mode:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=(
                    f"來源：{source}\n目標：{target}\n"
                    f"符合條件的訊息：約 {count} 則\n"
                    f"模式：--skip-dedup（只下縮圖記錄 metadata，不上傳）\n"
                    f"事後請執行 /process_deferred 比對並上傳\n\n"
                    f"確認執行？（是/否）"
                ),
            )

        dedup_note = f"（其中 {len(already_done)} 則已搬過，將跳過）" if already_done else ""
        return AgentResult(
            status=TaskStatus.NEED_INPUT,
            message=f"來源：{source}\n目標：{target}\n"
                    f"符合條件的訊息：約 {count} 則{dedup_note}\n"
                    f"預計搬移：{new_count} 則\n\n確認執行？（是/否）",
        )

    async def _handle_paused_response(self, task: TaskRequest) -> AgentResult:
        """Handle user response to a paused job or batch confirmation."""
        job_id = self._pending_jobs[task.task_id]
        job = await self.db.get_job(job_id)
        content = task.content.strip().lower()

        if job["status"] == "pending":
            # Batch / defer-scan confirmation
            if content in ("是", "yes", "y", "確認", "ok"):
                if job.get("mode") == "defer_scan":
                    return await self._start_defer_scan(task.task_id, job_id, job)
                return await self._start_batch(task.task_id, job_id, job)
            else:
                del self._pending_jobs[task.task_id]
                await self.db.update_job_status(job_id, "failed")
                return AgentResult(status=TaskStatus.DONE, message="已取消")

        if job["status"] == "paused":
            if content in ("重試", "retry"):
                return await self._resume_batch(task.task_id, job_id, job)
            elif content in ("跳過", "skip"):
                await self._skip_current_failed(job_id)
                return await self._resume_batch(task.task_id, job_id, job)
            elif content in ("一律跳過", "skip all", "auto skip"):
                await self.db.set_auto_skip(job_id, True)
                await self._skip_current_failed(job_id)
                return await self._resume_batch(task.task_id, job_id, job)

        if job["status"] == "awaiting_dedup":
            return await self._handle_dedup_response(task, job_id, job)

        if job["status"] == "running":
            return AgentResult(status=TaskStatus.DONE, message="搬移進行中，請等待完成")

        return AgentResult(status=TaskStatus.NEED_INPUT, message="請選擇：重試 / 跳過 / 一律跳過")

    async def _skip_current_failed(self, job_id: str):
        """Find failed messages in job and mark as skipped."""
        async with self.db._db.execute(
            "SELECT message_id FROM job_messages WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            await self.db.mark_message(job_id, row["message_id"], "skipped")

    async def _start_batch(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Populate job_messages and start batch transfer (non-blocking)."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])

        # Phase 3: incrementally sync the target chat's thumb index so the
        # upcoming source-side dedup (Phase 4) sees any messages added to
        # target since our last scan. scan_target is a no-op when nothing
        # new appeared, so the normal-case cost is one iter_messages call.
        chat_id = self._current_chat_id.get(task_id, 0)

        async def sync_progress_cb(scanned, total):
            await self.ws_send_progress(
                task_id, chat_id, f"同步目標索引：{scanned}/{total}"
            )

        try:
            total_msgs = None
            try:
                m = await self.tg_client.get_messages(target_entity, limit=0)
                total_msgs = getattr(m, "total", None)
            except Exception:
                total_msgs = None
            indexer = TargetIndexer(
                client=self.tg_client, tdb=self.db, mdb=self.media_db,
            )
            await indexer.scan_target(
                job["target_chat"], total_hint=total_msgs,
                progress_cb=sync_progress_cb,
            )
        except Exception as e:
            # Sync failure shouldn't block the batch — dedup will just fall
            # through to the full-file path for anything the index missed.
            logger.warning(f"pre-batch target sync failed: {e}")

        filter_type = job["filter_type"] or "all"
        filter_value = json.loads(job["filter_value"]) if job["filter_value"] else None
        messages = await self._collect_messages(source_entity, filter_type, filter_value)

        already_done = await self.db.get_transferred_message_ids(job["source_chat"], job["target_chat"], media_db=self.media_db)
        grouped_ids = {}
        msg_ids = []
        for msg in messages:
            if msg.id in already_done:
                continue
            msg_ids.append(msg.id)
            if msg.grouped_id:
                grouped_ids[msg.id] = msg.grouped_id

        await self.db.add_messages(job_id, msg_ids, grouped_ids)

        # Notify user via progress (not result, so task stays active)
        await self.ws_send_progress(task_id, chat_id,
            f"開始搬移 {len(msg_ids)} 則訊息\n來源：{job['source_chat']}\n目標：{job['target_chat']}")

        # Start batch in event loop (non-blocking)
        self._spawn_batch_bg(task_id, job_id, job, source_entity, target_entity, chat_id)

        # Return None — result will be sent by _run_batch_background when done
        return None

    async def _run_batch_background(self, task_id: str, job_id: str, job: dict,
                                     source_entity, target_entity, chat_id: int):
        """Run batch transfer and report via WS."""
        async def report_fn(text):
            await self.ws_send_progress(task_id, chat_id, text)

        keep_pending_binding = False
        try:
            status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)
            progress = await self.db.get_progress(job_id)

            if status == "paused":
                await self.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.NEED_INPUT,
                    message=f"搬移暫停\n"
                            f"進度：{progress['success']}/{progress['total']}\n"
                            f"請選擇：重試 / 跳過 / 一律跳過",
                ))
            elif status == "cancelled":
                await self.ws_send_result(task_id, AgentResult(
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
                pending = await self.media_db.list_pending_dedup_by_job(job_id)
                if pending:
                    await self.db.update_job_status(job_id, "awaiting_dedup")
                    summary = format_ambiguous_summary(pending)
                    await self.ws_send_result(task_id, AgentResult(
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
                    await self.ws_send_result(task_id, AgentResult(
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
            await self.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"搬移失敗：{e}",
            ))
        finally:
            if not keep_pending_binding:
                self._pending_jobs.pop(task_id, None)
            self._bg_tasks.pop(task_id, None)

    async def _start_defer_scan(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Confirm + kick off `/batch --skip-dedup`. The job_messages table is
        populated for progress accounting, but no upload happens — the bg
        coroutine only downloads thumbs and writes deferred_dedup rows."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        chat_id = self._current_chat_id.get(task_id, 0)

        filter_type = job["filter_type"] or "all"
        filter_value = json.loads(job["filter_value"]) if job["filter_value"] else None
        messages = await self._collect_messages(source_entity, filter_type, filter_value)
        msg_ids = [m.id for m in messages]
        grouped_ids = {m.id: m.grouped_id for m in messages if m.grouped_id}
        await self.db.add_messages(job_id, msg_ids, grouped_ids)

        await self.ws_send_progress(
            task_id, chat_id,
            f"開始延後掃描 {len(msg_ids)} 則訊息（只記 metadata，不上傳）",
        )

        bg = asyncio.create_task(
            self._run_defer_scan_background(task_id, job_id, job, messages, chat_id),
        )
        self._bg_tasks[task_id] = bg
        return None

    async def _run_defer_scan_background(
        self, task_id: str, job_id: str, job: dict, messages: list, chat_id: int,
    ):
        """Walk the source messages, compute thumb_phash for photo/video,
        record metadata into deferred_dedup. No comparison, no upload — those
        happen later via /process_deferred.
        """
        await self.db.update_job_status(job_id, "running")
        try:
            recorded = 0
            skipped = 0
            for msg in messages:
                if self.engine._cancelled and job_id in self.engine._cancelled:
                    self.engine._cancelled.discard(job_id)
                    await self.db.update_job_status(job_id, "cancelled")
                    await self.ws_send_result(task_id, AgentResult(
                        status=TaskStatus.DONE,
                        message=f"延後掃描已取消（已記錄 {recorded} 則）",
                    ))
                    return

                if self.engine.should_skip(msg):
                    await self.db.mark_message(job_id, msg.id, "skipped")
                    skipped += 1
                    continue

                ftype = self.engine._detect_file_type(msg) if msg.media else None
                if not ftype and not msg.text:
                    await self.db.mark_message(job_id, msg.id, "skipped")
                    skipped += 1
                    continue

                # Photo/video → fetch thumb + phash. Documents/audio → skip
                # phash; we still record metadata so process_deferred can do
                # caption-based reasoning.
                thumb_phash = None
                if ftype in ("photo", "video"):
                    try:
                        thumb_phash = await download_thumb_and_phash(
                            self.tg_client, msg,
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

                await self.media_db.insert_deferred_dedup(
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
                await self.db.mark_message(job_id, msg.id, "success")
                recorded += 1

                if recorded % self.engine.progress_interval == 0:
                    await self.ws_send_progress(
                        task_id, chat_id,
                        f"延後掃描進度：{recorded}/{len(messages)}",
                    )

            await self.db.update_job_status(job_id, "completed")
            await self.ws_send_result(task_id, AgentResult(
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
            await self.db.update_job_status(job_id, "failed")
            await self.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"延後掃描失敗：{e}",
            ))
        finally:
            self._pending_jobs.pop(task_id, None)
            self._bg_tasks.pop(task_id, None)

    async def _handle_process_deferred(self, task: TaskRequest):
        """Drain deferred_dedup: per row, do the Phase-4 thumb lookup against
        target index. No candidates → upload (skip_pre_dedup so we don't re-
        thumb-dedup against ourselves). All-metadata match → mark dedup +
        upgrade. Mismatch → push into pending_dedup so Phase 5 can surface
        the ambiguous queue at end.
        """
        rows = await self.media_db.list_deferred_dedup()
        if not rows:
            return AgentResult(
                status=TaskStatus.DONE,
                message="沒有延後比對的項目",
            )

        # Group rows by (source_chat, target_chat) so each pair runs as one
        # job — that lets Phase 5's ambiguous queue stay scoped per job_id.
        task_id = task.task_id
        chat_id = task.chat_id
        self._current_chat_id[task_id] = chat_id

        # For v1 we collapse all rows into one job using the first row's
        # source/target. If you have multiple source→target pairs queued, run
        # /process_deferred once per pair (the queue will only drain matching
        # rows on each run because we filter by source_chat/target_chat below).
        first = rows[0]
        source_chat = first["source_chat"]
        target_chat = first["target_chat"]
        scoped = [
            r for r in rows
            if r["source_chat"] == source_chat and r["target_chat"] == target_chat
        ]

        job_id = await self.db.create_job(
            source_chat=source_chat,
            target_chat=target_chat,
            mode="process_deferred",
            task_id=task_id,
            chat_id=chat_id,
        )
        await self.db.add_messages(
            job_id, [int(r["source_msg_id"]) for r in scoped],
        )
        self._pending_jobs[task_id] = job_id

        await self.ws_send_progress(
            task_id, chat_id,
            f"開始處理延後佇列：{len(scoped)} 則（{source_chat} → {target_chat}）",
        )

        bg = asyncio.create_task(
            self._run_process_deferred_background(
                task_id, job_id, source_chat, target_chat, scoped, chat_id,
            ),
        )
        self._bg_tasks[task_id] = bg
        return None

    async def _run_process_deferred_background(
        self, task_id: str, job_id: str, source_chat: str, target_chat: str,
        rows: list[dict], chat_id: int,
    ):
        """Background driver for /process_deferred. Mirrors _run_batch_background
        in shape: report progress via WS, surface Phase 5 summary on finish."""
        await self.db.update_job_status(job_id, "running")
        keep_pending_binding = False
        try:
            source_entity = await resolve_chat(self.tg_client, source_chat)
            target_entity = await resolve_chat(self.tg_client, target_chat)
            uploaded = skipped = ambiguous = failed = 0

            for row in rows:
                src_msg_id = int(row["source_msg_id"])
                thumb_phash = row.get("thumb_phash")

                try:
                    if thumb_phash:
                        candidates = await self.media_db.find_by_thumb_phash(
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
                        await self.media_db.upgrade_thumb_to_full(
                            matched_cand["media_id"], verified_by="metadata",
                        )
                        await self.db.mark_message(job_id, src_msg_id, "skipped")
                        skipped += 1
                    elif candidates:
                        # Thumb hit but metadata disagreed → park for Phase 5.
                        await self.media_db.insert_pending_dedup(
                            job_id=job_id, source_chat=source_chat,
                            source_msg_id=src_msg_id,
                            candidate_target_msg_ids=[
                                c["target_msg_id"] for c in candidates
                                if c.get("target_msg_id")
                            ],
                            reason="thumb_match_metadata_mismatch",
                        )
                        await self.db.mark_message(job_id, src_msg_id, "ambiguous")
                        ambiguous += 1
                    else:
                        # No candidate → upload. skip_pre_dedup so we don't
                        # re-do the thumb lookup we just performed manually.
                        msg = await self.tg_client.get_messages(
                            source_entity, ids=src_msg_id,
                        )
                        if msg is None:
                            await self.db.mark_message(
                                job_id, src_msg_id, "failed",
                                error="message deleted",
                            )
                            failed += 1
                        else:
                            result = await self.engine.transfer_single(
                                source_entity, target_entity, msg,
                                target_chat=target_chat,
                                source_chat=source_chat,
                                job_id=job_id,
                                skip_pre_dedup=True,
                                task_id=task_id,
                            )
                            if result.get("ok"):
                                await self.db.mark_message(
                                    job_id, src_msg_id, "success",
                                )
                                uploaded += 1
                            elif result.get("dedup"):
                                await self.db.mark_message(
                                    job_id, src_msg_id, "skipped",
                                )
                                skipped += 1
                            else:
                                await self.db.mark_message(
                                    job_id, src_msg_id, "failed",
                                )
                                failed += 1
                finally:
                    # Drop the deferred row regardless of outcome — failed
                    # uploads stay tracked via job_messages, not here.
                    await self.media_db.delete_deferred_dedup(int(row["id"]))

            pending = await self.media_db.list_pending_dedup_by_job(job_id)
            if pending:
                await self.db.update_job_status(job_id, "awaiting_dedup")
                summary = format_ambiguous_summary(pending)
                await self.ws_send_result(task_id, AgentResult(
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
                await self.db.update_job_status(job_id, "completed")
                await self.ws_send_result(task_id, AgentResult(
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
            await self.db.update_job_status(job_id, "failed")
            await self.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"延後比對失敗：{e}",
            ))
        finally:
            if not keep_pending_binding:
                self._pending_jobs.pop(task_id, None)
            self._bg_tasks.pop(task_id, None)

    async def _handle_dedup_response(self, task, job_id: str, job: dict) -> AgentResult:
        """Phase 5: apply user's arbitration on ambiguous-dedup queue.

        Reply grammar handled in `parse_ambiguous_reply`:
          - "same 1a, 2b" → source [1] is a duplicate of target candidate a,
            source [2] is target candidate b. Rows mentioned → drop from queue
            without uploading. Rows NOT mentioned → upload (user said they're
            different).
          - "skip" → drop all queued rows; upload nothing.
          - unparseable → re-prompt (don't default to upload, to avoid
            surprising the user with a bulk upload from a typo).
        """
        task_id = task.task_id
        chat_id = self._current_chat_id.get(task_id, job.get("chat_id", 0))
        parsed = parse_ambiguous_reply(task.content)
        if parsed is None:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=(
                    "無法解析你的回覆。格式：「same 1a, 2b」表示 [1] 跟 a 相同、"
                    "[2] 跟 b 相同；未提到的會上傳。全部略過請回覆「skip」。"
                ),
            )

        pending = await self.media_db.list_pending_dedup_by_job(job_id)
        if not pending:
            # Nothing to resolve — likely a stale reply after another path
            # drained the queue. Close out the job cleanly.
            self._pending_jobs.pop(task_id, None)
            await self.db.update_job_status(job_id, "completed")
            return AgentResult(
                status=TaskStatus.DONE, message="沒有待確認的項目",
            )

        to_upload: list[int] = []
        resolved_same = 0

        if parsed == "skip":
            # User opts out of all uploads — just clear the queue.
            for row in pending:
                await self.media_db.delete_pending_dedup(row["id"])
            resolved_same = len(pending)
        else:
            # parsed is dict {source_idx: target_letter}
            for idx, row in enumerate(pending, start=1):
                if idx in parsed:
                    # Mentioned → user says this one matches a target
                    # candidate. Drop the queue row; don't upload.
                    await self.media_db.delete_pending_dedup(row["id"])
                    resolved_same += 1
                else:
                    # Unmentioned → user implicitly says "different, upload".
                    to_upload.append(int(row["source_msg_id"]))
                    await self.media_db.delete_pending_dedup(row["id"])

        uploaded = 0
        upload_failed = 0
        if to_upload:
            source_entity = await resolve_chat(self.tg_client, job["source_chat"])
            target_entity = await resolve_chat(self.tg_client, job["target_chat"])
            for msg_id in to_upload:
                try:
                    msg = await self.tg_client.get_messages(source_entity, ids=msg_id)
                    if msg is None:
                        upload_failed += 1
                        continue
                    result = await self.engine.transfer_single(
                        source_entity, target_entity, msg,
                        target_chat=job["target_chat"],
                        source_chat=job["source_chat"],
                        job_id=job_id,
                        skip_pre_dedup=True,
                        task_id=task_id,
                    )
                    if result.get("ok"):
                        uploaded += 1
                        # Flip the original 'ambiguous' job_messages row to
                        # success so progress counts stay consistent.
                        await self.db.mark_message(
                            job_id, msg_id, "success",
                        )
                    else:
                        upload_failed += 1
                except Exception as e:
                    logger.error(
                        f"Dedup upload failed for msg {msg_id}: {e}",
                        exc_info=True,
                    )
                    upload_failed += 1

        self._pending_jobs.pop(task_id, None)
        await self.db.update_job_status(job_id, "completed")

        summary_parts = [f"已確認 {resolved_same} 則為相同"]
        if uploaded:
            summary_parts.append(f"已上傳 {uploaded} 則")
        if upload_failed:
            summary_parts.append(f"上傳失敗 {upload_failed} 則")
        return AgentResult(
            status=TaskStatus.DONE,
            message="｜".join(summary_parts),
        )

    async def _resume_batch(self, task_id: str, job_id: str, job: dict):
        """Resume a paused batch job (non-blocking)."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])
        chat_id = self._current_chat_id.get(task_id, 0)

        # task_id is invariant per the _pending_jobs guarantee (hub's reply
        # routes back under the same task_id we registered, hub never reuses
        # a task_id, and _pending_jobs is keyed by task_id). Only chat_id
        # may diverge if the user replied from a different chat.
        if job.get("chat_id") != chat_id:
            await self.db.update_job_binding(job_id, task_id, chat_id)

        await self.ws_send_progress(task_id, chat_id, "繼續搬移中...")

        self._spawn_batch_bg(task_id, job_id, job, source_entity, target_entity, chat_id)

        return None  # result sent by _run_batch_background

    async def _ai_parse_batch(self, content: str) -> dict | None:
        """Use LLM to parse natural language batch command."""
        if not self.llm:
            return None
        prompt = (
            "你是一個指令解析器。從以下使用者訊息中提取搬移參數，回覆 JSON：\n"
            '{"source": "@username / 連結 / 數字 chat_id（可以是群組、頻道、用戶、bot 等任何聊天對象）", '
            '"target": "同 source 格式 或 null", '
            '"filter_type": "all 或 count 或 date_range", '
            '"filter_value_raw": null 或 {"count": N} 或 {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}\n\n'
            f"使用者訊息：{content}\n\n只回覆 JSON，不要解釋。"
        )
        try:
            text = await self.llm.prompt(prompt)
            # Extract JSON from response (may be wrapped in markdown)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"AI parse failed: {e}")
            return None

    async def _count_messages(self, entity, filter_type: str, filter_value) -> int:
        """Estimate message count based on filter."""
        if filter_type == "count" and filter_value:
            return filter_value.get("count", 0) if isinstance(filter_value, dict) else int(filter_value)

        count = 0
        async for msg in self.tg_client.iter_messages(entity, limit=None):
            if filter_type == "date_range" and filter_value:
                msg_date = msg.date.strftime("%Y-%m-%d")
                if isinstance(filter_value, dict):
                    if msg_date < filter_value.get("from", ""):
                        break
                    if msg_date > filter_value.get("to", ""):
                        continue
            count += 1
            if count >= 10000:  # Safety limit for estimation
                break
        return count

    async def _collect_messages(self, entity, filter_type: str, filter_value) -> list:
        """Collect all messages matching the filter."""
        messages = []
        limit = None

        if filter_type == "count" and filter_value:
            limit = filter_value.get("count", 100) if isinstance(filter_value, dict) else int(filter_value)

        async for msg in self.tg_client.iter_messages(entity, limit=limit):
            if filter_type == "date_range" and filter_value:
                msg_date = msg.date.strftime("%Y-%m-%d")
                if isinstance(filter_value, dict):
                    if msg_date < filter_value.get("from", ""):
                        break
                    if msg_date > filter_value.get("to", ""):
                        continue
            messages.append(msg)

        messages.reverse()  # Oldest first
        return messages

    def create_app(self) -> web.Application:
        app = super().create_app()
        app.router.add_get("/dashboard", create_tg_dashboard_handler(self.media_db))
        return app


async def main():
    logging.basicConfig(level=logging.INFO)
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))

    agent = TGTransferAgent(hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
