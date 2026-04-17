import asyncio
import json
import os
import re
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from agents.tg_transfer.parser import parse_tg_link, detect_forward, classify_intent
from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.tg_client import create_client

logger = logging.getLogger(__name__)

_TARGET_RE = re.compile(r"(?:改成|設定為?|set\s+to)\s*(@\w+|https?://t\.me/\S+)", re.IGNORECASE)


class TGTransferAgent(BaseAgent):
    def __init__(self, hub_url: str, port: int = 0):
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(agent_dir=agent_dir, hub_url=hub_url, port=port)
        self.db: TransferDB = None
        self.tg_client = None
        self.engine: TransferEngine = None
        self._pending_jobs: dict[str, str] = {}  # task_id → job_id

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
        session_path = os.path.join(data_dir, session_name)
        self.tg_client = await create_client(session_path)

        self.engine = TransferEngine(
            client=self.tg_client,
            db=self.db,
            tmp_dir=os.path.join(data_dir, "tmp"),
            retry_limit=settings.get("retry_limit", 3),
            progress_interval=settings.get("progress_interval", 20),
        )

        # Resume interrupted jobs
        running_jobs = await self.db.get_running_jobs()
        for job in running_jobs:
            logger.info(f"Found interrupted job {job['job_id']}, will resume on next dispatch")

    async def handle_task(self, task: TaskRequest) -> AgentResult:
        try:
            return await self._dispatch(task)
        except Exception as e:
            logger.error(f"handle_task error: {e}", exc_info=True)
            return AgentResult(status=TaskStatus.ERROR, message=f"執行失敗：{e}")

    async def _dispatch(self, task: TaskRequest) -> AgentResult:
        content = task.content
        metadata = {}
        if task.conversation_history:
            metadata = task.conversation_history[-1].get("metadata", {})

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

        # Batch — use AI to parse
        return await self._handle_batch_request(task)

    async def _handle_single(self, task: TaskRequest, chat_id, message_id: int) -> AgentResult:
        target_chat = await self.db.get_config("default_target_chat")
        if not target_chat:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="尚未設定預設目標群組。請先設定：「預設目標改成 @群組名稱」",
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

            ok = await self.engine.transfer_album(target_entity, album_msgs)
            count = len(album_msgs)
        else:
            if self.engine.should_skip(msg):
                return AgentResult(status=TaskStatus.DONE, message="已跳過（不支援的訊息類型）")
            ok = await self.engine.transfer_single(source_entity, target_entity, msg)
            count = 1

        if ok:
            return AgentResult(status=TaskStatus.DONE, message=f"已轉存 {count} 則訊息到 {target_chat}")
        return AgentResult(status=TaskStatus.ERROR, message="轉存失敗")

    async def _handle_config(self, content: str) -> AgentResult:
        m = _TARGET_RE.search(content)
        if m:
            target = m.group(1)
            await self.db.set_config("default_target_chat", target)
            return AgentResult(status=TaskStatus.DONE, message=f"預設目標已設為 {target}")
        return AgentResult(
            status=TaskStatus.NEED_INPUT,
            message="請告訴我目標群組，例如：「預設目標改成 @channel_name」",
        )

    async def _handle_batch_request(self, task: TaskRequest) -> AgentResult:
        """Parse batch command with AI, return estimate for confirmation."""
        content = task.content
        parsed = await self._ai_parse_batch(content)
        if not parsed:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="我沒有理解你的搬移指令，可以再說一次嗎？\n"
                        "例如：「把 @source_channel 的內容搬到 @target」\n"
                        "或：「搬移 @source 最近 100 則到 @target」",
            )

        source = parsed["source"]
        target = parsed.get("target") or await self.db.get_config("default_target_chat")
        if not target:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="請指定目標群組，或先設定預設目標",
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
        """Populate job_messages and start batch transfer."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])

        filter_type = job["filter_type"] or "all"
        filter_value = json.loads(job["filter_value"]) if job["filter_value"] else None
        messages = await self._collect_messages(source_entity, filter_type, filter_value)

        # Dedup
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

        async def report_fn(text):
            pass  # Progress tracked in DB, reported via next handle_task

        status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)

        if status == "paused":
            progress = await self.db.get_progress(job_id)
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=f"搬移暫停\n"
                        f"進度：{progress['success']}/{progress['total']}\n"
                        f"請選擇：重試 / 跳過 / 一律跳過",
            )

        del self._pending_jobs[task_id]
        progress = await self.db.get_progress(job_id)
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"搬移完成\n"
                    f"來源：{job['source_chat']}\n"
                    f"目標：{job['target_chat']}\n"
                    f"成功：{progress['success']} 則\n"
                    f"跳過：{progress['skipped']} 則\n"
                    f"失敗：{progress['failed']} 則",
        )

    async def _resume_batch(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Resume a paused batch job."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])

        async def report_fn(text):
            pass

        status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)

        if status == "paused":
            progress = await self.db.get_progress(job_id)
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=f"搬移暫停\n"
                        f"進度：{progress['success']}/{progress['total']}\n"
                        f"請選擇：重試 / 跳過 / 一律跳過",
            )

        del self._pending_jobs[task_id]
        progress = await self.db.get_progress(job_id)
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"搬移完成\n"
                    f"來源：{job['source_chat']}\n"
                    f"目標：{job['target_chat']}\n"
                    f"成功：{progress['success']} 則\n"
                    f"跳過：{progress['skipped']} 則\n"
                    f"失敗：{progress['failed']} 則",
        )

    async def _ai_parse_batch(self, content: str) -> dict | None:
        """Use Gemini Flash to parse natural language batch command."""
        prompt = (
            "你是一個指令解析器。從以下使用者訊息中提取搬移參數，回覆 JSON：\n"
            '{"source": "@channel 或連結", "target": "@channel 或連結 或 null", '
            '"filter_type": "all 或 count 或 date_range", '
            '"filter_value_raw": null 或 {"count": N} 或 {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}\n\n'
            f"使用者訊息：{content}\n\n只回覆 JSON，不要解釋。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", prompt, "-m", "gemini-2.5-flash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = stdout.decode().strip()
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

    async def run(self) -> None:
        await self._init_services()
        await super().run()


async def main():
    logging.basicConfig(level=logging.INFO)
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))

    agent = TGTransferAgent(hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
