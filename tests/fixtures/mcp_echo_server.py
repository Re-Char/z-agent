"""Minimal real stdio MCP server used by end-to-end protocol tests."""

from __future__ import annotations

import json
import sys


def respond(request_id: object, result: dict) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False),
        flush=True,
    )


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        respond(
            message["id"],
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "zagent-test-echo", "version": "1.0.0"},
            },
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        respond(
            message["id"],
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return the supplied text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        text = str(arguments.get("text", ""))
        respond(
            message["id"],
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"echo": text},
                "isError": False,
            },
        )
    elif method == "notifications/cancelled":
        continue
    elif "id" in message:
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
            ),
            flush=True,
        )
