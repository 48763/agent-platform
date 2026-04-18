# gateway/list_chats.py
# List all chats and their IDs for the logged-in account
import os
from telethon.sync import TelegramClient


def main():
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    data_dir = os.environ.get("DATA_DIR", "data/gateway")
    session_path = os.path.join(data_dir, "bot_session")

    client = TelegramClient(session_path, api_id, api_hash)
    client.start()

    print(f"{'Chat ID':<20} {'Type':<10} {'Archived':<10} {'Name'}")
    print("-" * 70)
    for dialog in client.iter_dialogs(archived=False):
        chat_type = "user"
        if dialog.is_group:
            chat_type = "group"
        elif dialog.is_channel:
            chat_type = "channel"
        print(f"{dialog.id:<20} {chat_type:<10} {'':10} {dialog.name}")

    for dialog in client.iter_dialogs(archived=True):
        chat_type = "user"
        if dialog.is_group:
            chat_type = "group"
        elif dialog.is_channel:
            chat_type = "channel"
        print(f"{dialog.id:<20} {chat_type:<10} {'封存':10} {dialog.name}")

    client.disconnect()


if __name__ == "__main__":
    main()
