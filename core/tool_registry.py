import inspect
from typing import Any, Callable, get_type_hints

TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def tool(description: str):
    def decorator(func: Callable) -> Callable:
        func._tool_meta = {
            "name": func.__name__,
            "description": description,
        }
        return func
    return decorator


def collect_tools(module) -> list[Callable]:
    tools = []
    for name in dir(module):
        obj = getattr(module, name)
        if callable(obj) and hasattr(obj, "_tool_meta"):
            tools.append(obj)
    return tools


def tools_to_schema(tools: list[Callable]) -> list[dict]:
    schemas = []
    for func in tools:
        meta = func._tool_meta
        hints = get_type_hints(func)
        sig = inspect.signature(func)

        properties = {}
        required = []
        for param_name, param in sig.parameters.items():
            if param_name == "return":
                continue
            param_type = hints.get(param_name, str)
            properties[param_name] = {
                "type": TYPE_MAP.get(param_type, "string"),
            }
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        schemas.append({
            "name": meta["name"],
            "description": meta["description"],
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    return schemas
