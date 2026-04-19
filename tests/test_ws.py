import json
from core.ws import ws_msg, MsgType


def test_ws_msg_dispatch():
    msg = ws_msg(MsgType.DISPATCH, chat_id=123, message="hello")
    parsed = json.loads(msg)
    assert parsed["type"] == "dispatch"
    assert parsed["chat_id"] == 123
    assert parsed["message"] == "hello"


def test_ws_msg_result():
    msg = ws_msg(MsgType.RESULT, task_id="abc", status="done", message="ok")
    parsed = json.loads(msg)
    assert parsed["type"] == "result"
    assert parsed["task_id"] == "abc"
    assert parsed["status"] == "done"


def test_ws_msg_progress():
    msg = ws_msg(MsgType.PROGRESS, task_id="abc", chat_id=123, message="50%")
    parsed = json.loads(msg)
    assert parsed["type"] == "progress"
    assert parsed["task_id"] == "abc"
    assert parsed["chat_id"] == 123


def test_ws_msg_cancel():
    msg = ws_msg(MsgType.CANCEL, task_id="abc")
    parsed = json.loads(msg)
    assert parsed["type"] == "cancel"
    assert parsed["task_id"] == "abc"
