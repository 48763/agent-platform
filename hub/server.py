# hub/server.py
from aiohttp import web
from core.models import AgentInfo
from hub.registry import AgentRegistry
from hub.task_manager import TaskManager
from hub.router import Router


def create_hub_app(
    heartbeat_timeout: int = 30,
    llm_fallback=None,
) -> web.Application:
    app = web.Application()
    registry = AgentRegistry(heartbeat_timeout=heartbeat_timeout)
    task_manager = TaskManager()
    router = Router(registry=registry, llm_fallback=llm_fallback)

    app["registry"] = registry
    app["task_manager"] = task_manager
    app["router"] = router

    app.router.add_post("/register", handle_register)
    app.router.add_post("/heartbeat", handle_heartbeat)
    app.router.add_get("/agents", handle_list_agents)

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
