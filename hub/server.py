# hub/server.py
from aiohttp import web
from core.models import AgentInfo, TaskRequest
from hub.registry import AgentRegistry
from hub.task_manager import TaskManager
from hub.router import Router
from hub.cli import send_task_to_agent


def create_hub_app(
    heartbeat_timeout: int = 30,
    llm_fallback=None,
    use_gemini_fallback: bool = True,
) -> web.Application:
    app = web.Application()
    registry = AgentRegistry(heartbeat_timeout=heartbeat_timeout)
    task_manager = TaskManager()

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


async def handle_dispatch(request: web.Request) -> web.Response:
    data = await request.json()
    message = data["message"]
    chat_id = data.get("chat_id", 0)

    router = request.app["router"]
    task_manager = request.app["task_manager"]

    # Check for active task (multi-turn continuation)
    active_task = task_manager.get_active_task_for_chat(chat_id)
    if active_task and active_task.status in ("waiting_input", "waiting_approval"):
        task_manager.append_user_response(active_task.task_id, message)
        task_request = TaskRequest(
            task_id=active_task.task_id,
            content=message,
            conversation_history=active_task.conversation_history,
        )
        agent_info = request.app["registry"].get(active_task.agent_name)
        if agent_info is None:
            return web.json_response({"status": "error", "message": "Agent 已離線"})
        result = await send_task_to_agent(agent_info.url, task_request)
    else:
        # New task — route it
        agent = await router.route(message)
        if agent is None:
            return web.json_response({
                "status": "error",
                "message": "無法處理此訊息，沒有可用的 agent",
            })

        task = task_manager.create_task(
            agent_name=agent.name, chat_id=chat_id, content=message,
        )
        task_request = TaskRequest(
            task_id=task.task_id,
            content=message,
            conversation_history=task.conversation_history,
        )
        result = await send_task_to_agent(agent.url, task_request)
        active_task = task

    # Update task status based on result
    status = result.get("status")
    if status == "done":
        task_manager.complete_task(active_task.task_id)
    elif status == "need_input":
        active_task.status = "waiting_input"
    elif status == "need_approval":
        active_task.status = "waiting_approval"

    return web.json_response(result)
