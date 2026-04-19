"""WebSocket message protocol constants and helpers."""
import json
from enum import Enum


class MsgType(str, Enum):
    # Gateway → Hub
    DISPATCH = "dispatch"

    # Hub → Gateway
    REPLY = "reply"

    # Hub → Agent
    TASK = "task"
    CANCEL = "cancel"

    # Agent → Hub
    RESULT = "result"

    # Bidirectional (Agent → Hub → Gateway)
    PROGRESS = "progress"

    # Gateway → Hub (on connect)
    GW_REGISTER = "gw_register"


def ws_msg(msg_type: MsgType, **kwargs) -> str:
    """Build a JSON WS message string."""
    payload = {"type": msg_type.value, **kwargs}
    return json.dumps(payload, ensure_ascii=False)


def ws_parse(raw: str) -> dict:
    """Parse a WS message string into a dict."""
    return json.loads(raw)
