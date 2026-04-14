from typing import Any, Callable, Optional


class LLMClient:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    async def run(
        self,
        system_prompt: str,
        messages: list[dict],
        tools_schema: list[dict],
        tool_executor: Optional[Callable],
        max_iterations: int = 20,
    ) -> str:
        messages = list(messages)

        for _ in range(max_iterations):
            kwargs = {
                "model": self.model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": messages,
            }
            if tools_schema:
                kwargs["tools"] = tools_schema

            response = await self.client.messages.create(**kwargs)

            if response.stop_reason == "tool_use":
                tool_calls = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tc in tool_calls:
                    result = await tool_executor(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": str(result),
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                # Extract text from response
                for block in response.content:
                    if block.type == "text":
                        return block.text
                return ""

        return "Error: max iterations reached"
