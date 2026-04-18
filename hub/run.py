# hub/run.py
import os
from aiohttp import web
from hub.server import create_hub_app


def main():
    host = os.environ.get("HUB_HOST", "0.0.0.0")
    port = int(os.environ.get("HUB_PORT", "9000"))
    heartbeat_timeout = int(os.environ.get("HEARTBEAT_TIMEOUT", "30"))

    app = create_hub_app(heartbeat_timeout=heartbeat_timeout)
    print(f"Hub starting on {host}:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
