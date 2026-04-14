import uuid
from dataclasses import dataclass, field


@dataclass
class ManagedTask:
    task_id: str
    agent_name: str
    chat_id: int
    status: str  # working, waiting_input, waiting_approval, done
    conversation_history: list[dict] = field(default_factory=list)


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, ManagedTask] = {}

    def create_task(self, agent_name: str, chat_id: int, content: str) -> ManagedTask:
        task_id = str(uuid.uuid4())
        task = ManagedTask(
            task_id=task_id,
            agent_name=agent_name,
            chat_id=chat_id,
            status="working",
            conversation_history=[{"role": "user", "content": content}],
        )
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> ManagedTask | None:
        return self._tasks.get(task_id)

    def get_active_task_for_chat(self, chat_id: int) -> ManagedTask | None:
        for task in self._tasks.values():
            if task.chat_id == chat_id and task.status not in ("done",):
                return task
        return None

    def append_user_response(self, task_id: str, content: str) -> None:
        task = self._tasks[task_id]
        task.conversation_history.append({"role": "user", "content": content})
        task.status = "working"

    def complete_task(self, task_id: str) -> None:
        self._tasks[task_id].status = "done"
