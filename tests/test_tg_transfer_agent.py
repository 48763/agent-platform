import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.models import TaskRequest, TaskStatus


class TestHandleTaskDispatch:
    """Test that handle_task routes to correct handler based on input."""

    @pytest.mark.asyncio
    async def test_link_triggers_single_transfer(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.get_config = AsyncMock(return_value="@backup")
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.engine.transfer_single = AsyncMock(return_value={"ok": True, "dedup": False, "similar": None})
            agent.engine.should_skip = MagicMock(return_value=False)
            agent.config = {"settings": {"retry_limit": 3, "progress_interval": 20}}
            agent._pending_jobs = {}
            agent._search_state = {}
            agent._current_chat_id = {}
            agent.media_db = AsyncMock()
            agent._init_error = ""

            task = TaskRequest(task_id="t1", content="https://t.me/channel/123")

            with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
                mock_resolve.return_value = MagicMock()
                msg = MagicMock()
                msg.text = "hello"
                msg.media = None
                msg.grouped_id = None
                agent.tg_client.get_messages = AsyncMock(return_value=msg)
                result = await agent.handle_task(task)

            assert result.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_config_update(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.set_config = AsyncMock()
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.config = {"settings": {"retry_limit": 3, "progress_interval": 20}}
            agent._pending_jobs = {}
            agent._search_state = {}
            agent._current_chat_id = {}
            agent.media_db = AsyncMock()
            agent._init_error = ""

            task = TaskRequest(task_id="t2", content="預設目標改成 @my_backup")
            result = await agent.handle_task(task)

            assert result.status == TaskStatus.DONE
            agent.db.set_config.assert_called_once_with("default_target_chat", "@my_backup")

    @pytest.mark.asyncio
    async def test_forward_triggers_transfer(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.get_config = AsyncMock(return_value="@backup")
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.engine.transfer_single = AsyncMock(return_value={"ok": True, "dedup": False, "similar": None})
            agent.engine.should_skip = MagicMock(return_value=False)
            agent.config = {"settings": {"retry_limit": 3, "progress_interval": 20}}
            agent._pending_jobs = {}
            agent._search_state = {}
            agent._current_chat_id = {}
            agent.media_db = AsyncMock()
            agent._init_error = ""

            task = TaskRequest(
                task_id="t3",
                content="轉發的訊息內容",
                conversation_history=[{
                    "role": "user",
                    "content": "轉發的訊息內容",
                    "metadata": {"forward_chat_id": -1001234567890, "forward_message_id": 42},
                }],
            )

            with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
                mock_resolve.return_value = MagicMock()
                msg = MagicMock()
                msg.text = "hello"
                msg.media = None
                msg.grouped_id = None
                agent.tg_client.get_messages = AsyncMock(return_value=msg)
                result = await agent.handle_task(task)

            assert result.status == TaskStatus.DONE


class TestIncrementalTargetSyncBeforeBatch:
    """Phase 3 — right before a batch transfer starts, the target chat must
    be incrementally re-indexed so Phase 4 dedup can reliably skip files
    the target already has. Uses last_scanned_msg_id stored in config."""

    @pytest.mark.asyncio
    async def test_start_batch_triggers_target_scan(self):
        from agents.tg_transfer.__main__ import TGTransferAgent
        agent = TGTransferAgent.__new__(TGTransferAgent)
        agent.db = AsyncMock()
        agent.db.add_messages = AsyncMock()
        agent.db.get_transferred_message_ids = AsyncMock(return_value=set())
        agent.media_db = AsyncMock()
        agent.tg_client = AsyncMock()
        agent.tg_client.get_messages = AsyncMock()
        agent.engine = AsyncMock()
        agent.config = {"settings": {}}
        agent._pending_jobs = {}
        agent._current_chat_id = {"tid": 111}
        agent._init_error = ""
        agent.ws_send_progress = AsyncMock()
        agent.ws_send_result = AsyncMock()

        job = {
            "source_chat": "@s", "target_chat": "@t",
            "filter_type": None, "filter_value": None,
        }

        scan_calls = []

        async def fake_scan(self_idx, target_chat, total_hint=None, progress_cb=None):
            scan_calls.append(target_chat)
            return {"scanned": 0, "inserted": 0}

        with patch(
            "agents.tg_transfer.__main__.resolve_chat",
            new_callable=AsyncMock,
        ) as mock_resolve, patch(
            "agents.tg_transfer.__main__.TargetIndexer.scan_target",
            new=fake_scan,
        ), patch.object(
            TGTransferAgent, "_collect_messages",
            new_callable=AsyncMock, return_value=[],
        ):
            mock_resolve.return_value = MagicMock()
            await agent._start_batch("tid", "jid", job)

        assert "@t" in scan_calls, (
            "Phase 3: _start_batch must scan the target chat before "
            "processing source messages"
        )


class TestLLMFallbackRouting:
    """When the regex classifier falls through to 'batch' but the LLM
    recognises the fuzzy phrasing as a known command, dispatch should route
    to that command instead of treating it as a batch request."""

    def _build_agent(self):
        from agents.tg_transfer.__main__ import TGTransferAgent
        agent = TGTransferAgent.__new__(TGTransferAgent)
        agent.db = AsyncMock()
        agent.db.set_config = AsyncMock()
        agent.db.get_config = AsyncMock(return_value=None)
        agent.tg_client = AsyncMock()
        agent.engine = AsyncMock()
        agent.media_db = AsyncMock()
        agent.media_db.get_stats = AsyncMock(return_value={
            "total_media": 0, "total_tags": 0, "tag_counts": [],
        })
        agent.config = {"settings": {}}
        agent._pending_jobs = {}
        agent._search_state = {}
        agent._current_chat_id = {}
        agent._init_error = ""
        agent.llm = MagicMock()
        return agent

    @pytest.mark.asyncio
    async def test_fuzzy_threshold_routes_to_threshold_handler(self):
        """'我不想要超過 500 的' has no regex trigger, but LLM says threshold."""
        agent = self._build_agent()

        async def fake_ai_classify(content, llm):
            return {"intent": "threshold", "params": {"mb": 500}}

        with patch(
            "agents.tg_transfer.__main__.ai_classify_command",
            side_effect=fake_ai_classify,
        ):
            task = TaskRequest(task_id="fuzzy1", content="我不想要超過 500 的檔案了")
            result = await agent.handle_task(task)

        assert result.status == TaskStatus.DONE
        agent.db.set_config.assert_called_once_with("size_limit_mb", "500")

    @pytest.mark.asyncio
    async def test_fuzzy_stats_routes_to_stats_handler(self):
        agent = self._build_agent()

        async def fake_ai_classify(content, llm):
            return {"intent": "stats", "params": {}}

        with patch(
            "agents.tg_transfer.__main__.ai_classify_command",
            side_effect=fake_ai_classify,
        ):
            task = TaskRequest(task_id="fuzzy2", content="看一下目前儲存狀況")
            result = await agent.handle_task(task)

        assert result.status == TaskStatus.DONE
        agent.media_db.get_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_returns_none_falls_back_to_batch(self):
        """When LLM can't classify, we proceed to the original batch path."""
        agent = self._build_agent()

        async def fake_ai_classify(content, llm):
            return None

        async def fake_ai_parse_batch(content):
            return None
        agent._ai_parse_batch = fake_ai_parse_batch

        with patch(
            "agents.tg_transfer.__main__.ai_classify_command",
            side_effect=fake_ai_classify,
        ):
            task = TaskRequest(task_id="fuzzy3", content="完全看不懂的句子")
            result = await agent.handle_task(task)

        assert result.status == TaskStatus.NEED_INPUT
