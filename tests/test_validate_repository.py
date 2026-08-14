from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from shutil import copytree

from validation.validate_repository import ValidationError, parse_ticket, validate_repository


VALID_TICKET = """---
id: T-0001
title: Example ticket
status: READY
dependencies: []
priority: 1
---

# T-0001 Ã¢â‚¬â€ Example ticket

## Goal
Goal.
## Context
Context.
## Dependencies
None.
## Allowed scope
Scope.
## Acceptance criteria
- Done.
## Required tests
- Tests.
## Out of scope
Nothing.
## Implementation notes
Notes.
"""


class ParseTicketTests(unittest.TestCase):
    def write_ticket(self, directory: Path, text: str = VALID_TICKET, name: str = "T-0001.md") -> Path:
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_valid_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ticket = parse_ticket(self.write_ticket(Path(temporary)))
        self.assertEqual(ticket.ticket_id, "T-0001")
        self.assertEqual(ticket.dependencies, ())
        self.assertEqual(ticket.priority, 1)

    def test_rejects_filename_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_ticket(Path(temporary), name="T-9999.md")
            with self.assertRaisesRegex(ValidationError, "must match filename"):
                parse_ticket(path)

    def test_rejects_invalid_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_ticket(Path(temporary), VALID_TICKET.replace("status: READY", "status: NEW"))
            with self.assertRaisesRegex(ValidationError, "invalid status"):
                parse_ticket(path)

    def test_rejects_missing_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_ticket(Path(temporary), VALID_TICKET.replace("## Goal\nGoal.\n", ""))
            with self.assertRaisesRegex(ValidationError, "missing sections: Goal"):
                parse_ticket(path)

    def test_rejects_self_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_ticket(Path(temporary), VALID_TICKET.replace("dependencies: []", "dependencies: [T-0001]"))
            with self.assertRaisesRegex(ValidationError, "depend on itself"):
                parse_ticket(path)


class RepositoryTests(unittest.TestCase):
    def test_current_repository_is_valid(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tickets = validate_repository(root)
        self.assertEqual(
            {ticket.ticket_id for ticket in tickets},
            {
                *(f"T-{number:04d}" for number in range(1, 7)),
                "T-0010",
                "T-0011",
                "T-0012",
                "T-0013",
                "T-0014",
                "T-0015",
                "T-0016",
                "T-0017",
                "T-0018",
                "T-0019",
            },
        )

    def test_rejects_missing_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "repository"
            copytree(root, copy, ignore=lambda _directory, names: {".git", "__pycache__"} & set(names))
            ticket_path = copy / "tickets" / "T-0005.md"
            ticket_path.write_text(
                ticket_path.read_text(encoding="utf-8").replace("T-0004]", "T-9999]"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "dependency does not exist: T-9999"):
                validate_repository(copy)


if __name__ == "__main__":
    unittest.main()
