# hub/server.py
import os
from aiohttp import web
from core.models import AgentInfo, TaskRequest
from hub.registry import AgentRegistry
from hub.task_manager import TaskManager
from hub.router import Router
from hub.cli import send_task_to_agent
from hub.gemini_fallback import gemini_unified_route, GeminiChat
from hub.dashboard import (
    handle_dashboard, handle_dashboard_tasks,
    handle_task_close, handle_task_reopen, handle_task_delete,
    handle_dashboard_agents, handle_agent_disable, handle_agent_enable,
)

DB_PATH = os.environ.get("TASKS_DB_PATH", "/data/tasks.db")


def create_hub_app(
    heartbeat_timeout: int = 30,
    use_gemini_fallback: bool = True,
    db_path: str = DB_PATH,
) -> web.Application:
    app = web.Application()
    registry = AgentRegistry(heartbeat_timeout=heartbeat_timeout)
    task_manager = TaskManager(db_path=db_path)
    router = Router(registry=registry)
    chat = GeminiChat()

    app["registry"] = registry
    app["task_manager"] = task_manager
    app["router"] = router
    app["chat"] = chat
    app["use_gemini_fallback"] = use_gemini_fallback

    app.router.add_post("/register", handle_register)
    app.router.add_post("/heartbeat", handle_heartbeat)
    app.router.add_get("/agents", handle_list_agents)
    app.router.add_post("/dispatch", handle_dispatch)
    app.router.add_post("/set_message_id", handle_set_message_id)
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard/tasks", handle_dashboard_tasks)
    app.router.add_post("/dashboard/task/{task_id}/close", handle_task_close)
    app.router.add_post("/dashboard/task/{task_id}/reopen", handle_task_reopen)
    app.router.add_post("/dashboard/task/{task_id}/delete", handle_task_delete)
    app.router.add_get("/dashboard/agents", handle_dashboard_agents)
    app.router.add_post("/dashboard/agent/{name}/disable", handle_agent_disable)
    app.router.add_post("/dashboard/agent/{name}/enable", handle_agent_enable)

    return app


async def handle_register(request: web.Request) -> web.Response:
    data = await request.json()
    info = AgentInfo.from_dict(data)
    request.app["registry"].register(info)
    return web.json_response({"status": "registered", "name": info.name})


async def handle_heartbeat(request: web.Request) -> web.Response:
    data = await request.json()
    name = data["name"]
    success = request.app["registry"].heartbeat(name)
    if not success:
        return web.json_response({"error": "agent not found"}, status=404)
    return web.json_response({"status": "ok"})


async def handle_list_agents(request: web.Request) -> web.Response:
    agents = request.app["registry"].list_online()
    return web.json_response({"agents": [a.to_dict() for a in agents]})


async def handle_set_message_id(request: web.Request) -> web.Response:
    data = await request.json()
    task_id = data.get("task_id")
    message_id = data.get("message_id")
    if task_id and message_id:
        request.app["task_manager"].set_message_id(task_id, message_id)
    return web.json_response({"status": "ok"})


async def handle_dispatch(request: web.Request) -> web.Response:
    data = await request.json()
    message = data["message"]
    chat_id = data.get("chat_id", 0)
    reply_to_message_id = data.get("reply_to_message_id")
    source = data.get("source", "telegram")

    task_manager = request.app["task_manager"]
    registry = request.app["registry"]
    chat: GeminiChat = request.app["chat"]

    # Run lifecycle transitions
    task_manager.run_lifecycle()

    # Handle /clear command
    if message.strip() == "/clear":
        active = task_manager.get_active_task_for_chat(chat_id)
        if active:
            task_manager.complete_task(active["task_id"])
            return web.json_response({"status": "done", "message": "對話已結束"})
        return web.json_response({"status": "done", "message": "沒有進行中的對話"})

    # Priority 1: Reply to a specific bot message → exact task match
    if reply_to_message_id:
        task = task_manager.get_task_by_message_id(chat_id, reply_to_message_id)
        if task:
            if task["status"] == "closed":
                pass  # closed tasks cannot be reopened via reply
            elif task["status"] in ("done", "archived"):
                task_manager.update_status(task["task_id"], "working")
                return await _continue_task(request, task, message)
            else:
                return await _continue_task(request, task, message)

    # Priority 2: Active task waiting for input → direct continuation
    active_task = task_manager.get_active_task_for_chat(chat_id)
    if active_task and active_task["status"] in ("waiting_input", "waiting_approval"):
        return await _continue_task(request, active_task, message)

    # Priority 3: Keyword match (fast, no AI)
    router: Router = request.app["router"]
    keyword_match = router.match_by_keyword(message)
    if keyword_match:
        task = task_manager.create_task(
            agent_name=keyword_match.name, chat_id=chat_id, content=message, source=source,
        )
        result = await _dispatch_to_agent(request, task, message)
        return web.json_response(result)

    # Priority 4: Unified Gemini flash routing (one call decides everything)
    if request.app["use_gemini_fallback"]:
        # Gather all active tasks for this chat
        active_tasks = _get_all_active_tasks(task_manager, chat_id)
        online_agents = [a for a in registry.list_online() if a.priority >= 0]

        decision = await gemini_unified_route(message, active_tasks, online_agents)
        action = decision.get("action")

        if action == "continue":
            task = task_manager.get_task(decision["task_id"])
            if task:
                return await _continue_task(request, task, message)

        elif action == "route":
            agent_name = decision["agent_name"]
            agent_info = registry.get(agent_name)
            if agent_info:
                task = task_manager.create_task(
                    agent_name=agent_name, chat_id=chat_id, content=message, source=source,
                )
                result = await _dispatch_to_agent(request, task, message)
                return web.json_response(result)

        # action == "chat" or fallthrough
        return await _hub_chat_reply(request, chat_id, message, source)

    # No gemini fallback — error
    return web.json_response({"status": "error", "message": "無法處理此訊息"})


def _get_all_active_tasks(task_manager: TaskManager, chat_id: int) -> list[dict]:
    """Get all non-closed tasks for a chat (for unified router context)."""
    import time
    expiry_days = int(os.environ.get("TASK_EXPIRY_DAYS", "7"))
    expiry = time.time() - (expiry_days * 86400)
    rows = task_manager._conn.execute(
        "SELECT * FROM tasks WHERE chat_id = ? AND status NOT IN ('archived', 'closed') AND updated_at > ? ORDER BY updated_at DESC LIMIT 10",
        (chat_id, expiry),
    ).fetchall()
    return [task_manager._row_to_dict(r) for r in rows]


async def _hub_chat_reply(request: web.Request, chat_id: int, message: str, source: str = "telegram") -> web.Response:
    """Hub replies directly via Gemini Chat."""
    task_manager = request.app["task_manager"]
    chat: GeminiChat = request.app["chat"]

    # Check if there's an existing hub chat task
    active = task_manager.get_active_task_for_chat(chat_id)
    if active and active["agent_name"] == "_hub":
        # Continue hub chat with context
        task_manager.append_user_response(active["task_id"], message)
        task = task_manager.get_task(active["task_id"])
        reply = await chat.reply_with_context(task["conversation_history"])
    else:
        # New hub chat
        reply = await chat.reply(message)
        if reply:
            task = task_manager.create_task(
                agent_name="_hub", chat_id=chat_id, content=message, source=source,
            )
        else:
            return web.json_response({"status": "error", "message": "無法處理此訊息"})

    if reply:
        task_manager.append_assistant_response(task["task_id"], reply)
        task_manager.complete_task(task["task_id"])
        return web.json_response({
            "status": "done",
            "message": reply,
            "task_id": task["task_id"],
        })
    return web.json_response({"status": "error", "message": "無法處理此訊息"})


async def _continue_task(request: web.Request, task: dict, message: str) -> web.Response:
    """Continue an existing task with a new user message."""
    task_manager = request.app["task_manager"]
    chat: GeminiChat = request.app["chat"]
    task_manager.append_user_response(task["task_id"], message)

    # Refresh task after update
    task = task_manager.get_task(task["task_id"])

    if task["agent_name"] == "_hub":
        reply = await chat.reply_with_context(task["conversation_history"])
        if reply:
            task_manager.append_assistant_response(task["task_id"], reply)
            task_manager.complete_task(task["task_id"])
            return web.json_response({
                "status": "done",
                "message": reply,
                "task_id": task["task_id"],
            })
        return web.json_response({"status": "error", "message": "無法處理此訊息"})

    # Agent task
    agent_info = request.app["registry"].get(task["agent_name"])
    if agent_info is None:
        return web.json_response({"status": "error", "message": "Agent 已離線"})

    task_request = TaskRequest(
        task_id=task["task_id"],
        content=message,
        conversation_history=task["conversation_history"],
    )
    result = await send_task_to_agent(agent_info.url, task_request)

    _update_task_status(task_manager, task["task_id"], result)

    if result.get("message"):
        task_manager.append_assistant_response(task["task_id"], result["message"])

    result["task_id"] = task["task_id"]
    return web.json_response(result)


async def _dispatch_to_agent(request: web.Request, task: dict, message: str) -> dict:
    """Dispatch a new task to an agent."""
    import time as _time
    task_manager = request.app["task_manager"]
    registry = request.app["registry"]
    agent_info = registry.get(task["agent_name"])

    task_request = TaskRequest(
        task_id=task["task_id"],
        content=message,
        conversation_history=task["conversation_history"],
    )

    start = _time.time()
    result = await send_task_to_agent(agent_info.url, task_request)
    duration_ms = int((_time.time() - start) * 1000)

    success = result.get("status") != "error"
    registry.record_task_result(task["agent_name"], success, duration_ms)

    _update_task_status(task_manager, task["task_id"], result)

    if result.get("message"):
        task_manager.append_assistant_response(task["task_id"], result["message"])

    result["task_id"] = task["task_id"]
    return result


def _update_task_status(task_manager: TaskManager, task_id: str, result: dict):
    status = result.get("status")
    if status == "done":
        task_manager.complete_task(task_id)
    elif status == "need_input":
        task_manager.update_status(task_id, "waiting_input")
    elif status == "need_approval":
        task_manager.update_status(task_id, "waiting_approval")
