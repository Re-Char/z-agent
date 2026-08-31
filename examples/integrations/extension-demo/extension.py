"""Small Z-Agent Python extension used for manual import acceptance."""

from __future__ import annotations

from typing import Any

TOOLS = [
    {
        "name": "hello",
        "description": "返回一条中文问候，用来验证 Extension Host 工具调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "要问候的名字"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sum_numbers",
        "description": "计算数字数组之和。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "numbers": {
                    "type": "array",
                    "items": {"type": "number"},
                    "maxItems": 100,
                }
            },
            "required": ["numbers"],
            "additionalProperties": False,
        },
    },
]


def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "hello":
        target = str(arguments.get("name", "朋友")).strip() or "朋友"
        return {"message": f"你好，{target}！", "extension": "com.zagent.demo"}
    if name == "sum_numbers":
        values = arguments.get("numbers", [])
        if not isinstance(values, list) or not all(isinstance(item, (int, float)) for item in values):
            raise ValueError("numbers 必须是数字数组")
        return {"sum": sum(values), "count": len(values)}
    raise ValueError(f"未知工具：{name}")
