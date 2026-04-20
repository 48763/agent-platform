import asyncio
import json
import os
import re
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aiohttp import web
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from agents.tg_transfer.parser import (
    parse_tg_link, detect_forward, classify_intent, parse_threshold,
    parse_index_target_chat, ai_classify_command,
)
from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.tg_client import create_client
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.indexer import TargetIndexer
from agents.tg_transfer.byte_budget import ByteBudget
from agents.tg_transfer.search import format_search_results, format_similar_results
from agents.tg_transfer.hasher import compute_phash, hamming_distance, PHASH_AVAILABLE
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
        self._search_state: dict[str, dict] = {}  # task_id → {keyword, page}
        self._current_chat_id: dict[str, int] = {}  # task_id → chat_id

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

        # Resume interrupted jobs (running + paused)
        await self._resume_interrupted_jobs()

    async def _resume_interrupted_jobs(self):
        """On startup re-attach running/paused jobs to their original TG task.
        Running jobs get respawned in background; paused jobs get a reminder
        message so user can reply retry/skip/skip-all."""
        jobs = await self.db.get_resumable_jobs()
        for job in jobs:
            task_id = job.get("task_id")
            chat_id = job.get("chat_id")
            if not task_id or not chat_id:
                logger.warning(
                    f"Job {job['job_id']} has no task_id/chat_id binding, cannot resume"
                )
                continue

            self._pending_jobs[task_id] = job["job_id"]
            self._current_chat_id[task_id] = chat_id

            if job["status"] == "running":
                try:
                    source_entity = await resolve_chat(self.tg_client, job["source_chat"])
                    target_entity = await resolve_chat(self.tg_client, job["target_chat"])
                except Exception as e:
                    logger.error(f"Resume {job['job_id']} resolve failed: {e}")
                    continue
                await self.ws_send_progress(
                    task_id, chat_id, f"繼續搬移任務 {job['job_id']}"
                )
                asyncio.create_task(
                    self._run_batch_background(
                        task_id, job["job_id"], job, source_entity, target_entity, chat_id
                    )
                )
            elif job["status"] == "paused":
                await self.ws_send_progress(
                    task_id, chat_id,
                    f"服務重啟，關於任務 {job['job_id']}，請回覆：重試 / 跳過 / 一律跳過",
                )

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

    async def _dispatch(self, task: TaskRequest) -> AgentResult:
        content = task.content
        metadata = {}
        if task.conversation_history:
            metadata = task.conversation_history[-1].get("metadata", {})

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
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="尚未設定預設目標。請先設定：「預設目標改成 @名稱」（群組/頻道/用戶/bot 皆可）",
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

            ok = await self.engine.transfer_album(target_entity, album_msgs)
            count = len(album_msgs)
        else:
            if self.engine.should_skip(msg):
                return AgentResult(status=TaskStatus.DONE, message="已跳過（不支援的訊息類型）")
            result = await self.engine.transfer_single(
                source_entity, target_entity, msg,
                target_chat=target_chat, source_chat=str(chat_id), job_id=None,
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
        all_phashes = await self.media_db.get_all_phashes()
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
        """Parse batch command with AI, return estimate for confirmation."""
        content = task.content
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

        # Check dedup
        already_done = await self.db.get_transferred_message_ids(source, target)
        new_count = count - len(already_done) if already_done else count

        # Create job but don't start yet
        job_id = await self.db.create_job(
            source_chat=source,
            target_chat=target,
            mode="batch",
            filter_type=filter_type,
            filter_value=json.dumps(parsed.get("filter_value_raw")) if parsed.get("filter_value_raw") else None,
            task_id=task.task_id,
            chat_id=task.chat_id,
        )
        self._pending_jobs[task.task_id] = job_id

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
            # Batch confirmation
            if content in ("是", "yes", "y", "確認", "ok"):
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

        filter_type = job["filter_type"] or "all"
        filter_value = json.loads(job["filter_value"]) if job["filter_value"] else None
        messages = await self._collect_messages(source_entity, filter_type, filter_value)

        already_done = await self.db.get_transferred_message_ids(job["source_chat"], job["target_chat"])
        grouped_ids = {}
        msg_ids = []
        for msg in messages:
            if msg.id in already_done:
                continue
            msg_ids.append(msg.id)
            if msg.grouped_id:
                grouped_ids[msg.id] = msg.grouped_id

        await self.db.add_messages(job_id, msg_ids, grouped_ids)

        chat_id = self._current_chat_id.get(task_id, 0)

        # Notify user via progress (not result, so task stays active)
        await self.ws_send_progress(task_id, chat_id,
            f"開始搬移 {len(msg_ids)} 則訊息\n來源：{job['source_chat']}\n目標：{job['target_chat']}")

        # Start batch in event loop (non-blocking)
        asyncio.create_task(
            self._run_batch_background(task_id, job_id, job, source_entity, target_entity, chat_id)
        )

        # Return None — result will be sent by _run_batch_background when done
        return None

    async def _run_batch_background(self, task_id: str, job_id: str, job: dict,
                                     source_entity, target_entity, chat_id: int):
        """Run batch transfer and report via WS."""
        async def report_fn(text):
            await self.ws_send_progress(task_id, chat_id, text)

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
            self._pending_jobs.pop(task_id, None)

    async def _resume_batch(self, task_id: str, job_id: str, job: dict):
        """Resume a paused batch job (non-blocking)."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])
        chat_id = self._current_chat_id.get(task_id, 0)

        # If the reply came in under a new task_id (e.g. hub created a fresh
        # task), rewrite the DB binding so future progress goes to the new task.
        if job.get("task_id") != task_id or job.get("chat_id") != chat_id:
            await self.db.update_job_binding(job_id, task_id, chat_id)

        await self.ws_send_progress(task_id, chat_id, "繼續搬移中...")

        asyncio.create_task(
            self._run_batch_background(task_id, job_id, job, source_entity, target_entity, chat_id)
        )

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
