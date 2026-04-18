# core/base_agent.py
import asyncio
import os
import sys
from abc import ABC, abstractmethod
from aiohttp import web, ClientSession
from core.config import load_agent_config
from core.models import AgentInfo, AgentResult, TaskRequest, TaskStatus
from core.sandbox import Sandbox
from core.llm import create_llm_client, LLMInitError, LLMClient


class BaseAgent(ABC):
    def __init__(self, agent_dir: str, hub_url: str, port: int = 0):
        self.config = load_agent_config(agent_dir)
        self.name = self.config["name"]
        self.hub_url = hub_url
        self.port = port
        self.host = os.environ.get("AGENT_HOST", "localhost")
        sandbox_config = self.config.get("sandbox", {"allowed_dirs": []})
        self.sandbox = Sandbox(sandbox_config)
        self.llm: LLMClient | None = None

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
            priority=self.config.get("priority", 0),
        )
        async with ClientSession() as session:
            await session.post(f"{self.hub_url}/register", json=info.to_dict())

    async def _heartbeat_loop(self, actual_port: int, interval: int = 10) -> None:
        async with ClientSession() as session:
            while True:
                try:
                    async with session.post(
                        f"{self.hub_url}/heartbeat",
                        json={"name": self.name},
                    ) as resp:
                        if resp.status == 404:
                            # Hub doesn't know us — re-register
                            await self.register(actual_port)
                except Exception:
                    pass
                await asyncio.sleep(interval)

    async def _register_error(self, error: str) -> None:
        """Report startup error to Hub."""
        try:
            async with ClientSession() as session:
                await session.post(
                    f"{self.hub_url}/register_error",
                    json={"name": self.name, "error": error},
                )
        except Exception:
            pass  # Hub might not be running

    async def run(self) -> None:
        # Validate LLM if configured
        settings = self.config.get("settings", {})
        if settings.get("llm"):
            try:
                self.llm = await create_llm_client(settings)
            except LLMInitError as e:
                await self._register_error(str(e))
                print(f"ERROR: LLM init failed: {e}", file=sys.stderr)
                sys.exit(1)

        app = self.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {actual_port}")

        await self.register(actual_port)
        await self._heartbeat_loop(actual_port)
