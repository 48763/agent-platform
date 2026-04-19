# hub/cli.py
import asyncio
from aiohttp import ClientSession


async def cli_loop(hub_url: str = "http://localhost:9000") -> None:
    async with ClientSession() as session:
        print("Agent Platform CLI (輸入 'quit' 離開)")
        print("-" * 40)

        while True:
            try:
                message = input("\n你: ")
            except (EOFError, KeyboardInterrupt):
                print("\n再見！")
                break

            if message.strip().lower() in ("quit", "exit"):
                print("再見！")
                break

            async with session.post(
                f"{hub_url}/dispatch",
                json={"message": message, "chat_id": 0},
            ) as resp:
                result = await resp.json()

            status = result.get("status")
            print(f"\nAgent: {result.get('message', '')}")

            if status in ("need_input", "need_approval"):
                options = result.get("options")
                if options:
                    for i, opt in enumerate(options, 1):
                        print(f"  {i}. {opt}")


def main():
    asyncio.run(cli_loop())


if __name__ == "__main__":
    main()
