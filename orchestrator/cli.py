"""Inspect or advance repository-backed orchestration by one bounded step."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .persistence import RepositoryStateBackend
from .workflow import Orchestrator


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list-ready", "run-once", "show-state"))
    parser.add_argument("ticket_id", nargs="?", help="required by show-state")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--owner", default="local-orchestrator")
    parser.add_argument("--worker-identity", default="implementation-worker")
    parser.add_argument("--reviewer-identity", default="independent-reviewer")
    args = parser.parse_args(argv)

    backend = RepositoryStateBackend(args.root, args.state_directory)
    orchestrator = Orchestrator(
        args.root, backend, args.worker_identity, args.reviewer_identity,
    )
    if args.command == "list-ready":
        print(json.dumps([
            {"ticket_id": item.ticket_id, "priority": item.priority, "title": item.title}
            for item in orchestrator.list_ready()
        ], indent=2, sort_keys=True))
        return 0
    if args.command == "show-state":
        if not args.ticket_id:
            parser.error("show-state requires ticket_id")
        state = backend.load_state(args.ticket_id)
        print(json.dumps(state.to_dict() if state else None, indent=2, sort_keys=True))
        return 0 if state else 2
    result = orchestrator.run_once(args.owner)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
