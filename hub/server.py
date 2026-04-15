# hub/server.py
import os
from aiohttp import web
from core.models import AgentInfo, TaskRequest
from hub.registry import AgentRegistry
from hub.task_manager import TaskManager
from hub.router import Router
from hub.cli import send_task_to_agent
from hub.gemini_fallback import gemini_default_reply, gemini_is_continuation

DB_PATH = os.environ.get("TASKS_DB_PATH", "/data/tasks.db")


def create_hub_app(
    heartbeat_timeout: int = 30,
    llm_fallback=None,
    use_gemini_fallback: bool = True,
    db_path: str = DB_PATH,
) -> web.Application:
    app = web.Application()
    registry = AgentRegistry(heartbeat_timeout=heartbeat_timeout)
    task_manager = TaskManager(db_path=db_path)

    if llm_fallback is None and use_gemini_fallback:
        from hub.gemini_fallback import gemini_route
        llm_fallback = gemini_route

    router = Router(registry=registry, llm_fallback=llm_fallback)

    app["registry"] = registry
    app["task_manager"] = task_manager
    app["router"] = router

    app.router.add_post("/register", handle_register)
    app.router.add_post("/heartbeat", handle_heartbeat)
    app.router.add_get("/agents", handle_list_agents)
    app.router.add_post("/dispatch", handle_dispatch)
    app.router.add_post("/set_message_id", handle_set_message_id)

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

    router = request.app["router"]
    task_manager = request.app["task_manager"]

    # Clean up expired tasks
    task_manager.close_expired_tasks()

    # Handle /clear command
    if message.strip() == "/clear":
        active = task_manager.get_active_task_for_chat(chat_id)
        if active:
            task_manager.close_task(active["task_id"])
            return web.json_response({"status": "done", "message": "對話已結束"})
        return web.json_response({"status": "done", "message": "沒有進行中的對話"})

    # Priority 1: Reply to a specific bot message → exact task match
    if reply_to_message_id:
        task = task_manager.get_task_by_message_id(chat_id, reply_to_message_id)
        if task:
            # Reopen if closed/done
            if task["status"] in ("closed", "done"):
                task_manager.update_status(task["task_id"], "working")
            return await _continue_task(request, task, message)

    # Priority 2: Active task waiting for input → direct continuation
    active_task = task_manager.get_active_task_for_chat(chat_id)
    if active_task and active_task["status"] in ("waiting_input", "waiting_approval"):
        return await _continue_task(request, active_task, message)

    # Priority 3: Active task exists (working) → flash check if continuation
    if active_task:
        history = active_task["conversation_history"]
        last_topic = history[-1]["content"] if history else ""

        is_continuation = await gemini_is_continuation(message, last_topic)
        if is_continuation:
            return await _continue_task(request, active_task, message)
        else:
            task_manager.close_task(active_task["task_id"])

    # Priority 3: Route to agent
    agent = await router.route(message)
    if agent is None:
        # No agent — Hub replies via Gemini, store as hub task
        reply = await gemini_default_reply(message)
        if reply:
            task = task_manager.create_task(
                agent_name="_hub", chat_id=chat_id, content=message,
            )
            task_manager.append_assistant_response(task["task_id"], reply)
            return web.json_response({
                "status": "done",
                "message": reply,
                "task_id": task["task_id"],
            })
        return web.json_response({"status": "error", "message": "無法處理此訊息"})

    # Create new task and dispatch
    task = task_manager.create_task(
        agent_name=agent.name, chat_id=chat_id, content=message,
    )
    result = await _dispatch_to_agent(request, task, message)
    return web.json_response(result)


async def _continue_task(request: web.Request, task: dict, message: str) -> web.Response:
    """Continue an existing task with a new user message."""
    task_manager = request.app["task_manager"]
    task_manager.append_user_response(task["task_id"], message)

    # Refresh task after update
    task = task_manager.get_task(task["task_id"])

    if task["agent_name"] == "_hub":
        # Hub task — reply via Gemini with conversation context
        history = task["conversation_history"]
        context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
        reply = await gemini_default_reply(context)
        if reply:
            task_manager.append_assistant_response(task["task_id"], reply)
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

    # Update status
    _update_task_status(task_manager, task["task_id"], result)

    if result.get("message"):
        task_manager.append_assistant_response(task["task_id"], result["message"])

    result["task_id"] = task["task_id"]
    return web.json_response(result)


async def _dispatch_to_agent(request: web.Request, task: dict, message: str) -> dict:
    """Dispatch a new task to an agent."""
    task_manager = request.app["task_manager"]
    agent_info = request.app["registry"].get(task["agent_name"])

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
    return result


def _update_task_status(task_manager: TaskManager, task_id: str, result: dict):
    status = result.get("status")
    if status == "done":
        task_manager.complete_task(task_id)
    elif status == "need_input":
        task_manager.update_status(task_id, "waiting_input")
    elif status == "need_approval":
        task_manager.update_status(task_id, "waiting_approval")
