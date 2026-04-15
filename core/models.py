from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid


class TaskStatus(str, Enum):
    DONE = "done"
    NEED_INPUT = "need_input"
    NEED_APPROVAL = "need_approval"
    ERROR = "error"


@dataclass
class AgentResult:
    status: TaskStatus
    message: str
    options: Optional[list[str]] = None
    action: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"status": self.status.value, "message": self.message}
        if self.options is not None:
            d["options"] = self.options
        if self.action is not None:
            d["action"] = self.action
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AgentResult":
        return cls(
            status=TaskStatus(data["status"]),
            message=data["message"],
            options=data.get("options"),
            action=data.get("action"),
        )


@dataclass
class TaskRequest:
    task_id: str
    content: str
    conversation_history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "conversation_history": self.conversation_history,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRequest":
        return cls(
            task_id=data["task_id"],
            content=data["content"],
            conversation_history=data.get("conversation_history", []),
        )


@dataclass
class AgentInfo:
    name: str
    description: str
    url: str
    route_patterns: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    priority: int = 0  # higher = matched first

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "route_patterns": self.route_patterns,
            "capabilities": self.capabilities,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentInfo":
        return cls(
            name=data["name"],
            description=data["description"],
            url=data["url"],
            route_patterns=data.get("route_patterns", []),
            capabilities=data.get("capabilities", []),
            priority=data.get("priority", 0),
        )
