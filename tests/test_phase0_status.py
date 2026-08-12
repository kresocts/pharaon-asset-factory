from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from validation.phase0_status import format_summary, inspect_phase0, main


TICKET_TEMPLATE = """---
id: {ticket_id}
title: Phase 0 fixture
status: {status}
dependencies: []
priority: 1
---

# {ticket_id} - Phase 0 fixture

## Goal
Validate the fixture.
## Context
Phase 0 status test fixture.
## Dependencies
None.
## Allowed scope
Fixture only.
## Acceptance criteria
- Report status.
## Required tests
- `python -m unittest`
## Out of scope
External services.
## Implementation notes
Keep deterministic.
"""


class Phase0StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "tickets").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "validation").mkdir()
        for number in range(1, 6):
            ticket_id = f"T-{number:04d}"
            (self.root / "tickets" / f"{ticket_id}.md").write_text(
                TICKET_TEMPLATE.format(ticket_id=ticket_id, status="DONE"),
                encoding="utf-8",
            )
        for workflow, count in {"worker": 2, "reviewer": 3, "orchestrator": 4}.items():
            methods = "\n".join(
                f"    def test_case_{index}(self):\n        pass"
                for index in range(count)
            )
            (self.root / "tests" / f"test_{workflow}_workflow.py").write_text(
                f"class WorkflowTests:\n{methods}\n", encoding="utf-8"
            )
        (self.root / "validation" / "run_ci.py").write_text("# fixture\n", encoding="utf-8")
        (self.root / "README.md").write_text(
            "Run `python validation/run_ci.py`.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_phase0_is_ready_with_zero_exit(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([str(self.root)])

        self.assertEqual(exit_code, 0)
        self.assertIn("T-0001: DONE", output.getvalue())
        self.assertIn("T-0005: DONE", output.getvalue())
        self.assertIn("worker: 2", output.getvalue())
        self.assertIn("reviewer: 3", output.getvalue())
        self.assertIn("orchestrator: 4", output.getvalue())
        self.assertIn("Overall: PHASE_0_READY", output.getvalue())

    def test_incomplete_phase0_identifies_ticket_and_returns_nonzero(self) -> None:
        ticket_path = self.root / "tickets" / "T-0003.md"
        ticket_path.write_text(
            TICKET_TEMPLATE.format(ticket_id="T-0003", status="REVIEW"), encoding="utf-8"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([str(self.root)])

        self.assertEqual(exit_code, 1)
        self.assertIn("T-0003: REVIEW", output.getvalue())
        self.assertIn("All Phase 0 tickets DONE: NO", output.getvalue())
        self.assertIn("Overall: PHASE_0_INCOMPLETE", output.getvalue())

    def test_same_repository_state_produces_same_logical_output(self) -> None:
        first = inspect_phase0(self.root)
        second = inspect_phase0(self.root)

        self.assertEqual(first, second)
        self.assertEqual(format_summary(first), format_summary(second))


if __name__ == "__main__":
    unittest.main()
