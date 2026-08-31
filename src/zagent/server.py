from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import socket
from typing import Optional

import uvicorn

from zagent.api import create_api
from zagent.bootstrap import ApplicationContainer


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Z-Agent core service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir")
    parser.add_argument("--project-dir")
    parser.add_argument("--auth-token")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("v1 core service only permits loopback hosts")
    token = args.auth_token if args.auth_token is not None else secrets.token_urlsafe(32)
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((args.host, 0))
            port = probe.getsockname()[1]
    container = ApplicationContainer(args.data_dir, args.project_dir)
    app = create_api(container, token)
    config = uvicorn.Config(app, host=args.host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        # Electron reads this protocol line before making authenticated requests.
        print(json.dumps({"ready": True, "host": args.host, "port": port, "token": token}), flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server.run()
    finally:
        container.close()


if __name__ == "__main__":
    main()
