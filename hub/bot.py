# hub/bot.py
import os
import asyncio
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


class TelegramBot:
    def __init__(self, token: str, hub_url: str):
        self.token = token
        self.hub_url = hub_url

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Agent Platform 已啟動！直接輸入訊息，我會分配給對應的 agent 處理。"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        chat_id = update.effective_chat.id

        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch",
                json={"message": message, "chat_id": chat_id},
            ) as resp:
                result = await resp.json()

        status = result.get("status")
        text = result.get("message", "")
        options = result.get("options")

        if status == "need_approval":
            text = f"⚠️ {text}"

        if options:
            keyboard = [
                [InlineKeyboardButton(opt, callback_data=opt)]
                for opt in options
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = query.data
        chat_id = update.effective_chat.id

        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch",
                json={"message": choice, "chat_id": chat_id},
            ) as resp:
                result = await resp.json()

        text = result.get("message", "")
        options = result.get("options")

        if result.get("status") == "need_approval":
            text = f"⚠️ {text}"

        if options:
            keyboard = [
                [InlineKeyboardButton(opt, callback_data=opt)]
                for opt in options
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await query.edit_message_text(f"{query.message.text}\n\n✅ 選擇: {choice}")
            await query.message.reply_text(text)

    def run(self):
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        logger.info("Telegram Bot started")
        app.run_polling()


def main():
    from core.config import load_config

    config = load_config("config.yaml")
    token_env = config.get("telegram", {}).get("token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(token_env)
    if not token:
        print(f"Error: Set {token_env} environment variable")
        return

    hub_config = config.get("hub", {})
    hub_url = f"http://localhost:{hub_config.get('port', 9000)}"

    bot = TelegramBot(token=token, hub_url=hub_url)
    bot.run()


if __name__ == "__main__":
    main()
