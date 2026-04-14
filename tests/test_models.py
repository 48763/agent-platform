# tests/test_models.py
from core.models import AgentResult, TaskRequest, AgentInfo, TaskStatus


def test_agent_result_done():
    result = AgentResult(status=TaskStatus.DONE, message="完成")
    d = result.to_dict()
    assert d == {"status": "done", "message": "完成"}
    assert AgentResult.from_dict(d) == result


def test_agent_result_need_input_with_options():
    result = AgentResult(
        status=TaskStatus.NEED_INPUT,
        message="選一個",
        options=["A", "B"],
    )
    d = result.to_dict()
    assert d["options"] == ["A", "B"]
    assert AgentResult.from_dict(d) == result


def test_agent_result_need_approval():
    result = AgentResult(
        status=TaskStatus.NEED_APPROVAL,
        message="是否允許？",
        action="run_command: rm -rf dist/",
        options=["允許", "拒絕"],
    )
    d = result.to_dict()
    assert d["action"] == "run_command: rm -rf dist/"
    assert AgentResult.from_dict(d) == result


def test_task_request_roundtrip():
    req = TaskRequest(
        task_id="abc-123",
        content="台北天氣",
        conversation_history=[{"role": "user", "content": "hi"}],
    )
    d = req.to_dict()
    assert TaskRequest.from_dict(d) == req


def test_agent_info_roundtrip():
    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣|weather"],
        capabilities=["get_weather"],
    )
    d = info.to_dict()
    assert AgentInfo.from_dict(d) == info
