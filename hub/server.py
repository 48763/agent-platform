# hub/server.py
import os
from aiohttp import web
from core.models import AgentInfo, TaskRequest
from hub.registry import AgentRegistry
from hub.task_manager import TaskManager
from hub.router import Router
from hub.gemini_fallback import gemini_unified_route, GeminiChat
from hub.dashboard import (
    handle_dashboard, handle_dashboard_tasks,
    handle_task_close, handle_task_reopen, handle_task_delete,
    handle_dashboard_agents, handle_agent_disable, handle_agent_enable,
)


async def handle_dashboard_gateways(request: web.Request) -> web.Response:
    gateways = request.app.get("gateway_connections", [])
    result = []
    for gw in gateways:
        if gw.get("ws") and not gw["ws"].closed:
            result.append({
                "platform": gw.get("platform"),
                "mode": gw.get("mode"),
                "phone": gw.get("phone"),
                "allowed_chats": gw.get("allowed_chats"),
            })
    return web.json_response({"gateways": result})
from hub.auth import (
    check_session, is_auth_enabled,
    handle_login_page, handle_login, handle_logout,
)
from hub.ws_handler import handle_agent_ws, handle_gateway_ws

DB_PATH = os.environ.get("TASKS_DB_PATH", "/data/tasks.db")


def create_hub_app(
    heartbeat_timeout: int = 30,
    use_gemini_fallback: bool = True,
    db_path: str = DB_PATH,
) -> web.Application:
    @web.middleware
    async def auth_middleware(request, handler):
        # Skip auth for API routes and auth routes
        path = request.path
        no_auth_prefixes = ("/register", "/agents", "/dispatch", "/set_message_id", "/auth/", "/ws/")
        if any(path.startswith(p) for p in no_auth_prefixes):
            return await handler(request)

        # Dashboard routes require auth
        if is_auth_enabled() and not check_session(request):
            raise web.HTTPFound("/auth/login")

        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])
    registry = AgentRegistry(heartbeat_timeout=heartbeat_timeout)
    task_manager = TaskManager(db_path=db_path)
    router = Router(registry=registry)
    chat = GeminiChat()

    app["registry"] = registry
    app["task_manager"] = task_manager
    app["router"] = router
    app["chat"] = chat
    app["use_gemini_fallback"] = use_gemini_fallback
    app["gateway_connections"] = []

    # Auth routes (no auth required)
    app.router.add_get("/auth/login", handle_login_page)
    app.router.add_post("/auth/login", handle_login)
    app.router.add_get("/auth/logout", handle_logout)

    # API routes (no auth — used by agents and gateway)
    app.router.add_post("/register", handle_register)
    app.router.add_post("/register_error", handle_register_error)
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
    app.router.add_get("/dashboard/agent/{name}/proxy", handle_agent_dashboard_proxy)
    app.router.add_get("/dashboard/gateways", handle_dashboard_gateways)

    # WebSocket routes (no auth — used by agents and gateway)
    app.router.add_get("/ws/agent/{name}", handle_agent_ws)
    app.router.add_get("/ws/gateway", handle_gateway_ws)

    return app


async def handle_register(request: web.Request) -> web.Response:
    data = await request.json()
    info = AgentInfo.from_dict(data)
    auth_status = data.get("auth_status")
    auth_error = data.get("auth_error")
    has_dashboard = data.get("has_dashboard", False)
    request.app["registry"].register(info, auth_status=auth_status, auth_error=auth_error, has_dashboard=has_dashboard)
    return web.json_response({"status": "registered", "name": info.name})


async def handle_register_error(request: web.Request) -> web.Response:
    data = await request.json()
    name = data.get("name", "unknown")
    error = data.get("error", "unknown error")
    request.app["registry"].register_error(name, error)
    return web.json_response({"status": "recorded", "name": name})


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


async def handle_agent_dashboard_proxy(request: web.Request) -> web.Response:
    """Proxy an agent's /dashboard endpoint through Hub."""
    name = request.match_info["name"]
    registry = request.app["registry"]
    agents = {a["name"]: a for a in registry.list_all()}
    agent = agents.get(name)
    if not agent or not agent.get("url"):
        return web.Response(text="Agent not found", status=404)
    from aiohttp import ClientSession as _ClientSession
    try:
        async with _ClientSession() as session:
            async with session.get(f"{agent['url']}/dashboard") as resp:
                body = await resp.text()
                return web.Response(text=body, content_type=resp.content_type or "text/html", status=resp.status)
    except Exception as e:
        return web.Response(text=f"Cannot reach agent dashboard: {e}", status=502)


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

    # Agent task — send via WS
    registry = request.app["registry"]
    agent_ws = registry.get_ws(task["agent_name"])
    if agent_ws is None:
        return web.json_response({"status": "error", "message": "Agent 已離線"})

    from core.ws import ws_msg, MsgType
    await agent_ws.send_str(ws_msg(MsgType.TASK,
        task_id=task["task_id"],
        content=message,
        conversation_history=task["conversation_history"],
        chat_id=task["chat_id"],
    ))

    # Task dispatched via WS — result will come back asynchronously
    return web.json_response({
        "status": "working",
        "message": "處理中...",
        "task_id": task["task_id"],
    })


async def _dispatch_to_agent(request: web.Request, task: dict, message: str) -> dict:
    """Dispatch a new task to an agent via WS."""
    registry = request.app["registry"]
    agent_ws = registry.get_ws(task["agent_name"])

    if agent_ws is None:
        return {"status": "error", "message": "Agent 已離線", "task_id": task["task_id"]}

    from core.ws import ws_msg, MsgType
    await agent_ws.send_str(ws_msg(MsgType.TASK,
        task_id=task["task_id"],
        content=message,
        conversation_history=task["conversation_history"],
        chat_id=task["chat_id"],
    ))

    # Task dispatched via WS — result comes back asynchronously
    return {"status": "working", "message": "處理中...", "task_id": task["task_id"]}


