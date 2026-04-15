# gateway/telegram_handler.py
import os
import logging
from aiohttp import ClientSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, token: str, hub_url: str):
        self.token = token
        self.hub_url = hub_url

    async def _dispatch_to_hub(self, message: str, chat_id: int) -> dict:
        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch",
                json={"message": message, "chat_id": chat_id},
            ) as resp:
                return await resp.json()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Agent Platform 已啟動！直接輸入訊息，我會分配給對應的 agent 處理。"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        chat_id = update.effective_chat.id
        result = await self._dispatch_to_hub(message, chat_id)
        await self._send_response(update.message.reply_text, result)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = query.data
        chat_id = update.effective_chat.id
        result = await self._dispatch_to_hub(choice, chat_id)

        text = result.get("message", "")
        options = result.get("options")

        if result.get("status") == "need_approval":
            text = f"⚠️ {text}"

        if options:
            keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in options]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text(f"{query.message.text}\n\n✅ 選擇: {choice}")
            await query.message.reply_text(text)

    async def _send_response(self, reply_func, result: dict):
        text = result.get("message", "")
        options = result.get("options")
        status = result.get("status")

        if status == "need_approval":
            text = f"⚠️ {text}"

        if options:
            keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in options]
            await reply_func(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await reply_func(text)

    def create_application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        return app

    def run(self):
        app = self.create_application()
        logger.info("Telegram Handler started")
        app.run_polling()
