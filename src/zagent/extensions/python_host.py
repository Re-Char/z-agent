from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"


def _load(root_value: str, entry_value: str):
    root = Path(root_value).resolve()
    entry = (root / entry_value).resolve()
    if root not in entry.parents or not entry.is_file() or entry.suffix != ".py":
        raise RuntimeError("invalid Python extension entry")
    spec = importlib.util.spec_from_file_location("zagent_user_extension", entry)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Python extension")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = getattr(module, "TOOLS", None)
    invoke = getattr(module, "invoke", None)
    if not isinstance(tools, list) or not callable(invoke):
        raise RuntimeError("extension must export TOOLS list and invoke(name, arguments)")
    normalized = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise RuntimeError("invalid extension tool declaration")
        schema = tool.get("inputSchema", {"type": "object"})
        if not isinstance(schema, dict):
            raise RuntimeError("extension inputSchema must be an object")
        normalized.append({**tool, "inputSchema": schema})
    return normalized, invoke


def _respond(request_id: Any, *, result: Any = None, error: str | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = {"code": -32000, "message": error}
    else:
        payload["result"] = result
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--entry", required=True)
    args = parser.parse_args()
    tools, invoke = _load(args.root, args.entry)
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if method == "notifications/initialized" or method == "notifications/cancelled":
            continue
        if "id" not in message:
            continue
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "zagent-python-extension-host",
                        "version": "0.2.5",
                        "pid": os.getpid(),
                    },
                }
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                params = message.get("params", {})
                value = invoke(str(params.get("name", "")), params.get("arguments", {}))
                result = value if isinstance(value, dict) and "content" in value else {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "structuredContent": value if isinstance(value, dict) else {"value": value},
                    "isError": False,
                }
            else:
                raise RuntimeError(f"unsupported method: {method}")
            _respond(message["id"], result=result)
        except Exception as exc:  # noqa: BLE001 - extension failures are protocol errors
            _respond(message["id"], error=str(exc))


if __name__ == "__main__":
    main()
