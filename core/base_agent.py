# core/base_agent.py
import asyncio
import os
from abc import ABC, abstractmethod
from aiohttp import web, ClientSession
from core.config import load_agent_config
from core.models import AgentInfo, AgentResult, TaskRequest, TaskStatus
from core.sandbox import Sandbox


class BaseAgent(ABC):
    def __init__(self, agent_dir: str, hub_url: str, port: int = 0):
        self.config = load_agent_config(agent_dir)
        self.name = self.config["name"]
        self.hub_url = hub_url
        self.port = port
        self.host = os.environ.get("AGENT_HOST", "localhost")
        sandbox_config = self.config.get("sandbox", {"allowed_dirs": []})
        self.sandbox = Sandbox(sandbox_config)

    @abstractmethod
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        pass

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/task", self._handle_task_http)
        app.router.add_get("/health", self._handle_health)
        return app

    async def _handle_task_http(self, request: web.Request) -> web.Response:
        data = await request.json()
        task = TaskRequest.from_dict(data)
        result = await self.handle_task(task)
        return web.json_response(result.to_dict())

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"name": self.name, "status": "ok"})

    async def register(self, actual_port: int) -> None:
        info = AgentInfo(
            name=self.name,
            description=self.config.get("description", ""),
            url=f"http://{self.host}:{actual_port}",
            route_patterns=self.config.get("route_patterns", []),
            capabilities=self.config.get("capabilities", []),
        )
        async with ClientSession() as session:
            await session.post(f"{self.hub_url}/register", json=info.to_dict())

    async def _heartbeat_loop(self, interval: int = 10) -> None:
        async with ClientSession() as session:
            while True:
                try:
                    await session.post(
                        f"{self.hub_url}/heartbeat",
                        json={"name": self.name},
                    )
                except Exception:
                    pass
                await asyncio.sleep(interval)

    async def run(self) -> None:
        app = self.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {actual_port}")

        await self.register(actual_port)
        await self._heartbeat_loop()
