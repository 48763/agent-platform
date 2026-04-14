from hub.task_manager import TaskManager, ManagedTask


def test_create_task():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="台北天氣")
    assert task.agent_name == "weather"
    assert task.chat_id == 123
    assert task.status == "working"
    assert len(task.conversation_history) == 1
    assert task.conversation_history[0]["content"] == "台北天氣"


def test_get_task():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    found = tm.get_task(task.task_id)
    assert found is not None
    assert found.task_id == task.task_id


def test_get_active_task_for_chat():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    active = tm.get_active_task_for_chat(123)
    assert active is not None
    assert active.task_id == task.task_id


def test_append_user_response():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    task.status = "waiting_input"
    tm.append_user_response(task.task_id, "台北")
    assert len(task.conversation_history) == 2
    assert task.conversation_history[1]["content"] == "台北"
    assert task.status == "working"


def test_complete_task():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.complete_task(task.task_id)
    assert task.status == "done"
    assert tm.get_active_task_for_chat(123) is None
