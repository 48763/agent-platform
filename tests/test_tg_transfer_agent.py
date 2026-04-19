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
