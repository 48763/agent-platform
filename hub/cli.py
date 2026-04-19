# hub/cli.py
import asyncio
from aiohttp import ClientSession
from core.models import TaskRequest
from hub.router import Router
from hub.task_manager import TaskManager


async def dispatch_message(
    message: str,
    router: Router,
    task_manager: TaskManager,
) -> dict:
    agent = await router.route(message)
    if agent is None:
        return {"status": "error", "message": "無法處理此訊息，沒有可用的 agent"}

    task = task_manager.create_task(
        agent_name=agent.name,
        chat_id=0,  # CLI mode, no chat_id
        content=message,
    )
    task_request = TaskRequest(
        task_id=task.task_id,
        content=message,
        conversation_history=task.conversation_history,
    )
    result = await send_task_to_agent(agent.url, task_request)

    if result.get("status") == "done":
        task_manager.complete_task(task.task_id)
    elif result.get("status") in ("need_input", "need_approval"):
        task.status = f"waiting_{result['status'].split('_')[1]}"

    return result


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
