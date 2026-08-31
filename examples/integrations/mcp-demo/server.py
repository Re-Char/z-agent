"""Self-contained stdio MCP server for Z-Agent manual import acceptance."""

from __future__ import annotations

import json
import sys
from typing import Any

TOOLS = [
    {
        "name": "echo",
        "description": "原样返回文本，用于验证真实 MCP stdio 链路。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sum_numbers",
        "description": "计算数字数组之和。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "numbers": {"type": "array", "items": {"type": "number"}, "maxItems": 100}
            },
            "required": ["numbers"],
            "additionalProperties": False,
        },
    },
]


def respond(request_id: Any, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        message["result"] = result or {}
    else:
        message["error"] = {"code": -32000, "message": error}
    print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        text = str(arguments.get("text", ""))
        structured = {"echo": text, "server": "zagent-demo-mcp"}
    elif name == "sum_numbers":
        values = arguments.get("numbers", [])
        if not isinstance(values, list) or not all(isinstance(item, (int, float)) for item in values):
            raise ValueError("numbers 必须是数字数组")
        structured = {"sum": sum(values), "count": len(values)}
    else:
        raise ValueError(f"未知工具：{name}")
    return {
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "structuredContent": structured,
        "isError": False,
    }


for raw_line in sys.stdin:
    message: Any = None
    try:
        message = json.loads(raw_line)
        method = message.get("method")
        if method in {"notifications/initialized", "notifications/cancelled"}:
            continue
        if "id" not in message:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "zagent-demo-mcp", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            result = call_tool(str(params.get("name", "")), params.get("arguments", {}))
        else:
            raise ValueError(f"不支持的方法：{method}")
        respond(message["id"], result=result)
    except Exception as exc:  # noqa: BLE001 - report protocol errors to the client
        if isinstance(message, dict) and "id" in message:
            respond(message["id"], error=str(exc))
