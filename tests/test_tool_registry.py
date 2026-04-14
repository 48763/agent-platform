from core.tool_registry import tool, collect_tools, tools_to_schema


def test_tool_decorator():
    @tool(description="Add two numbers")
    def add(a: int, b: int) -> int:
        return a + b

    assert add._tool_meta["description"] == "Add two numbers"
    assert add(1, 2) == 3  # still callable


def test_collect_tools_from_module():
    import types
    mod = types.ModuleType("fake")

    @tool(description="tool A")
    def func_a() -> str:
        return "a"

    @tool(description="tool B")
    def func_b(x: str) -> str:
        return x

    mod.func_a = func_a
    mod.func_b = func_b
    mod.not_a_tool = lambda: None

    tools = collect_tools(mod)
    assert len(tools) == 2
    names = {t._tool_meta["name"] for t in tools}
    assert names == {"func_a", "func_b"}


def test_tools_to_schema():
    @tool(description="Get weather for a city")
    def get_weather(city: str) -> str:
        return f"sunny in {city}"

    schema = tools_to_schema([get_weather])
    assert len(schema) == 1
    s = schema[0]
    assert s["name"] == "get_weather"
    assert s["description"] == "Get weather for a city"
    assert "city" in s["input_schema"]["properties"]
    assert s["input_schema"]["properties"]["city"]["type"] == "string"
    assert s["input_schema"]["required"] == ["city"]
