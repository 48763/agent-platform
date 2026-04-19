# gateway/telegram_user_handler.py
import asyncio
import json
import logging
from aiohttp import ClientSession, WSMsgType
from telethon import TelegramClient, events
from core.ws import MsgType, ws_msg, ws_parse

logger = logging.getLogger(__name__)

MESSAGE_BATCH_DELAY = 5


class TelegramUserHandler:
    def __init__(self, api_id: int, api_hash: str, phone: str, hub_url: str,
                 session_path: str = "gateway/bot_session",
                 allowed_chats: list[int] | None = None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.hub_url = hub_url
        self.session_path = session_path
        self.allowed_chats = allowed_chats
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)

        self._buffers: dict[int, list[tuple[str, any]]] = {}
        self._buffer_timers: dict[int, asyncio.Task] = {}
        self._ws = None

    async def _ws_send_dispatch(self, message: str, chat_id: int,
                                 reply_to_message_id: int | None = None):
        if not self._ws or self._ws.closed:
            logger.error("WS not connected, cannot dispatch")
            return
        payload = {
            "type": MsgType.DISPATCH.value,
            "message": message,
            "chat_id": chat_id,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        await self._ws.send_json(payload)

    async def _handle_hub_message(self, data: dict):
        msg_type = data.get("type")
        chat_id = data.get("chat_id")
        text = data.get("message", "")
        status = data.get("status")
        task_id = data.get("task_id")
        options = data.get("options")

        if not chat_id or not text:
            return

        if status == "need_approval":
            text = f"⚠️ {text}"

        if options:
            option_text = "\n".join(f"  {i}. {opt}" for i, opt in enumerate(options, 1))
            text = f"{text}\n\n{option_text}"

        try:
            entity = await self.client.get_entity(chat_id)
            sent = await self.client.send_message(entity, text)

            if task_id and sent:
                await self._notify_message_id(task_id, sent.id)
        except Exception as e:
            logger.error(f"Failed to send TG message to {chat_id}: {e}")

    async def _notify_message_id(self, task_id: str, message_id: int):
        try:
            async with ClientSession() as session:
                await session.post(
                    f"{self.hub_url}/set_message_id",
                    json={"task_id": task_id, "message_id": message_id},
                )
        except Exception as e:
            logger.error(f"Failed to set message_id: {e}")

    async def _flush_buffer(self, chat_id: int):
        await asyncio.sleep(MESSAGE_BATCH_DELAY)

        buffer = self._buffers.pop(chat_id, [])
        self._buffer_timers.pop(chat_id, None)

        if not buffer:
            return

        merged_text = "\n".join(text for text, _ in buffer)
        last_event = buffer[-1][1]

        try:
            await self._ws_send_dispatch(merged_text, chat_id)
        except Exception as e:
            logger.error(f"Error dispatching merged message: {e}")
            await last_event.reply(f"處理失敗: {type(e).__name__}: {e}")

    def _setup_handlers(self):
        @self.client.on(events.NewMessage)
        async def handler(event):
            logger.debug(f"NewMessage: chat_id={event.chat_id} out={event.out} text={bool(event.text)} reply={bool(event.reply_to)}")
            if event.out:
                return
            if self.allowed_chats and event.chat_id not in self.allowed_chats:
                logger.debug(f"Skipped: chat_id={event.chat_id} not in allowed_chats={self.allowed_chats}")
                return
            if not event.text:
                return

            chat_id = event.chat_id
            message = event.text
            logger.info(f"Dispatching message from chat_id={chat_id}: {message[:50]}")

            if event.reply_to and event.reply_to.reply_to_msg_id:
                reply_to_message_id = event.reply_to.reply_to_msg_id
                try:
                    await self._ws_send_dispatch(message, chat_id, reply_to_message_id)
                except Exception as e:
                    logger.error(f"Error dispatching reply: {e}")
                    await event.reply(f"處理失敗: {type(e).__name__}: {e}")
                return

            if chat_id not in self._buffers:
                self._buffers[chat_id] = []

            self._buffers[chat_id].append((message, event))

            if chat_id in self._buffer_timers:
                self._buffer_timers[chat_id].cancel()

            self._buffer_timers[chat_id] = asyncio.create_task(
                self._flush_buffer(chat_id)
            )

    async def _ws_loop(self):
        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/gateway"

        while True:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        logger.info(f"WS connected to Hub: {ws_url}")

                        phone_masked = self.phone[:4] + "***" + self.phone[-3:] if self.phone else None
                        await ws.send_json({
                            "type": MsgType.GW_REGISTER.value,
                            "platform": "telegram",
                            "mode": "userbot",
                            "phone": phone_masked,
                            "allowed_chats": self.allowed_chats,
                        })

                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                await self._handle_hub_message(data)
                            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break

            except Exception as e:
                logger.warning(f"WS connection lost: {e}")

            self._ws = None
            logger.info("Reconnecting to Hub in 3 seconds...")
            await asyncio.sleep(3)

    def run(self):
        self._setup_handlers()
        logger.info("Telegram Userbot starting...")
        self.client.start(phone=self.phone)
        logger.info("Telegram Userbot connected")

        loop = self.client.loop
        loop.create_task(self._ws_loop())

        self.client.run_until_disconnected()
