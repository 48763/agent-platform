# gateway/list_chats.py
# List all chats and their IDs for the logged-in account
import os
from telethon.sync import TelegramClient


def main():
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session_path = os.environ.get("SESSION_PATH", "data/gateway/bot_session")

    client = TelegramClient(session_path, api_id, api_hash)
    client.start()

    print(f"{'Chat ID':<20} {'Type':<10} {'Name'}")
    print("-" * 60)
    for dialog in client.iter_dialogs():
        chat_type = "user"
        if dialog.is_group:
            chat_type = "group"
        elif dialog.is_channel:
            chat_type = "channel"
        print(f"{dialog.id:<20} {chat_type:<10} {dialog.name}")

    client.disconnect()


if __name__ == "__main__":
    main()
