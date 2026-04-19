# gateway/telegram_handler.py
import json
import asyncio
import logging
from aiohttp import ClientSession, WSMsgType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from core.ws import MsgType

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, token: str, hub_url: str):
        self.token = token
        self.hub_url = hub_url
        self._ws = None

    async def _ws_send_dispatch(self, message: str, chat_id: int):
        if not self._ws or self._ws.closed:
            logger.error("WS not connected, cannot dispatch")
            return
        await self._ws.send_json({
            "type": MsgType.DISPATCH.value,
            "message": message,
            "chat_id": chat_id,
        })

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Agent Platform 已啟動！直接輸入訊息，我會分配給對應的 agent 處理。"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        chat_id = update.effective_chat.id
        await self._ws_send_dispatch(message, chat_id)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = query.data
        chat_id = update.effective_chat.id
        await self._ws_send_dispatch(choice, chat_id)

    async def _handle_hub_message(self, data: dict, app: Application):
        chat_id = data.get("chat_id")
        text = data.get("message", "")
        status = data.get("status")
        options = data.get("options")

        if not chat_id or not text:
            return

        if status == "need_approval":
            text = f"⚠️ {text}"

        try:
            bot = app.bot
            if options:
                keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in options]
                await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(f"Failed to send TG message: {e}")

    async def _ws_loop(self, app: Application):
        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/gateway"

        while True:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        logger.info(f"WS connected to Hub: {ws_url}")

                        await ws.send_json({
                            "type": MsgType.GW_REGISTER.value,
                            "mode": "bot",
                        })

                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                await self._handle_hub_message(data, app)
                            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break
            except Exception as e:
                logger.warning(f"WS connection lost: {e}")

            self._ws = None
            await asyncio.sleep(3)

    def create_application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        return app

    def run(self):
        app = self.create_application()
        logger.info("Telegram Handler started")

        async def post_init(application):
            asyncio.create_task(self._ws_loop(application))

        app.post_init = post_init
        app.run_polling()
