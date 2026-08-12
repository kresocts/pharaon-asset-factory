"""Prepare one stateless worker execution and print its JSON contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .repository import GitRepository
from .workflow import PreparationState, WorkerWorkflow


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket_id", help="exactly one canonical ticket ID, for example T-0003")
    parser.add_argument("--ensure-branch", action="store_true", help="create or reuse the canonical ticket branch")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)

    preparation = WorkerWorkflow(args.root).prepare(args.ticket_id)
    output = preparation.to_dict()
    if args.ensure_branch and preparation.state == PreparationState.READY:
        assert preparation.context is not None
        output["branch_action"] = GitRepository(args.root).ensure_ticket_branch(
            preparation.context.branch, "origin/main"
        )
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if preparation.state == PreparationState.READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
