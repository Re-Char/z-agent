from __future__ import annotations

import argparse
import json
from typing import Optional

from zagent.bootstrap import ApplicationContainer
from zagent.server import main as server_main


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="zagent")
    parser.add_argument("--data-dir")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    create = commands.add_parser("session-create")
    create.add_argument("--title", default="新任务")
    events = commands.add_parser("events")
    events.add_argument("session_id")
    search = commands.add_parser("search")
    search.add_argument("session_id")
    search.add_argument("query")
    args = parser.parse_args(argv)
    if args.command == "serve":
        server_args = ["--port", "8765"]
        if args.data_dir:
            server_args.extend(["--data-dir", args.data_dir])
        server_main(server_args)
        return
    container = ApplicationContainer(args.data_dir)
    try:
        if args.command == "session-create":
            result = container.store.create_session(args.title)
        elif args.command == "events":
            result = [event.to_dict() for event in container.store.list_events(args.session_id)]
        else:
            result = container.context.execute(
                args.session_id, "context_search", {"query": args.query, "limit": 10}
            )["results"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        container.close()


if __name__ == "__main__":
    main()
