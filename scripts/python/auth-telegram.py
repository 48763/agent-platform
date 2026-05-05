# scripts/python/auth-telegram.py
# Telethon session 認證（通用 — 透過 SESSION_DIR / TELETHON_SESSION 控制輸出）
import os
import sys
from telethon.sync import TelegramClient

SESSION_DIR = os.environ["SESSION_DIR"]


def main():
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session_name = os.environ.get("TELETHON_SESSION", "tg_transfer")
    phone = os.environ.get("TELEGRAM_PHONE", "")

    if not api_id or not api_hash:
        print("錯誤: 缺少 TELEGRAM_API_ID 或 TELEGRAM_API_HASH")
        sys.exit(1)
    if not phone:
        print("錯誤: 缺少 TELEGRAM_PHONE")
        sys.exit(1)

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, session_name)

    code = None
    password = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--code" and i + 1 < len(args):
            code = args[i + 1]
            i += 2
        elif args[i] == "--password" and i + 1 < len(args):
            password = args[i + 1]
            i += 2
        else:
            code = code or args[i]
            i += 1

    print(f"Session: {session_path}")
    print(f"Phone: {phone}")
    print(f"API ID: {api_id}")

    client = TelegramClient(session_path, api_id, api_hash)

    start_kwargs = {"phone": phone}
    if code:
        print(f"使用驗證碼: {code}")
        start_kwargs["code_callback"] = lambda: code
    else:
        print("將發送驗證碼到 Telegram，請在提示後輸入...")
        start_kwargs["code_callback"] = lambda: input("請輸入驗證碼: ")
    if password:
        start_kwargs["password"] = password

    client.start(**start_kwargs)

    me = client.get_me()
    print(f"認證成功: {me.first_name} (@{me.username}), id={me.id}")

    client.disconnect()


if __name__ == "__main__":
    main()
