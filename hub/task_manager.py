# hub/task_manager.py
import json
import os
import sqlite3
import time
import uuid

TASK_EXPIRY_DAYS = int(os.environ.get("TASK_EXPIRY_DAYS", "7"))
DB_PATH = os.environ.get("TASKS_DB_PATH", "/data/tasks.db")


class TaskManager:
    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'working',
                conversation_history TEXT NOT NULL DEFAULT '[]',
                last_message_id INTEGER,
                source TEXT NOT NULL DEFAULT 'telegram',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_chat_id ON tasks(chat_id)
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS task_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_messages_lookup
            ON task_messages(chat_id, message_id)
        """)
        self._conn.commit()

    def create_task(self, agent_name: str, chat_id: int, content: str, source: str = "telegram") -> dict:
        task_id = str(uuid.uuid4())
        now = time.time()
        history = [{"role": "user", "content": content}]
        self._conn.execute(
            "INSERT INTO tasks (task_id, agent_name, chat_id, status, conversation_history, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, agent_name, chat_id, "working", json.dumps(history), source, now, now),
        )
        self._conn.commit()
        return self._get_task_dict(task_id)

    def get_task(self, task_id: str) -> dict | None:
        return self._get_task_dict(task_id)

    def get_task_by_message_id(self, chat_id: int, message_id: int) -> dict | None:
        """Find task by any bot reply message_id (for TG reply-based continuation)."""
        row = self._conn.execute(
            "SELECT t.* FROM tasks t JOIN task_messages tm ON t.task_id = tm.task_id "
            "WHERE tm.chat_id = ? AND tm.message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        if row:
            return self._row_to_dict(row)
        return None

    def get_active_task_for_chat(self, chat_id: int) -> dict | None:
        """Get the most recently updated non-closed task for a chat."""
        expiry = time.time() - (TASK_EXPIRY_DAYS * 86400)
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status NOT IN ('closed', 'done') AND updated_at > ? ORDER BY updated_at DESC LIMIT 1",
            (chat_id, expiry),
        ).fetchone()
        if row:
            return self._row_to_dict(row)
        return None

    def append_user_response(self, task_id: str, content: str):
        task = self._get_task_dict(task_id)
        if not task:
            return
        history = task["conversation_history"]
        history.append({"role": "user", "content": content})
        self._conn.execute(
            "UPDATE tasks SET conversation_history = ?, status = 'working', updated_at = ? WHERE task_id = ?",
            (json.dumps(history), time.time(), task_id),
        )
        self._conn.commit()

    def append_assistant_response(self, task_id: str, content: str):
        task = self._get_task_dict(task_id)
        if not task:
            return
        history = task["conversation_history"]
        history.append({"role": "assistant", "content": content})
        self._conn.execute(
            "UPDATE tasks SET conversation_history = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(history), time.time(), task_id),
        )
        self._conn.commit()

    def set_message_id(self, task_id: str, message_id: int):
        """Store a bot reply message_id for reply-based lookup."""
        task = self._get_task_dict(task_id)
        if not task:
            return
        self._conn.execute(
            "INSERT INTO task_messages (task_id, chat_id, message_id, created_at) VALUES (?, ?, ?, ?)",
            (task_id, task["chat_id"], message_id, time.time()),
        )
        self._conn.commit()

    def update_status(self, task_id: str, status: str):
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, time.time(), task_id),
        )
        self._conn.commit()

    def complete_task(self, task_id: str):
        self.update_status(task_id, "done")

    def close_task(self, task_id: str):
        self.update_status(task_id, "closed")

    def close_expired_tasks(self):
        expiry = time.time() - (TASK_EXPIRY_DAYS * 86400)
        self._conn.execute(
            "UPDATE tasks SET status = 'closed' WHERE status NOT IN ('closed', 'done') AND updated_at < ?",
            (expiry,),
        )
        self._conn.commit()

    def _get_task_dict(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row:
            return self._row_to_dict(row)
        return None

    def _row_to_dict(self, row) -> dict:
        return {
            "task_id": row["task_id"],
            "agent_name": row["agent_name"],
            "chat_id": row["chat_id"],
            "status": row["status"],
            "conversation_history": json.loads(row["conversation_history"]),
            "last_message_id": row["last_message_id"],
            "source": row["source"] if "source" in row.keys() else "telegram",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
