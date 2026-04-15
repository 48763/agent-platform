# gateway/telegram_user_handler.py
import logging
from aiohttp import ClientSession
from telethon import TelegramClient, events

logger = logging.getLogger(__name__)


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

    async def _dispatch_to_hub(self, message: str, chat_id: int,
                                reply_to_message_id: int | None = None) -> dict:
        payload = {"message": message, "chat_id": chat_id}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch", json=payload,
            ) as resp:
                return await resp.json()

    async def _notify_message_id(self, task_id: str, message_id: int):
        """Tell Hub which message_id the bot replied with, for reply-based lookup."""
        try:
            async with ClientSession() as session:
                await session.post(
                    f"{self.hub_url}/set_message_id",
                    json={"task_id": task_id, "message_id": message_id},
                )
        except Exception as e:
            logger.error(f"Failed to set message_id: {e}")

    def _setup_handlers(self):
        @self.client.on(events.NewMessage)
        async def handler(event):
            if event.out:
                return
            if self.allowed_chats and event.chat_id not in self.allowed_chats:
                return
            if not event.text:
                return

            chat_id = event.chat_id
            message = event.text

            # Check if this is a reply to one of our messages
            reply_to_message_id = None
            if event.reply_to and event.reply_to.reply_to_msg_id:
                reply_to_message_id = event.reply_to.reply_to_msg_id

            try:
                result = await self._dispatch_to_hub(message, chat_id, reply_to_message_id)
                text = result.get("message", "")
                status = result.get("status")
                options = result.get("options")
                task_id = result.get("task_id")

                if status == "need_approval":
                    text = f"⚠️ {text}"

                if options:
                    option_text = "\n".join(f"  {i}. {opt}" for i, opt in enumerate(options, 1))
                    text = f"{text}\n\n{option_text}"

                # Reply and store the message_id
                sent = await event.reply(text)

                # Notify Hub of the bot's message_id for future reply lookups
                if task_id and sent:
                    await self._notify_message_id(task_id, sent.id)

            except Exception as e:
                logger.error(f"Error dispatching message: {e}")
                await event.reply(f"處理失敗: {e}")

    def run(self):
        self._setup_handlers()
        logger.info("Telegram Userbot starting...")
        self.client.start(phone=self.phone)
        logger.info("Telegram Userbot connected")
        self.client.run_until_disconnected()
