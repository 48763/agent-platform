# gateway/__main__.py
import os
import logging
from gateway.telegram_handler import TelegramHandler

logging.basicConfig(level=logging.INFO)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: Set TELEGRAM_BOT_TOKEN environment variable")
        return

    hub_url = os.environ.get("HUB_URL", "http://hub:9000")
    handler = TelegramHandler(token=token, hub_url=hub_url)
    handler.run()


if __name__ == "__main__":
    main()
