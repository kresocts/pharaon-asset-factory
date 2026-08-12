#!/usr/bin/env python3
"""Report deterministic Phase 0 readiness from canonical repository state."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    from validation.validate_repository import ValidationError, parse_ticket
except ModuleNotFoundError:  # Direct script execution places validation/ on sys.path.
    from validate_repository import ValidationError, parse_ticket


ROOT = Path(__file__).resolve().parents[1]
PHASE_0_TICKET_IDS = tuple(f"T-{number:04d}" for number in range(1, 6))
WORKFLOW_TEST_FILES = (
    ("worker", Path("tests/test_worker_workflow.py")),
    ("reviewer", Path("tests/test_reviewer_workflow.py")),
    ("orchestrator", Path("tests/test_orchestrator_workflow.py")),
)
BASELINE_COMMAND = "python validation/run_ci.py"


@dataclass(frozen=True)
class Phase0Summary:
    ticket_statuses: tuple[tuple[str, str], ...]
    workflow_test_counts: tuple[tuple[str, int], ...]
    baseline_ci_command_exists: bool

    @property
    def all_tickets_done(self) -> bool:
        return all(status == "DONE" for _, status in self.ticket_statuses)

    @property
    def overall_status(self) -> str:
        complete = self.all_tickets_done and self.baseline_ci_command_exists
        return "PHASE_0_READY" if complete else "PHASE_0_INCOMPLETE"


def _ticket_status(root: Path, ticket_id: str) -> str:
    path = root / "tickets" / f"{ticket_id}.md"
    try:
        return parse_ticket(path).status
    except (OSError, UnicodeError, ValidationError):
        return "INVALID_OR_MISSING"


def _test_count(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return 0
    return sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        for node in ast.walk(tree)
    )


def inspect_phase0(root: Path) -> Phase0Summary:
    """Build a Phase 0 summary solely from files under *root*."""
    resolved_root = root.resolve()
    ticket_statuses = tuple(
        (ticket_id, _ticket_status(resolved_root, ticket_id))
        for ticket_id in PHASE_0_TICKET_IDS
    )
    workflow_test_counts = tuple(
        (name, _test_count(resolved_root / relative_path))
        for name, relative_path in WORKFLOW_TEST_FILES
    )
    try:
        readme = (resolved_root / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        readme = ""
    baseline_exists = (
        (resolved_root / "validation" / "run_ci.py").is_file()
        and BASELINE_COMMAND in readme
    )
    return Phase0Summary(ticket_statuses, workflow_test_counts, baseline_exists)


def format_summary(summary: Phase0Summary) -> str:
    """Render a stable, concise human-readable summary."""
    lines = ["Phase 0 tickets:"]
    lines.extend(f"- {ticket_id}: {status}" for ticket_id, status in summary.ticket_statuses)
    lines.append(f"All Phase 0 tickets DONE: {'YES' if summary.all_tickets_done else 'NO'}")
    lines.append("Workflow tests:")
    lines.extend(f"- {name}: {count}" for name, count in summary.workflow_test_counts)
    lines.append(
        "Canonical baseline CI command exists: "
        f"{'YES' if summary.baseline_ci_command_exists else 'NO'}"
    )
    lines.append(f"Overall: {summary.overall_status}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?", type=Path, default=ROOT,
        help="Repository root to inspect (defaults to this repository).",
    )
    args = parser.parse_args(argv)
    summary = inspect_phase0(args.root)
    print(format_summary(summary))
    return 0 if summary.overall_status == "PHASE_0_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
