# run_hub.py
import asyncio
from aiohttp import web
from hub.server import create_hub_app
from core.config import load_config


def main():
    config = load_config("config.yaml")
    hub_config = config.get("hub", {})
    app = create_hub_app(heartbeat_timeout=hub_config.get("heartbeat_timeout", 30))

    host = hub_config.get("host", "0.0.0.0")
    port = hub_config.get("port", 9000)
    print(f"Hub starting on {host}:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
