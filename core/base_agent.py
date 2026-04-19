# core/base_agent.py
import asyncio
import os
import sys
import logging
from abc import ABC, abstractmethod
from aiohttp import web, ClientSession, WSMsgType
from core.config import load_agent_config
from core.models import AgentInfo, AgentResult, TaskRequest, TaskStatus
from core.sandbox import Sandbox
from core.llm import create_llm_client, check_llm_auth, LLMInitError, LLMClient
from core.ws import MsgType, ws_msg, ws_parse

logger = logging.getLogger(__name__)


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
        self._llm_authenticated: bool = True
        self._llm_error: str = ""
        self._init_error: str = ""
        self._ws = None
        self._cancelled_tasks: set[str] = set()

    @abstractmethod
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        pass

    async def _init_services(self) -> None:
        """Override to initialize agent-specific services (DB, clients, etc).
        Exceptions are caught by run() and reported to Hub as error state.
        """
        pass

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        return app

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"name": self.name, "status": "ok"})

    async def ws_send_result(self, task_id: str, result: AgentResult):
        """Send task result to Hub via WS."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(ws_msg(MsgType.RESULT,
                task_id=task_id,
                status=result.status.value,
                message=result.message,
                options=result.options,
            ))

    async def ws_send_progress(self, task_id: str, chat_id: int, message: str):
        """Send progress update to Hub via WS."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(ws_msg(MsgType.PROGRESS,
                task_id=task_id,
                chat_id=chat_id,
                message=message,
            ))

    def is_cancelled(self, task_id: str) -> bool:
        return task_id in self._cancelled_tasks

    def on_cancel(self, task_id: str):
        """Hook for subclasses to handle task cancellation."""
        pass

    async def register(self, actual_port: int) -> None:
        info = AgentInfo(
            name=self.name,
            description=self.config.get("description", ""),
            url=f"http://{self.host}:{actual_port}",
            route_patterns=self.config.get("route_patterns", []),
            capabilities=self.config.get("capabilities", []),
            priority=self.config.get("priority", 0),
        )
        data = info.to_dict()
        # Check if this agent has a /dashboard route
        if hasattr(self, '_app') and self._app:
            data["has_dashboard"] = any(
                r.resource.canonical == "/dashboard"
                for r in self._app.router.routes()
                if hasattr(r, 'resource') and r.resource
            )
        if self._init_error:
            data["auth_status"] = "error"
            data["auth_error"] = self._init_error
        elif not self._llm_authenticated:
            data["auth_status"] = "unauthenticated"
            data["auth_error"] = self._llm_error
        async with ClientSession() as session:
            await session.post(f"{self.hub_url}/register", json=data)

    async def _ws_loop(self) -> None:
        """Maintain WS connection to Hub. Auto-reconnect on disconnect."""
        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/agent/{self.name}"

        while True:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        logger.info(f"WS connected to Hub: {ws_url}")

                        # Re-register on every WS connect (Hub may have restarted)
                        try:
                            await self.register(self._actual_port)
                        except Exception as e:
                            logger.warning(f"Re-register failed: {e}")

                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = ws_parse(msg.data)
                                msg_type = data.get("type")

                                if msg_type == MsgType.TASK.value:
                                    asyncio.create_task(self._handle_ws_task(data))

                                elif msg_type == MsgType.CANCEL.value:
                                    task_id = data.get("task_id")
                                    if task_id:
                                        self._cancelled_tasks.add(task_id)
                                        self.on_cancel(task_id)
                                        logger.info(f"Task cancelled: {task_id}")

                            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break

            except Exception as e:
                logger.warning(f"WS connection lost: {e}")

            self._ws = None
            logger.info("Reconnecting to Hub in 3 seconds...")
            await asyncio.sleep(3)

    async def _handle_ws_task(self, data: dict):
        """Handle incoming task from Hub via WS."""
        task = TaskRequest(
            task_id=data["task_id"],
            content=data["content"],
            conversation_history=data.get("conversation_history", []),
            chat_id=data.get("chat_id", 0),
        )

        try:
            result = await self.handle_task(task)
        except Exception as e:
            logger.error(f"handle_task error: {e}", exc_info=True)
            result = AgentResult(status=TaskStatus.ERROR, message=f"執行失敗：{e}")

        self._cancelled_tasks.discard(task.task_id)

        # None means task is handled asynchronously (e.g. background batch)
        if result is not None:
            await self.ws_send_result(task.task_id, result)

    async def run(self) -> None:
        # Check LLM auth if configured
        settings = self.config.get("settings", {})
        if settings.get("llm"):
            auth_ok, auth_error = await check_llm_auth(settings)
            if auth_ok:
                try:
                    self.llm = await create_llm_client(settings)
                except LLMInitError as e:
                    self._llm_authenticated = False
                    self._llm_error = str(e)
                    print(f"WARNING: LLM init failed: {e}", file=sys.stderr)
            else:
                self._llm_authenticated = False
                self._llm_error = auth_error
                print(f"WARNING: LLM not authenticated: {auth_error}", file=sys.stderr)

        # Initialize agent-specific services
        try:
            await self._init_services()
        except Exception as e:
            self._init_error = str(e)
            print(f"WARNING: Service init failed: {e}", file=sys.stderr)

        app = self.create_app()
        self._app = app
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        self._actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {self._actual_port}")

        await self.register(self._actual_port)

        # Start WS connection (runs forever, auto-reconnects)
        await self._ws_loop()
