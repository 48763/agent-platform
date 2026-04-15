# gateway/__main__.py
import os
import logging

logging.basicConfig(level=logging.INFO)


def main():
    hub_url = os.environ.get("HUB_URL", "http://hub:9000")
    mode = os.environ.get("GATEWAY_MODE", "bot")

    if mode == "userbot":
        from gateway.telegram_user_handler import TelegramUserHandler

        api_id = os.environ.get("TELEGRAM_API_ID")
        api_hash = os.environ.get("TELEGRAM_API_HASH")
        phone = os.environ.get("TELEGRAM_PHONE")

        if not all([api_id, api_hash, phone]):
            print("Error: Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE")
            return

        # Optional: restrict to specific chat IDs
        allowed_chats = os.environ.get("ALLOWED_CHATS")
        chat_list = None
        if allowed_chats:
            chat_list = [int(c.strip()) for c in allowed_chats.split(",")]

        session_path = os.environ.get("SESSION_PATH", "gateway/bot_session")

        handler = TelegramUserHandler(
            api_id=int(api_id),
            api_hash=api_hash,
            phone=phone,
            hub_url=hub_url,
            session_path=session_path,
            allowed_chats=chat_list,
        )
        handler.run()

    else:
        from gateway.telegram_handler import TelegramHandler

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print("Error: Set TELEGRAM_BOT_TOKEN environment variable")
            return

        handler = TelegramHandler(token=token, hub_url=hub_url)
        handler.run()


if __name__ == "__main__":
    main()
