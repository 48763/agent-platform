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
        self.allowed_chats = allowed_chats  # None = all chats
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)

    async def _dispatch_to_hub(self, message: str, chat_id: int) -> dict:
        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch",
                json={"message": message, "chat_id": chat_id},
            ) as resp:
                return await resp.json()

    def _setup_handlers(self):
        @self.client.on(events.NewMessage)
        async def handler(event):
            # Skip own messages
            if event.out:
                return

            # Skip if not in allowed chats
            if self.allowed_chats and event.chat_id not in self.allowed_chats:
                return

            # Skip non-text messages
            if not event.text:
                return

            chat_id = event.chat_id
            message = event.text

            try:
                result = await self._dispatch_to_hub(message, chat_id)
                text = result.get("message", "")
                status = result.get("status")
                options = result.get("options")

                if status == "need_approval":
                    text = f"⚠️ {text}"

                if options:
                    option_text = "\n".join(f"  {i}. {opt}" for i, opt in enumerate(options, 1))
                    text = f"{text}\n\n{option_text}"

                await event.reply(text)
            except Exception as e:
                logger.error(f"Error dispatching message: {e}")
                await event.reply(f"處理失敗: {e}")

    def run(self):
        self._setup_handlers()
        logger.info("Telegram Userbot starting...")
        self.client.start(phone=self.phone)
        logger.info("Telegram Userbot connected")
        self.client.run_until_disconnected()
