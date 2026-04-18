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

    # Collect all dialogs, deduplicate by name+type (keep supergroup over regular group)
    seen = {}  # key: (name, chat_type) -> dialog info
    seen_ids = set()

    for archived in (False, True):
        for dialog in client.iter_dialogs(archived=archived):
            if dialog.id in seen_ids:
                continue
            seen_ids.add(dialog.id)

            chat_type = "user"
            if dialog.is_group:
                chat_type = "group"
            elif dialog.is_channel:
                chat_type = "channel"

            name = dialog.name or "已刪除帳號"
            is_archived = archived

            key = (name, chat_type)
            is_supergroup = str(dialog.id).startswith("-100")

            if key in seen and chat_type == "group":
                prev = seen[key]
                # Keep supergroup ID, discard old regular group ID
                if is_supergroup and not prev["is_supergroup"]:
                    seen[key] = dict(id=dialog.id, chat_type=chat_type,
                                     archived=is_archived, name=name,
                                     is_supergroup=True)
                # If previous was supergroup, skip this regular group
                continue

            seen[key] = dict(id=dialog.id, chat_type=chat_type,
                             archived=is_archived, name=name,
                             is_supergroup=is_supergroup)

    print(f"{'Chat ID':<20} {'Type':<10} {'Archived':<10} {'Name'}")
    print("-" * 70)
    for info in seen.values():
        archived_str = "封存" if info["archived"] else ""
        print(f"{info['id']:<20} {info['chat_type']:<10} {archived_str:10} {info['name']}")

    client.disconnect()


if __name__ == "__main__":
    main()
