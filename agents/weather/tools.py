# agents/weather/tools.py
from aiohttp import ClientSession
from core.tool_registry import tool


@tool(description="查詢指定城市的天氣資訊")
async def get_weather(city: str) -> str:
    url = f"https://wttr.in/{city}?format=3&lang=zh-tw"
    try:
        async with ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.text()
                return f"查詢天氣時發生錯誤 (HTTP {resp.status})"
    except Exception as e:
        return f"查詢天氣時發生錯誤: {e}"
