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
            agent._awaiting_target = {}
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
            agent._awaiting_target = {}
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
            agent._awaiting_target = {}
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
        from agents.tg_transfer.batch_controller import BatchController
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
        agent._batch_message_cache = {}
        agent._current_chat_id = {"tid": 111}
        agent._init_error = ""
        agent.ws_send_progress = AsyncMock()
        agent.ws_send_result = AsyncMock()
        agent.batch_controller = BatchController(agent)

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


class TestAwaitingTargetFlow:
    """When no default_target_chat is configured, the bot asks the user for a
    target. On the user's reply, the bot should set the target AND proceed
    with the original transfer — not re-classify the reply as a new command."""

    @pytest.mark.asyncio
    async def test_no_target_asks_then_reply_sets_and_transfers(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.get_config = AsyncMock(return_value=None)  # no default_target
            agent.db.set_config = AsyncMock()
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.engine.transfer_single = AsyncMock(
                return_value={"ok": True, "dedup": False, "similar": None},
            )
            agent.engine.should_skip = MagicMock(return_value=False)
            agent.config = {"settings": {}}
            agent._pending_jobs = {}
            agent._search_state = {}
            agent._current_chat_id = {}
            agent._awaiting_target = {}
            agent.media_db = AsyncMock()
            agent._init_error = ""
            agent.llm = None

            # Step 1: send link with no target configured → NEED_INPUT
            task1 = TaskRequest(task_id="t-target", content="https://t.me/channel/123")
            with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock):
                result1 = await agent.handle_task(task1)
            assert result1.status == TaskStatus.NEED_INPUT

            # Step 2: user replies with target → should set config + transfer
            # After set_config is called, get_config should return the new value.
            config_store = {}

            async def fake_set_config(key, val):
                config_store[key] = val

            async def fake_get_config(key):
                return config_store.get(key)

            agent.db.set_config = AsyncMock(side_effect=fake_set_config)
            agent.db.get_config = AsyncMock(side_effect=fake_get_config)
            task2 = TaskRequest(
                task_id="t-target",
                content="@my_backup",
                conversation_history=[
                    {"role": "user", "content": "https://t.me/channel/123"},
                    {"role": "assistant", "content": result1.message},
                    {"role": "user", "content": "@my_backup"},
                ],
            )
            with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
                mock_resolve.return_value = MagicMock()
                msg = MagicMock()
                msg.text = "hello"
                msg.media = None
                msg.grouped_id = None
                agent.tg_client.get_messages = AsyncMock(return_value=msg)
                result2 = await agent.handle_task(task2)

            assert result2.status == TaskStatus.DONE
            agent.db.set_config.assert_called_with("default_target_chat", "@my_backup")


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
        agent._awaiting_target = {}
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


class TestResumeOnReconnect:
    """Every WS reconnect must re-attach running jobs whose background
    coroutine silently died (e.g. hub restart killed the in-flight send).
    The first-connect-only behaviour in the original resume logic left
    jobs permanently stuck on reconnect."""

    def _build_agent(self):
        import asyncio
        from agents.tg_transfer.__main__ import TGTransferAgent
        from agents.tg_transfer.batch_controller import BatchController
        agent = TGTransferAgent.__new__(TGTransferAgent)
        agent.db = AsyncMock()
        agent.tg_client = AsyncMock()
        agent._pending_jobs = {}
        agent._current_chat_id = {}
        agent._awaiting_target = {}
        agent._search_state = {}
        agent.hub_url = "http://hub.test"
        # ws_send_progress is inherited from BaseAgent; stub it out.
        agent.ws_send_progress = AsyncMock()
        # Default: hub unreachable → no pre-filter (preserves legacy behavior
        # for tests that don't exercise the closed-task pre-filter path).
        agent._fetch_hub_task_statuses = AsyncMock(return_value={})
        agent.batch_controller = BatchController(agent)
        return agent

    @pytest.mark.asyncio
    async def test_running_job_respawned_when_bg_task_missing(self):
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j1", "status": "running",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t1", "chat_id": 100,
        }])
        spawned = []
        agent.batch_controller.spawn_batch = MagicMock(
            side_effect=lambda *a, **kw: spawned.append(a) or MagicMock()
        )

        with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock()
            await agent._resume_interrupted_jobs(first_connect=True)

        assert len(spawned) == 1
        assert spawned[0][0] == "t1"  # task_id
        assert spawned[0][1] == "j1"  # job_id
        agent.ws_send_progress.assert_called_once()

    @pytest.mark.asyncio
    async def test_running_job_skipped_when_bg_task_alive(self):
        """Double-spawn guard: if a background task is already running for this
        job, reconnect must NOT spawn another one."""
        import asyncio
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j1", "status": "running",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t1", "chat_id": 100,
        }])
        # Alive placeholder: a never-resolved future counts as not-done.
        alive = asyncio.get_event_loop().create_future()
        agent.batch_controller._bg_tasks["t1"] = alive
        agent.batch_controller.spawn_batch = MagicMock()

        await agent._resume_interrupted_jobs(first_connect=False)

        agent.batch_controller.spawn_batch.assert_not_called()
        agent.ws_send_progress.assert_not_called()
        alive.cancel()

    @pytest.mark.asyncio
    async def test_running_job_respawned_when_bg_task_done(self):
        """Silently-died bg task (done but job still 'running' in DB) must
        trigger a re-spawn on the next WS connect."""
        import asyncio
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j1", "status": "running",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t1", "chat_id": 100,
        }])
        dead = asyncio.get_event_loop().create_future()
        dead.set_result(None)
        agent.batch_controller._bg_tasks["t1"] = dead
        spawned = []
        agent.batch_controller.spawn_batch = MagicMock(
            side_effect=lambda *a, **kw: spawned.append(a) or MagicMock()
        )

        with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock()
            await agent._resume_interrupted_jobs(first_connect=False)

        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_closed_task_skipped_and_job_cancelled(self):
        """If hub reports the task as 'closed' (user closed it out-of-band),
        resume must skip re-spawn AND mark the job cancelled in DB so it stops
        showing up as 'running' forever. Without this, the agent would briefly
        download bytes before hub's CANCEL round-trip stopped it."""
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j-closed", "status": "running",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t-closed", "chat_id": 100,
        }])
        agent.db.update_job_status = AsyncMock()
        agent._fetch_hub_task_statuses = AsyncMock(return_value={
            "t-closed": "closed",
        })
        agent.batch_controller.spawn_batch = MagicMock()

        await agent._resume_interrupted_jobs(first_connect=True)

        agent.batch_controller.spawn_batch.assert_not_called()
        agent.ws_send_progress.assert_not_called()
        agent.db.update_job_status.assert_awaited_once_with("j-closed", "cancelled")

    @pytest.mark.asyncio
    async def test_missing_task_skipped_and_job_cancelled(self):
        """If hub has no record of the task (e.g. dashboard deletion), treat
        as closed and cancel the residual running job."""
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j-missing", "status": "running",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t-missing", "chat_id": 100,
        }])
        agent.db.update_job_status = AsyncMock()
        agent._fetch_hub_task_statuses = AsyncMock(return_value={
            "t-missing": "missing",
        })
        agent.batch_controller.spawn_batch = MagicMock()

        await agent._resume_interrupted_jobs(first_connect=True)

        agent.batch_controller.spawn_batch.assert_not_called()
        agent.db.update_job_status.assert_awaited_once_with("j-missing", "cancelled")

    @pytest.mark.asyncio
    async def test_hub_unreachable_falls_back_to_resume(self):
        """Pre-filter is an optimization, NOT a correctness gate. If hub is
        unreachable (empty statuses map) we must still try to resume — agent
        independence trumps small cancel-latency cost."""
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j-unk", "status": "running",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t-unk", "chat_id": 100,
        }])
        agent.db.update_job_status = AsyncMock()
        # empty = hub unreachable
        agent._fetch_hub_task_statuses = AsyncMock(return_value={})
        spawned = []
        agent.batch_controller.spawn_batch = MagicMock(
            side_effect=lambda *a, **kw: spawned.append(a) or MagicMock()
        )

        with patch(
            "agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock,
        ) as mock_resolve:
            mock_resolve.return_value = MagicMock()
            await agent._resume_interrupted_jobs(first_connect=True)

        assert len(spawned) == 1
        agent.db.update_job_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_job_reminder_first_connect_only(self):
        """User-facing 'please retry/skip' reminder only on the first connect,
        to avoid spamming on every WS flap."""
        agent = self._build_agent()
        agent.db.get_resumable_jobs = AsyncMock(return_value=[{
            "job_id": "j2", "status": "paused",
            "source_chat": "src", "target_chat": "tgt",
            "task_id": "t2", "chat_id": 200,
        }])

        await agent._resume_interrupted_jobs(first_connect=True)
        assert agent.ws_send_progress.call_count == 1
        assert "t2" in agent._pending_jobs

        # Second connect: binding preserved, no new reminder.
        agent.ws_send_progress.reset_mock()
        await agent._resume_interrupted_jobs(first_connect=False)
        agent.ws_send_progress.assert_not_called()
        assert agent._pending_jobs["t2"] == "j2"


class TestHandleDedupResponse:
    """Phase 5: user arbitrates ambiguous pending_dedup queue after batch end."""

    def _build_agent(self):
        from agents.tg_transfer.__main__ import TGTransferAgent
        from agents.tg_transfer.batch_controller import BatchController
        agent = TGTransferAgent.__new__(TGTransferAgent)
        agent.db = AsyncMock()
        agent.media_db = AsyncMock()
        agent.tg_client = AsyncMock()
        agent.engine = AsyncMock()
        agent._pending_jobs = {}
        agent._current_chat_id = {}
        agent._awaiting_target = {}
        agent._search_state = {}
        agent.hub_url = "http://hub.test"
        agent.batch_controller = BatchController(agent)
        return agent

    @pytest.mark.asyncio
    async def test_skip_clears_queue_no_uploads(self):
        agent = self._build_agent()
        job = {"job_id": "j1", "source_chat": "src", "target_chat": "tgt",
               "task_id": "t1", "chat_id": 100, "status": "awaiting_dedup"}
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[
            {"id": 1, "source_msg_id": 101, "candidate_target_msg_ids": [501]},
            {"id": 2, "source_msg_id": 102, "candidate_target_msg_ids": [502]},
        ])
        agent.media_db.delete_pending_dedup = AsyncMock()
        agent._pending_jobs["t1"] = "j1"

        task = TaskRequest(task_id="t1", content="skip")
        result = await agent._handle_dedup_response(task, "j1", job)

        assert result.status == TaskStatus.DONE
        # Both rows deleted, neither uploaded.
        assert agent.media_db.delete_pending_dedup.await_count == 2
        agent.engine.transfer_single.assert_not_called()
        # Binding released.
        assert "t1" not in agent._pending_jobs

    @pytest.mark.asyncio
    async def test_same_marks_resolved_without_upload(self):
        """User says [1] matches target candidate 'a' → that row is dropped
        and NOT uploaded. There's nothing else in the queue, so no uploads
        happen at all."""
        agent = self._build_agent()
        job = {"job_id": "j1", "source_chat": "src", "target_chat": "tgt",
               "task_id": "t1", "chat_id": 100, "status": "awaiting_dedup"}
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[
            {"id": 1, "source_msg_id": 101, "candidate_target_msg_ids": [501]},
        ])
        agent.media_db.delete_pending_dedup = AsyncMock()
        agent._pending_jobs["t1"] = "j1"

        task = TaskRequest(task_id="t1", content="same 1a")
        result = await agent._handle_dedup_response(task, "j1", job)

        assert result.status == TaskStatus.DONE
        agent.engine.transfer_single.assert_not_called()
        agent.media_db.delete_pending_dedup.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_unmentioned_rows_get_uploaded(self):
        """User says [1] is same, [2] is unmentioned → [2] must upload.
        Upload path MUST pass skip_pre_dedup=True — otherwise the same thumb
        collision would re-park the message in pending_dedup forever."""
        agent = self._build_agent()
        job = {"job_id": "j1", "source_chat": "src", "target_chat": "tgt",
               "task_id": "t1", "chat_id": 100, "status": "awaiting_dedup"}
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[
            {"id": 1, "source_msg_id": 101, "candidate_target_msg_ids": [501]},
            {"id": 2, "source_msg_id": 102, "candidate_target_msg_ids": [502]},
        ])
        agent.media_db.delete_pending_dedup = AsyncMock()
        agent.engine.transfer_single = AsyncMock(return_value={
            "ok": True, "dedup": False, "similar": None,
        })
        agent.tg_client.get_messages = AsyncMock(return_value=MagicMock())
        agent._pending_jobs["t1"] = "j1"

        task = TaskRequest(task_id="t1", content="same 1a")
        with patch("agents.tg_transfer.__main__.resolve_chat",
                   new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock()
            result = await agent._handle_dedup_response(task, "j1", job)

        assert result.status == TaskStatus.DONE
        # Only msg 102 (the unmentioned one) should be uploaded.
        assert agent.engine.transfer_single.await_count == 1
        call = agent.engine.transfer_single.await_args_list[0]
        assert call.kwargs.get("skip_pre_dedup") is True
        # Both queue rows dropped regardless (1 resolved, 2 uploaded).
        assert agent.media_db.delete_pending_dedup.await_count == 2

    @pytest.mark.asyncio
    async def test_unparseable_reply_reprompts(self):
        """Garbled replies (typos) must NOT default to 'all different, upload
        everything' — that could spend a lot of bandwidth on a mistake."""
        agent = self._build_agent()
        job = {"job_id": "j1", "source_chat": "src", "target_chat": "tgt",
               "task_id": "t1", "chat_id": 100, "status": "awaiting_dedup"}

        task = TaskRequest(task_id="t1", content="uhhh what")
        result = await agent._handle_dedup_response(task, "j1", job)

        assert result.status == TaskStatus.NEED_INPUT
        agent.engine.transfer_single.assert_not_called()
        # Don't drop the queue on unparseable input — user can retry.
        agent.media_db.delete_pending_dedup.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_queue_closes_cleanly(self):
        """Stale reply after the queue was drained (e.g. by another reply)
        should just confirm 'nothing to do' rather than crash."""
        agent = self._build_agent()
        job = {"job_id": "j1", "source_chat": "src", "target_chat": "tgt",
               "task_id": "t1", "chat_id": 100, "status": "awaiting_dedup"}
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[])
        agent._pending_jobs["t1"] = "j1"

        task = TaskRequest(task_id="t1", content="same 1a")
        result = await agent._handle_dedup_response(task, "j1", job)

        assert result.status == TaskStatus.DONE
        assert "t1" not in agent._pending_jobs


class TestProcessDeferred:
    """Phase 6: /process_deferred drains deferred_dedup, routing each row to
    upload (no candidate), skip (strict metadata match), or pending_dedup
    (thumb hit but metadata mismatch → Phase 5 user arbitration)."""

    def _build_agent(self):
        from agents.tg_transfer.__main__ import TGTransferAgent
        from agents.tg_transfer.batch_controller import BatchController
        agent = TGTransferAgent.__new__(TGTransferAgent)
        agent.db = AsyncMock()
        agent.db.create_job = AsyncMock(return_value="job-deferred")
        agent.db.add_messages = AsyncMock()
        agent.db.update_job_status = AsyncMock()
        agent.db.mark_message = AsyncMock()
        agent.media_db = AsyncMock()
        agent.tg_client = AsyncMock()
        agent.engine = AsyncMock()
        agent.engine._cancelled = set()
        agent._pending_jobs = {}
        agent._current_chat_id = {}
        agent._batch_message_cache = {}
        agent.ws_send_progress = AsyncMock()
        agent.ws_send_result = AsyncMock()
        agent.batch_controller = BatchController(agent)
        return agent

    @pytest.mark.asyncio
    async def test_empty_queue_returns_done(self):
        agent = self._build_agent()
        agent.media_db.list_deferred_dedup = AsyncMock(return_value=[])

        task = TaskRequest(task_id="td", content="/process_deferred")
        result = await agent._handle_process_deferred(task)

        assert result.status == TaskStatus.DONE
        agent.db.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_candidate_uploads_with_skip_pre_dedup(self):
        """No thumb match → fetch source msg + transfer_single. Must pass
        skip_pre_dedup=True since we already did the lookup ourselves; without
        it we'd re-run thumb dedup, hit no candidate, and waste cycles."""
        agent = self._build_agent()
        agent.media_db.list_deferred_dedup = AsyncMock(return_value=[
            {"id": 1, "source_chat": "@src", "source_msg_id": 100,
             "target_chat": "@tgt", "thumb_phash": "ph1",
             "file_type": "photo", "file_size": 1234, "caption": "hi",
             "duration": None, "grouped_id": None},
        ])
        agent.media_db.find_by_thumb_phash = AsyncMock(return_value=[])
        agent.media_db.delete_deferred_dedup = AsyncMock()
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[])
        agent.engine.transfer_single = AsyncMock(return_value={
            "ok": True, "dedup": False, "similar": None,
        })
        agent.tg_client.get_messages = AsyncMock(return_value=MagicMock())

        with patch("agents.tg_transfer.__main__.resolve_chat",
                   new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock()
            task = TaskRequest(task_id="td", content="/process_deferred",
                               chat_id=99)
            await agent._handle_process_deferred(task)
            # Wait for the bg task to finish
            await agent.batch_controller._bg_tasks["td"]

        agent.engine.transfer_single.assert_awaited_once()
        kwargs = agent.engine.transfer_single.await_args.kwargs
        assert kwargs.get("skip_pre_dedup") is True
        agent.media_db.delete_deferred_dedup.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_strict_metadata_match_skips_and_upgrades(self):
        """All metadata agree with a thumb candidate → upgrade trust to full
        and mark skipped, no upload."""
        agent = self._build_agent()
        agent.media_db.list_deferred_dedup = AsyncMock(return_value=[
            {"id": 7, "source_chat": "@src", "source_msg_id": 200,
             "target_chat": "@tgt", "thumb_phash": "ph2",
             "file_type": "photo", "file_size": 999, "caption": "match",
             "duration": None, "grouped_id": None},
        ])
        agent.media_db.find_by_thumb_phash = AsyncMock(return_value=[
            {"media_id": 50, "target_msg_id": 5000,
             "caption": "match", "file_size": 999, "duration": None},
        ])
        agent.media_db.upgrade_thumb_to_full = AsyncMock()
        agent.media_db.delete_deferred_dedup = AsyncMock()
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[])

        with patch("agents.tg_transfer.__main__.resolve_chat",
                   new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock()
            task = TaskRequest(task_id="td", content="/process_deferred",
                               chat_id=99)
            await agent._handle_process_deferred(task)
            await agent.batch_controller._bg_tasks["td"]

        agent.engine.transfer_single.assert_not_called()
        agent.media_db.upgrade_thumb_to_full.assert_awaited_once_with(
            50, verified_by="metadata",
        )

    @pytest.mark.asyncio
    async def test_metadata_mismatch_pushes_to_pending_dedup(self):
        """Thumb collides but caption disagrees → can't auto-decide. Park in
        pending_dedup so Phase 5 surfaces an ambiguous queue."""
        agent = self._build_agent()
        agent.media_db.list_deferred_dedup = AsyncMock(return_value=[
            {"id": 9, "source_chat": "@src", "source_msg_id": 300,
             "target_chat": "@tgt", "thumb_phash": "ph3",
             "file_type": "video", "file_size": 5000, "caption": "v1",
             "duration": 30, "grouped_id": None},
        ])
        agent.media_db.find_by_thumb_phash = AsyncMock(return_value=[
            {"media_id": 70, "target_msg_id": 7000,
             "caption": "v2", "file_size": 5000, "duration": 30},
        ])
        agent.media_db.insert_pending_dedup = AsyncMock(return_value=1)
        agent.media_db.delete_deferred_dedup = AsyncMock()
        agent.media_db.list_pending_dedup_by_job = AsyncMock(return_value=[
            {"id": 1, "source_msg_id": 300,
             "candidate_target_msg_ids": [7000]},
        ])

        with patch("agents.tg_transfer.__main__.resolve_chat",
                   new_callable=AsyncMock) as mock_resolve, \
             patch("agents.tg_transfer.__main__.format_ambiguous_summary",
                   return_value="(summary)"):
            mock_resolve.return_value = MagicMock()
            task = TaskRequest(task_id="td", content="/process_deferred",
                               chat_id=99)
            await agent._handle_process_deferred(task)
            await agent.batch_controller._bg_tasks["td"]

        agent.media_db.insert_pending_dedup.assert_awaited_once()
        # Job marked awaiting_dedup, not completed.
        statuses = [c.args[1] for c in agent.db.update_job_status.await_args_list]
        assert "awaiting_dedup" in statuses
        # Final WS push asks for input.
        last_result = agent.ws_send_result.await_args_list[-1].args[1]
        assert last_result.status == TaskStatus.NEED_INPUT
