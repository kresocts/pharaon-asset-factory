from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker import cli
from worker.contract import (
    AttemptResult, AttemptStatus, CriterionEvidence, EvidenceStatus, TestEvidence,
)
from worker.github import GitHubCli
from worker.repository import GitRepository
from worker.workflow import PreparationState, WorkerWorkflow


TICKET_TEMPLATE = """---
id: {ticket_id}
title: {title}
status: {status}
dependencies: [{dependencies}]
priority: 1
---

# {ticket_id} — {title}

## Goal
{goal}
## Context
Fixture context.
## Dependencies
Fixture dependencies.
## Allowed scope
Only fixture scope.
## Acceptance criteria
- [ ] First criterion remains exact.
- [ ] Second criterion remains exact.
## Required tests
- `python -m unittest`
## Out of scope
Everything else.
## Implementation notes
Fixture notes.
"""


class RepositoryFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "tickets").mkdir()
        (self.root / "AGENTS.md").write_text("One ticket. Never self-merge.\n", encoding="utf-8")

    def close(self) -> None:
        self.temporary.cleanup()

    def ticket(
        self, ticket_id: str, status: str, dependencies: tuple[str, ...] = (), title: str = "Fixture work"
    ) -> None:
        text = TICKET_TEMPLATE.format(
            ticket_id=ticket_id, title=title, status=status,
            dependencies=", ".join(dependencies), goal=f"Implement {ticket_id}.",
        )
        (self.root / "tickets" / f"{ticket_id}.md").write_text(text, encoding="utf-8")


class WorkerPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepositoryFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_ready_ticket_generates_complete_deterministic_context(self) -> None:
        self.fixture.ticket("T-0001", "DONE")
        self.fixture.ticket("T-0003", "READY", ("T-0001",), "Define worker-agent workflow")

        first = WorkerWorkflow(self.fixture.root).prepare("T-0003")
        second = WorkerWorkflow(self.fixture.root).prepare("T-0003")

        self.assertEqual(first, second)
        self.assertEqual(first.state, PreparationState.READY)
        assert first.context is not None
        self.assertEqual(first.context.ticket_id, "T-0003")
        self.assertEqual(first.context.dependencies, ("T-0001",))
        self.assertEqual(first.context.acceptance_criteria, (
            "First criterion remains exact.", "Second criterion remains exact.",
        ))
        self.assertEqual(first.context.branch, "ticket/T-0003-define-worker-agent-workflow")
        self.assertIn("Never self-merge", first.context.repository_rules)

    def test_blocked_ticket_returns_exact_state_and_dependency_reasons(self) -> None:
        self.fixture.ticket("T-0001", "IN_PROGRESS")
        self.fixture.ticket("T-0003", "BLOCKED", ("T-0001",))
        original = (self.fixture.root / "tickets" / "T-0003.md").read_text(encoding="utf-8")

        result = WorkerWorkflow(self.fixture.root).prepare("T-0003")

        self.assertEqual(result.state, PreparationState.BLOCKED)
        self.assertEqual(result.reasons, (
            "T-0003 status is BLOCKED, not READY or IN_PROGRESS",
            "dependency T-0001 is IN_PROGRESS, not DONE",
        ))
        self.assertIsNone(result.context)
        self.assertEqual((self.fixture.root / "tickets" / "T-0003.md").read_text(encoding="utf-8"), original)

    def test_superseded_ticket_is_not_runnable(self) -> None:
        self.fixture.ticket("T-0003", "SUPERSEDED")
        result = WorkerWorkflow(self.fixture.root).prepare("T-0003")
        self.assertEqual(result.state, PreparationState.BLOCKED)
        self.assertEqual(result.reasons, (
            "T-0003 status is SUPERSEDED, not READY or IN_PROGRESS",
        ))

    def test_superseded_dependency_blocks_dependent_ticket(self) -> None:
        self.fixture.ticket("T-0001", "SUPERSEDED")
        self.fixture.ticket("T-0003", "READY", ("T-0001",))
        result = WorkerWorkflow(self.fixture.root).prepare("T-0003")
        self.assertEqual(result.state, PreparationState.BLOCKED)
        self.assertEqual(result.reasons, (
            "dependency T-0001 is SUPERSEDED, not DONE",
        ))

    def test_malformed_and_unknown_tickets_are_validation_failures(self) -> None:
        self.assertIn("malformed ticket ID", WorkerWorkflow(self.fixture.root).prepare("3").reasons[0])
        self.assertEqual(
            WorkerWorkflow(self.fixture.root).prepare("T-9999").reasons,
            ("unknown ticket: T-9999",),
        )

    def test_cli_rejects_zero_or_multiple_ticket_ids(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["T-0001", "T-0002"])

    def test_repository_ticket_plain_bullets_are_preserved(self) -> None:
        self.fixture.ticket("T-0003", "READY")
        path = self.fixture.root / "tickets" / "T-0003.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("- [ ]", "-"),
            encoding="utf-8",
        )
        result = WorkerWorkflow(self.fixture.root).prepare("T-0003")
        assert result.context is not None
        self.assertEqual(len(result.context.acceptance_criteria), 2)
        self.assertEqual(
            result.context.acceptance_criteria[0],
            "First criterion remains exact.",
        )

    def test_empty_acceptance_criteria_is_a_validation_failure(self) -> None:
        self.fixture.ticket("T-0003", "READY")
        path = self.fixture.root / "tickets" / "T-0003.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "- [ ] First criterion remains exact.\n- [ ] Second criterion remains exact.",
                "No machine-readable criteria.",
            ),
            encoding="utf-8",
        )
        result = WorkerWorkflow(self.fixture.root).prepare("T-0003")
        self.assertEqual(result.state, PreparationState.VALIDATION_FAILED)
        self.assertEqual(result.reasons, ("T-0003 has no parseable acceptance criteria",))


class AttemptResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepositoryFixture()
        self.fixture.ticket("T-0003", "READY", title="Define worker-agent workflow")
        self.workflow = WorkerWorkflow(self.fixture.root)
        preparation = self.workflow.prepare("T-0003")
        assert preparation.context is not None
        self.context = preparation.context
        self.criteria = tuple(
            CriterionEvidence(item, EvidenceStatus.PASS, "implemented", "covered")
            for item in self.context.acceptance_criteria
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def test_failing_attempt_is_explicit_and_recoverable(self) -> None:
        result = AttemptResult(
            schema_version=1, ticket_id="T-0003", branch=self.context.branch,
            base_branch="main", attempt=1, status=AttemptStatus.IMPLEMENTATION_FAILED,
            tests=(TestEvidence("python -m unittest", EvidenceStatus.FAIL, "one assertion failed"),),
            acceptance_criteria=tuple(
                CriterionEvidence(item, EvidenceStatus.NOT_EVALUATED) for item in self.context.acceptance_criteria
            ),
            failures=("worker implementation raised an exception",),
        )
        self.workflow.validate_result(self.context, result)
        self.assertEqual(result.branch, self.context.branch)
        self.assertEqual(result.to_dict()["status"], "IMPLEMENTATION_FAILED")

    def test_successful_attempt_is_review_ready_with_individual_evidence(self) -> None:
        result = AttemptResult(
            schema_version=1, ticket_id="T-0003", branch=self.context.branch,
            base_branch="main", attempt=2, status=AttemptStatus.REVIEW_READY,
            commit_sha="abc123", pr_reference="https://example.test/pull/3",
            changed_files=("worker/workflow.py",), change_summary="Implemented contract.",
            tests=(TestEvidence("python -m unittest", EvidenceStatus.PASS, "all passed"),),
            acceptance_criteria=self.criteria,
        )
        self.workflow.validate_result(self.context, result)
        self.assertEqual(result.status, AttemptStatus.REVIEW_READY)
        self.assertEqual(result.branch, "ticket/T-0003-define-worker-agent-workflow")

    def test_success_cannot_omit_or_rewrite_acceptance_criteria(self) -> None:
        result = AttemptResult(
            schema_version=1, ticket_id="T-0003", branch=self.context.branch,
            base_branch="main", attempt=1, status=AttemptStatus.REVIEW_READY,
            tests=(TestEvidence("tests", EvidenceStatus.PASS, "passed"),),
            acceptance_criteria=self.criteria[:1],
        )
        with self.assertRaisesRegex(ValueError, "preserve and address every"):
            self.workflow.validate_result(self.context, result)


class BoundaryTests(unittest.TestCase):
    def test_git_adapter_reuses_local_canonical_branch(self) -> None:
        repository = GitRepository(Path("."))
        with patch.object(repository, "_run", side_effect=["ticket/T-0003-example", ""]) as run:
            action = repository.ensure_ticket_branch("ticket/T-0003-example")
        self.assertEqual(action, "reused-local")
        self.assertEqual(run.call_args_list[1].args, ("switch", "ticket/T-0003-example"))

    def test_github_adapter_updates_existing_pr_without_duplicate(self) -> None:
        github = GitHubCli(Path("."))
        existing = '[{"number":3,"url":"https://example.test/pull/3","state":"OPEN"}]'
        with patch.object(github, "_run", side_effect=[existing, ""]) as run:
            pr = github.ensure_pull_request("T-0003", "ticket/T-0003-example", Path("body.md"))
        self.assertEqual(pr.number, 3)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[:2], ("pr", "edit"))
        self.assertFalse(hasattr(github, "approve"))
        self.assertFalse(hasattr(github, "merge"))


if __name__ == "__main__":
    unittest.main()
