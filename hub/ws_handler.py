# hub/ws_handler.py
"""WebSocket endpoint handlers for agent and gateway connections."""
import logging
import os
import time

from aiohttp import web, WSMsgType

from core.models import TaskRequest
from core.ws import MsgType, ws_msg, ws_parse
from hub.gemini_fallback import GeminiChat, gemini_unified_route
from hub.router import Router
from hub.task_manager import TaskManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent WS endpoint: /ws/agent/{name}
# ---------------------------------------------------------------------------

async def handle_agent_ws(request: web.Request) -> web.WebSocketResponse:
    name = request.match_info["name"]
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    registry = request.app["registry"]
    task_manager: TaskManager = request.app["task_manager"]

    registry.set_ws(name, ws)
    logger.info("Agent WS connected: %s", name)

    try:
        async for raw_msg in ws:
            if raw_msg.type == WSMsgType.TEXT:
                try:
                    data = ws_parse(raw_msg.data)
                except Exception:
                    logger.warning("Agent %s sent invalid WS message", name)
                    continue

                msg_type = data.get("type")
                if msg_type == MsgType.RESULT.value:
                    await _handle_agent_result(request.app, data)
                elif msg_type == MsgType.PROGRESS.value:
                    await _forward_progress_to_gateway(request.app, data)
                else:
                    logger.warning("Agent %s sent unknown type: %s", name, msg_type)
            elif raw_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        registry.remove_ws(name)
        logger.info("Agent WS disconnected: %s", name)
        await _close_agent_tasks(task_manager, name, request.app)

    return ws


# ---------------------------------------------------------------------------
# Gateway WS endpoint: /ws/gateway
# ---------------------------------------------------------------------------

async def handle_gateway_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    gw_connections: list = request.app["gateway_connections"]
    gw_info = {"ws": ws, "platform": None, "mode": None, "phone": None, "allowed_chats": None}
    gw_connections.append(gw_info)
    logger.info("Gateway WS connected (total: %d)", len(gw_connections))

    try:
        async for raw_msg in ws:
            if raw_msg.type == WSMsgType.TEXT:
                try:
                    data = ws_parse(raw_msg.data)
                except Exception:
                    logger.warning("Gateway sent invalid WS message")
                    continue

                msg_type = data.get("type")
                if msg_type == MsgType.GW_REGISTER.value:
                    gw_info["platform"] = data.get("platform")
                    gw_info["mode"] = data.get("mode")
                    gw_info["phone"] = data.get("phone")
                    gw_info["allowed_chats"] = data.get("allowed_chats")
                    logger.info("Gateway registered: platform=%s mode=%s phone=%s", gw_info["platform"], gw_info["mode"], gw_info["phone"])
                elif msg_type == MsgType.DISPATCH.value:
                    await _handle_gateway_dispatch(request.app, ws, data)
                else:
                    logger.warning("Gateway sent unknown type: %s", msg_type)
            elif raw_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        if gw_info in gw_connections:
            gw_connections.remove(gw_info)
        logger.info("Gateway WS disconnected (remaining: %d)", len(gw_connections))

    return ws


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _handle_agent_result(app: web.Application, data: dict):
    """Process a result message from an agent, update task, forward to gateway."""
    task_manager: TaskManager = app["task_manager"]
    task_id = data.get("task_id")
    status = data.get("status")
    message = data.get("message")

    if not task_id:
        logger.warning("Agent result missing task_id")
        return

    # Update task status
    if status == "done":
        task_manager.complete_task(task_id)
    elif status == "need_input":
        task_manager.update_status(task_id, "waiting_input")
    elif status == "need_approval":
        task_manager.update_status(task_id, "waiting_approval")
    elif status == "error":
        task_manager.complete_task(task_id)
    elif status == "cancelled":
        task_manager.close_task(task_id)

    # Append assistant response
    if message:
        task_manager.append_assistant_response(task_id, message)

    # Forward reply to gateway
    task = task_manager.get_task(task_id)
    if task and message:
        await _send_to_gateway(
            app,
            task["chat_id"],
            ws_msg(MsgType.REPLY,
                   chat_id=task["chat_id"],
                   task_id=task_id,
                   status=status or "done",
                   message=message,
                   options=data.get("options")),
        )


async def _forward_progress_to_gateway(app: web.Application, data: dict):
    """Forward a progress message from agent to gateway."""
    task_manager: TaskManager = app["task_manager"]
    task_id = data.get("task_id")
    if not task_id:
        return
    task = task_manager.get_task(task_id)
    if not task:
        return
    await _send_to_gateway(
        app,
        task["chat_id"],
        ws_msg(MsgType.PROGRESS,
               chat_id=task["chat_id"],
               task_id=task_id,
               message=data.get("message", "")),
    )


async def _send_to_gateway(app: web.Application, chat_id: int, message_str: str):
    """Find the appropriate gateway WS and send a message."""
    gw_connections: list = app.get("gateway_connections", [])
    for gw_info in gw_connections:
        ws = gw_info["ws"]
        if not ws.closed:
            try:
                await ws.send_str(message_str)
                return
            except Exception:
                logger.exception("Failed to send to gateway")
    logger.warning("No gateway connected for chat_id=%s", chat_id)


async def _close_agent_tasks(task_manager: TaskManager, agent_name: str, app: web.Application):
    """On agent disconnect, mark all its active tasks as error and notify gateway."""
    import sqlite3
    rows = task_manager._conn.execute(
        "SELECT * FROM tasks WHERE agent_name = ? AND status IN ('working', 'waiting_input', 'waiting_approval')",
        (agent_name,),
    ).fetchall()
    for row in rows:
        task = task_manager._row_to_dict(row)
        task_manager.complete_task(task["task_id"])
        task_manager.append_assistant_response(task["task_id"], f"Agent {agent_name} 已斷線")
        await _send_to_gateway(
            app,
            task["chat_id"],
            ws_msg(MsgType.REPLY,
                   chat_id=task["chat_id"],
                   task_id=task["task_id"],
                   status="error",
                   message=f"Agent {agent_name} 已斷線"),
        )


# ---------------------------------------------------------------------------
# Gateway dispatch logic (mirrors server.py handle_dispatch)
# ---------------------------------------------------------------------------

async def _handle_gateway_dispatch(app: web.Application, gw_ws: web.WebSocketResponse, data: dict):
    """Dispatch a message from the gateway, replicating hub/server.py dispatch logic."""
    message = data.get("message", "")
    chat_id = data.get("chat_id", 0)
    reply_to_message_id = data.get("reply_to_message_id")
    source = data.get("source", "telegram")

    task_manager: TaskManager = app["task_manager"]
    registry = app["registry"]
    chat: GeminiChat = app["chat"]

    # Run lifecycle transitions
    task_manager.run_lifecycle()

    # Handle /clear command
    if message.strip() == "/clear":
        active = task_manager.get_active_task_for_chat(chat_id)
        if active:
            task_manager.complete_task(active["task_id"])
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="done", message="對話已結束"))
        else:
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="done", message="沒有進行中的對話"))
        return

    # Priority 1: Reply to a specific bot message -> exact task match
    if reply_to_message_id:
        task = task_manager.get_task_by_message_id(chat_id, reply_to_message_id)
        if task:
            if task["status"] == "closed":
                pass  # closed tasks cannot be reopened via reply
            elif task["status"] in ("done", "archived"):
                task_manager.update_status(task["task_id"], "working")
                await _continue_task_ws(app, gw_ws, task, message, chat_id)
                return
            else:
                await _continue_task_ws(app, gw_ws, task, message, chat_id)
                return

    # Priority 2: Active task waiting for input -> direct continuation
    active_task = task_manager.get_active_task_for_chat(chat_id)
    if active_task and active_task["status"] in ("waiting_input", "waiting_approval"):
        await _continue_task_ws(app, gw_ws, active_task, message, chat_id)
        return

    # Priority 3: Keyword match (fast, no AI)
    router: Router = app["router"]
    keyword_match = router.match_by_keyword(message)
    if keyword_match:
        task = task_manager.create_task(
            agent_name=keyword_match.name, chat_id=chat_id, content=message, source=source,
        )
        await _dispatch_to_agent_ws(app, gw_ws, task, message, chat_id)
        return

    # Priority 4: Unified Gemini flash routing
    if app["use_gemini_fallback"]:
        active_tasks = _get_all_active_tasks(task_manager, chat_id)
        online_agents = [a for a in registry.list_online() if a.priority >= 0]

        decision = await gemini_unified_route(message, active_tasks, online_agents)
        action = decision.get("action")

        if action == "continue":
            task = task_manager.get_task(decision["task_id"])
            if task:
                await _continue_task_ws(app, gw_ws, task, message, chat_id)
                return

        elif action == "route":
            agent_name = decision["agent_name"]
            agent_info = registry.get(agent_name)
            if agent_info:
                task = task_manager.create_task(
                    agent_name=agent_name, chat_id=chat_id, content=message, source=source,
                )
                await _dispatch_to_agent_ws(app, gw_ws, task, message, chat_id)
                return

        # action == "chat" or fallthrough
        await _hub_chat_reply_ws(app, gw_ws, chat_id, message, source)
        return

    # No gemini fallback - error
    await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="無法處理此訊息"))


def _get_all_active_tasks(task_manager: TaskManager, chat_id: int) -> list[dict]:
    """Get all non-closed tasks for a chat (for unified router context)."""
    expiry_days = int(os.environ.get("TASK_EXPIRY_DAYS", "7"))
    expiry = time.time() - (expiry_days * 86400)
    rows = task_manager._conn.execute(
        "SELECT * FROM tasks WHERE chat_id = ? AND status NOT IN ('archived', 'closed') AND updated_at > ? ORDER BY updated_at DESC LIMIT 10",
        (chat_id, expiry),
    ).fetchall()
    return [task_manager._row_to_dict(r) for r in rows]


async def _continue_task_ws(app: web.Application, gw_ws: web.WebSocketResponse,
                            task: dict, message: str, chat_id: int):
    """Continue an existing task over WS."""
    task_manager: TaskManager = app["task_manager"]
    chat: GeminiChat = app["chat"]
    registry = app["registry"]

    task_manager.append_user_response(task["task_id"], message)
    task = task_manager.get_task(task["task_id"])

    if task["agent_name"] == "_hub":
        reply = await chat.reply_with_context(task["conversation_history"])
        if reply:
            task_manager.append_assistant_response(task["task_id"], reply)
            task_manager.complete_task(task["task_id"])
            await gw_ws.send_str(ws_msg(MsgType.REPLY,
                                        chat_id=chat_id,
                                        task_id=task["task_id"],
                                        status="done",
                                        message=reply))
        else:
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="無法處理此訊息"))
        return

    # Agent task: send via WS
    agent_ws = registry.get_ws(task["agent_name"])
    if agent_ws is None:
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="Agent 已離線"))
        return

    try:
        await agent_ws.send_str(ws_msg(MsgType.TASK,
                                       task_id=task["task_id"],
                                       content=message,
                                       conversation_history=task["conversation_history"],
                                       chat_id=chat_id))
    except Exception:
        logger.exception("Failed to send task to agent %s via WS", task["agent_name"])
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="Agent 通訊失敗"))


async def _dispatch_to_agent_ws(app: web.Application, gw_ws: web.WebSocketResponse,
                                task: dict, message: str, chat_id: int):
    """Dispatch a new task to an agent via WS."""
    registry = app["registry"]
    agent_ws = registry.get_ws(task["agent_name"])

    if agent_ws is None:
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="Agent 已離線"))
        return

    try:
        await agent_ws.send_str(ws_msg(MsgType.TASK,
                                       task_id=task["task_id"],
                                       content=message,
                                       conversation_history=task["conversation_history"],
                                       chat_id=chat_id))
    except Exception:
        logger.exception("Failed to dispatch task to agent %s via WS", task["agent_name"])
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="Agent 通訊失敗"))


async def _hub_chat_reply_ws(app: web.Application, gw_ws: web.WebSocketResponse,
                             chat_id: int, message: str, source: str = "telegram"):
    """Hub replies directly via Gemini Chat over WS."""
    task_manager: TaskManager = app["task_manager"]
    chat: GeminiChat = app["chat"]

    # Check if there's an existing hub chat task
    active = task_manager.get_active_task_for_chat(chat_id)
    if active and active["agent_name"] == "_hub":
        task_manager.append_user_response(active["task_id"], message)
        task = task_manager.get_task(active["task_id"])
        reply = await chat.reply_with_context(task["conversation_history"])
    else:
        reply = await chat.reply(message)
        if reply:
            task = task_manager.create_task(
                agent_name="_hub", chat_id=chat_id, content=message, source=source,
            )
        else:
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="無法處理此訊息"))
            return

    if reply:
        task_manager.append_assistant_response(task["task_id"], reply)
        task_manager.complete_task(task["task_id"])
        await gw_ws.send_str(ws_msg(MsgType.REPLY,
                                    chat_id=chat_id,
                                    task_id=task["task_id"],
                                    status="done",
                                    message=reply))
    else:
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, status="error", message="無法處理此訊息"))
