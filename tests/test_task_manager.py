from hub.task_manager import TaskManager


def make_tm(tmp_path):
    return TaskManager(db_path=str(tmp_path / "test_tasks.db"))


def test_create_task(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="台北天氣")
    assert task["agent_name"] == "weather"
    assert task["chat_id"] == 123
    assert task["status"] == "working"
    assert len(task["conversation_history"]) == 1
    assert task["conversation_history"][0]["content"] == "台北天氣"


def test_get_task(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    found = tm.get_task(task["task_id"])
    assert found is not None
    assert found["task_id"] == task["task_id"]


def test_get_active_task_for_chat(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    active = tm.get_active_task_for_chat(123)
    assert active is not None
    assert active["task_id"] == task["task_id"]


def test_append_user_response(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.update_status(task["task_id"], "waiting_input")
    tm.append_user_response(task["task_id"], "台北")
    updated = tm.get_task(task["task_id"])
    assert len(updated["conversation_history"]) == 2
    assert updated["conversation_history"][1]["content"] == "台北"
    assert updated["status"] == "working"


def test_complete_task(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.complete_task(task["task_id"])
    updated = tm.get_task(task["task_id"])
    assert updated["status"] == "done"
    # done tasks are still selectable (can be continued via reply)
    active = tm.get_active_task_for_chat(123)
    assert active is not None
    assert active["status"] == "done"


def test_archived_task_not_active(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.archive_task(task["task_id"])
    assert tm.get_active_task_for_chat(123) is None


def test_closed_task_not_active(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.close_task(task["task_id"])
    assert tm.get_active_task_for_chat(123) is None


def test_reopen_task(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.close_task(task["task_id"])
    tm.reopen_task(task["task_id"])
    updated = tm.get_task(task["task_id"])
    assert updated["status"] == "done"


def test_close_task(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.close_task(task["task_id"])
    active = tm.get_active_task_for_chat(123)
    assert active is None


def test_message_id_lookup(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="code", chat_id=123, content="看檔案")
    tm.set_message_id(task["task_id"], 99999)
    found = tm.get_task_by_message_id(123, 99999)
    assert found is not None
    assert found["task_id"] == task["task_id"]


def test_append_assistant_response(tmp_path):
    tm = make_tm(tmp_path)
    task = tm.create_task(agent_name="hub", chat_id=123, content="你好")
    tm.append_assistant_response(task["task_id"], "嗨！")
    updated = tm.get_task(task["task_id"])
    assert len(updated["conversation_history"]) == 2
    assert updated["conversation_history"][1] == {"role": "assistant", "content": "嗨！"}
