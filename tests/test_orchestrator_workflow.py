from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from orchestrator.contract import (
    FailureCategory,
    OrchestrationStage,
    RetryPolicy,
)
from orchestrator.persistence import InMemoryBackend, WorkflowEvidence
from orchestrator.workflow import Orchestrator, select_ready_ticket
from reviewer.contract import (
    CheckConclusion,
    CheckResult,
    CriterionFinding,
    FindingStatus,
    PriorReviewComment,
    PullRequestEvidence,
    ReviewDecision,
    ReviewResult,
)
from validation.validate_repository import Ticket
from worker.contract import (
    AttemptResult,
    AttemptStatus,
    CriterionEvidence,
    EvidenceStatus,
    TestEvidence,
)


TICKET_TEMPLATE = """---
id: {ticket_id}
title: {title}
status: READY
dependencies: []
priority: {priority}
---

# {ticket_id} - {title}

## Goal
Implement the fixture.
## Context
Fixture context.
## Dependencies
None.
## Allowed scope
Fixture files only.
## Acceptance criteria
- Preserve the first criterion.
- Preserve the second criterion.
## Required tests
- `python -m unittest`
## Out of scope
Paid services and merging.
## Implementation notes
Keep it deterministic.
"""


def ticket(ticket_id: str, status: str = "READY", dependencies: tuple[str, ...] = (), priority: int = 1) -> Ticket:
    return Ticket(Path(f"tickets/{ticket_id}.md"), ticket_id, f"Ticket {ticket_id}", status, dependencies, priority)


class WorkflowFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "tickets").mkdir()
        (self.root / "AGENTS.md").write_text("Never self-review or merge.\n", encoding="utf-8")
        self.ticket_id = "T-0010"
        self.title = "Fixture orchestration"
        (self.root / "tickets" / f"{self.ticket_id}.md").write_text(
            TICKET_TEMPLATE.format(ticket_id=self.ticket_id, title=self.title, priority=1),
            encoding="utf-8",
        )
        self.ticket = Ticket(
            self.root / "tickets" / f"{self.ticket_id}.md", self.ticket_id,
            self.title, "READY", (), 1,
        )
        self.backend = InMemoryBackend((self.ticket,))
        self.url = "https://example.test/pull/10"

    def close(self) -> None:
        self.temporary.cleanup()

    @property
    def criteria(self) -> tuple[str, ...]:
        return ("Preserve the first criterion.", "Preserve the second criterion.")

    def attempt(self, attempt: int, status: AttemptStatus = AttemptStatus.REVIEW_READY) -> AttemptResult:
        passing = status == AttemptStatus.REVIEW_READY
        return AttemptResult(
            schema_version=1, ticket_id=self.ticket_id,
            branch="ticket/T-0010-fixture-orchestration", base_branch="main",
            attempt=attempt, status=status,
            commit_sha=f"sha-{attempt}" if passing else None,
            pr_reference=self.url if passing else None,
            tests=(TestEvidence(
                "python -m unittest", EvidenceStatus.PASS if passing else EvidenceStatus.FAIL,
                "passed" if passing else "failed",
            ),),
            acceptance_criteria=tuple(
                CriterionEvidence(
                    criterion, EvidenceStatus.PASS if passing else EvidenceStatus.NOT_EVALUATED,
                    "implemented" if passing else "", "tested" if passing else "",
                )
                for criterion in self.criteria
            ),
            failures=() if passing else (status.value.lower(),),
        )

    def evidence_for_attempt(self, result: AttemptResult) -> WorkflowEvidence:
        assert result.commit_sha is not None
        return WorkflowEvidence(
            worker_result=result,
            pull_request=PullRequestEvidence(
                10, self.url, self.ticket_id, result.branch, "main", result.commit_sha,
            ),
            pr_diff=f"diff for attempt {result.attempt}",
            ci_checks=(CheckResult("baseline", True, CheckConclusion.PASS, "passed"),),
            prior_review_comments=(PriorReviewComment("reviewer-old", "Earlier evidence."),),
            worker_active=False,
        )


class FakeWorkerDispatcher:
    def __init__(self, fixture: WorkflowFixture, statuses: tuple[AttemptStatus, ...]):
        self.fixture = fixture
        self.statuses = list(statuses)
        self.requests = []

    def dispatch(self, request):
        self.requests.append(request)
        result = self.fixture.attempt(request.attempt, self.statuses.pop(0))
        if result.status == AttemptStatus.REVIEW_READY:
            self.fixture.backend.set_evidence(
                self.fixture.ticket_id, self.fixture.evidence_for_attempt(result)
            )
        return result


class FakeReviewerDispatcher:
    def __init__(self, decisions: tuple[ReviewDecision, ...]):
        self.decisions = list(decisions)
        self.requests = []

    def dispatch(self, request):
        self.requests.append(request)
        decision = self.decisions.pop(0)
        context = request.context
        failed = decision == ReviewDecision.REQUEST_CHANGES
        findings = tuple(
            CriterionFinding(
                criterion,
                FindingStatus.UNSATISFIED if failed and index == 0 else FindingStatus.SATISFIED,
                "diff and CI evidence",
                "Add the missing deterministic edge-case test." if failed and index == 0 else "",
            )
            for index, criterion in enumerate(context.acceptance_criteria)
        )
        return ReviewResult(
            schema_version=1, ticket_id=context.ticket_id,
            pr_reference=context.pull_request.url,
            reviewed_commit_sha=context.pull_request.head_sha,
            reviewer_identity=context.reviewer_identity,
            implementation_worker_identity=context.implementation_worker_identity,
            decision=decision, criterion_findings=findings,
            ci_assessment="baseline: PASS",
            actionable_reasons=("Add the missing deterministic edge-case test.",) if failed else (),
            prior_comment_disposition=(), summary="Changes needed." if failed else "Approved.",
        )


class SelectionAndStateTests(unittest.TestCase):
    def test_readiness_requires_ready_done_dependencies_and_not_done(self) -> None:
        tickets = (
            ticket("T-0001", "DONE"),
            ticket("T-0010", "READY", ("T-0001",)),
            ticket("T-0011", "READY", ("T-0012",)),
            ticket("T-0012", "REVIEW"),
            ticket("T-0013", "DONE"),
        )
        self.assertEqual(select_ready_ticket(tickets).ticket_id, "T-0010")  # type: ignore[union-attr]
        self.assertIsNone(select_ready_ticket(tickets, frozenset({"T-0010"})))

    def test_selection_is_priority_then_ticket_id_regardless_of_input_order(self) -> None:
        tickets = (ticket("T-0012", priority=2), ticket("T-0011", priority=1), ticket("T-0010", priority=1))
        self.assertEqual(select_ready_ticket(tickets).ticket_id, "T-0010")  # type: ignore[union-attr]
        self.assertEqual(select_ready_ticket(tuple(reversed(tickets))).ticket_id, "T-0010")  # type: ignore[union-attr]

    def test_invalid_transition_is_rejected(self) -> None:
        fixture = WorkflowFixture()
        try:
            state = Orchestrator(fixture.root, fixture.backend, "worker", "reviewer").claim_next("run").state
            assert state is not None
            self.assertEqual(state.__class__.from_dict(state.to_dict()), state)
            with self.assertRaisesRegex(ValueError, "CLAIMED -> APPROVED"):
                state.transition(OrchestrationStage.APPROVED_AWAITING_MERGE, "skip")
        finally:
            fixture.close()

    def test_concurrent_claim_has_exactly_one_winner(self) -> None:
        fixture = WorkflowFixture()
        try:
            orchestrators = tuple(
                Orchestrator(fixture.root, fixture.backend, f"worker-{i}", f"reviewer-{i}")
                for i in range(2)
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(pool.map(lambda item: item[1].claim_next(f"run-{item[0]}"), enumerate(orchestrators)))
            self.assertEqual(sum(result.action == "CLAIMED" for result in results), 1)
            self.assertEqual(len(fixture.backend.claims), 1)
        finally:
            fixture.close()

    def test_self_review_configuration_is_rejected(self) -> None:
        fixture = WorkflowFixture()
        try:
            with self.assertRaisesRegex(ValueError, "identities must remain different"):
                Orchestrator(fixture.root, fixture.backend, "same", "same")
        finally:
            fixture.close()


class RecoveryAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkflowFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_restart_reconstructs_claim_without_duplicate_worker(self) -> None:
        first_dispatcher = FakeWorkerDispatcher(self.fixture, (AttemptStatus.REVIEW_READY,))
        first = Orchestrator(self.fixture.root, self.fixture.backend, "worker", "reviewer", first_dispatcher)
        claimed = first.claim_next("run-1").state
        assert claimed is not None
        running = claimed.transition(OrchestrationStage.WORKER_RUNNING, "worker accepted dispatch")
        self.fixture.backend.save_state(running, claimed.revision)
        result = self.fixture.attempt(1)
        self.fixture.backend.set_evidence(self.fixture.ticket_id, self.fixture.evidence_for_attempt(result))

        restarted = Orchestrator(self.fixture.root, self.fixture.backend, "worker", "reviewer", first_dispatcher)
        outcome = restarted.run_once("run-2")

        self.assertEqual(outcome.action, "RECONCILED")
        self.assertEqual(outcome.state.stage, OrchestrationStage.REVIEW_PENDING)  # type: ignore[union-attr]
        self.assertEqual(len(first_dispatcher.requests), 0)
        self.assertEqual(len(self.fixture.backend.claims), 1)

    def test_disappeared_worker_uses_bounded_infrastructure_path(self) -> None:
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer",
            retry_policy=RetryPolicy(max_worker_attempts=3, max_infrastructure_retries=1, max_reviewer_retries=2),
        )
        claimed = orchestrator.claim_next("run").state
        assert claimed is not None
        running = claimed.transition(OrchestrationStage.WORKER_RUNNING, "worker accepted dispatch")
        self.fixture.backend.save_state(running, claimed.revision)
        self.fixture.backend.set_evidence(self.fixture.ticket_id, WorkflowEvidence(worker_active=False))

        recovered = orchestrator.reconcile(running)
        self.assertEqual(recovered.stage, OrchestrationStage.BLOCKED)  # type: ignore[union-attr]
        self.assertEqual(recovered.infrastructure_retries, 1)  # type: ignore[union-attr]
        self.assertEqual(recovered.worker_attempts, 0)  # type: ignore[union-attr]

    def test_implementation_failures_stop_at_worker_budget(self) -> None:
        worker = FakeWorkerDispatcher(
            self.fixture, (AttemptStatus.IMPLEMENTATION_FAILED, AttemptStatus.IMPLEMENTATION_FAILED)
        )
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer", worker,
            retry_policy=RetryPolicy(max_worker_attempts=2, max_infrastructure_retries=2, max_reviewer_retries=2),
        )
        orchestrator.run_once("run")
        first = orchestrator.run_once("run")
        second = orchestrator.run_once("run")
        self.assertEqual(first.state.stage, OrchestrationStage.WORKER_FAILED)  # type: ignore[union-attr]
        self.assertEqual(second.state.stage, OrchestrationStage.BLOCKED)  # type: ignore[union-attr]
        self.assertEqual(second.state.worker_attempts, 2)  # type: ignore[union-attr]
        self.assertEqual(second.state.last_failure, FailureCategory.IMPLEMENTATION)  # type: ignore[union-attr]

    def test_infrastructure_failures_do_not_consume_worker_attempts(self) -> None:
        worker = FakeWorkerDispatcher(
            self.fixture, (AttemptStatus.INFRA_FAILURE, AttemptStatus.INFRA_FAILURE)
        )
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer", worker,
            retry_policy=RetryPolicy(max_worker_attempts=2, max_infrastructure_retries=2, max_reviewer_retries=2),
        )
        orchestrator.run_once("run")
        orchestrator.run_once("run")
        result = orchestrator.run_once("run")
        self.assertEqual(result.state.stage, OrchestrationStage.BLOCKED)  # type: ignore[union-attr]
        self.assertEqual(result.state.infrastructure_retries, 2)  # type: ignore[union-attr]
        self.assertEqual(result.state.worker_attempts, 0)  # type: ignore[union-attr]


    def test_restart_reconciles_persisted_reviewer_approval(self) -> None:
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer"
        )
        claimed = orchestrator.claim_next("run").state
        assert claimed is not None
        running = claimed.transition(OrchestrationStage.WORKER_RUNNING, "worker dispatched")
        self.fixture.backend.save_state(running, claimed.revision)
        attempt = self.fixture.attempt(1)
        review_pending = running.transition(
            OrchestrationStage.REVIEW_PENDING, "worker evidence persisted",
            worker_attempts=1, pr_reference=self.fixture.url,
            current_commit_sha="sha-1", latest_worker_status="REVIEW_READY",
        )
        self.fixture.backend.save_state(review_pending, running.revision)
        review_running = review_pending.transition(
            OrchestrationStage.REVIEW_RUNNING, "reviewer dispatched"
        )
        self.fixture.backend.save_state(review_running, review_pending.revision)
        evidence = self.fixture.evidence_for_attempt(attempt)
        approval = ReviewResult(
            schema_version=1, ticket_id=self.fixture.ticket_id,
            pr_reference=self.fixture.url, reviewed_commit_sha="sha-1",
            reviewer_identity="reviewer", implementation_worker_identity="worker",
            decision=ReviewDecision.APPROVE,
            criterion_findings=tuple(
                CriterionFinding(item, FindingStatus.SATISFIED, "diff and CI evidence")
                for item in self.fixture.criteria
            ),
            ci_assessment="baseline: PASS", actionable_reasons=(),
            prior_comment_disposition=(), summary="Approved.",
        )
        self.fixture.backend.set_evidence(
            self.fixture.ticket_id, replace(evidence, reviewer_result=approval)
        )

        restarted = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer"
        )
        recovered = restarted.reconcile(review_running)

        self.assertEqual(
            recovered.stage, OrchestrationStage.APPROVED_AWAITING_MERGE  # type: ignore[union-attr]
        )

    def test_reviewer_execution_failures_use_separate_bounded_budget(self) -> None:
        class BrokenReviewer:
            def dispatch(self, request):
                raise ValueError("review provider returned malformed evidence")

        worker = FakeWorkerDispatcher(self.fixture, (AttemptStatus.REVIEW_READY,))
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer",
            worker, BrokenReviewer(),
            RetryPolicy(
                max_worker_attempts=3,
                max_infrastructure_retries=2,
                max_reviewer_retries=2,
            ),
        )
        orchestrator.run_once("run")
        orchestrator.run_once("run")
        first = orchestrator.run_once("run")
        second = orchestrator.run_once("run")

        self.assertEqual(first.state.stage, OrchestrationStage.REVIEW_PENDING)  # type: ignore[union-attr]
        self.assertEqual(first.state.reviewer_retries, 1)  # type: ignore[union-attr]
        self.assertEqual(second.state.stage, OrchestrationStage.BLOCKED)  # type: ignore[union-attr]
        self.assertEqual(second.state.reviewer_retries, 2)  # type: ignore[union-attr]
        self.assertEqual(second.state.worker_attempts, 1)  # type: ignore[union-attr]
class EndToEndFakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = WorkflowFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_request_changes_then_fresh_worker_then_approval(self) -> None:
        worker = FakeWorkerDispatcher(
            self.fixture, (AttemptStatus.REVIEW_READY, AttemptStatus.REVIEW_READY)
        )
        reviewer = FakeReviewerDispatcher(
            (ReviewDecision.REQUEST_CHANGES, ReviewDecision.APPROVE)
        )
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker-a", "reviewer-b",
            worker, reviewer,
        )
        original_ticket = (self.fixture.root / "tickets" / "T-0010.md").read_text(encoding="utf-8")

        self.assertEqual(orchestrator.run_once("run").action, "CLAIMED")
        self.assertEqual(orchestrator.run_once("run").state.stage, OrchestrationStage.REVIEW_PENDING)  # type: ignore[union-attr]
        self.assertEqual(orchestrator.run_once("run").state.stage, OrchestrationStage.CHANGES_REQUESTED)  # type: ignore[union-attr]
        self.assertEqual(orchestrator.run_once("run").state.stage, OrchestrationStage.REVIEW_PENDING)  # type: ignore[union-attr]
        final = orchestrator.run_once("run").state

        assert final is not None
        self.assertEqual(final.stage, OrchestrationStage.APPROVED_AWAITING_MERGE)
        self.assertEqual(final.worker_attempts, 2)
        self.assertEqual(final.infrastructure_retries, 0)
        self.assertEqual(final.pr_reference, self.fixture.url)
        self.assertEqual(worker.requests[0].context.branch, worker.requests[1].context.branch)
        self.assertEqual(worker.requests[0].attempt, 1)
        self.assertEqual(worker.requests[1].attempt, 2)
        self.assertEqual(
            tuple(
                event.attempt for event in final.history
                if event.stage == OrchestrationStage.WORKER_RUNNING
            ),
            (1, 2),
        )
        self.assertEqual(
            worker.requests[1].prior_review_feedback,
            ("Add the missing deterministic edge-case test.",),
        )
        self.assertEqual(reviewer.requests[0].context.pull_request.number, 10)
        self.assertEqual(reviewer.requests[1].context.pull_request.number, 10)
        self.assertEqual(final.worker_identity, "worker-a")
        self.assertEqual(final.reviewer_identity, "reviewer-b")
        self.assertNotEqual(final.worker_identity, final.reviewer_identity)
        self.assertEqual(
            (self.fixture.root / "tickets" / "T-0010.md").read_text(encoding="utf-8"),
            original_ticket,
        )
        self.assertFalse(hasattr(orchestrator, "merge"))
        self.assertFalse(hasattr(worker, "provision"))

    def test_external_completion_reconciles_without_redispatch(self) -> None:
        worker = FakeWorkerDispatcher(self.fixture, (AttemptStatus.REVIEW_READY,))
        reviewer = FakeReviewerDispatcher((ReviewDecision.APPROVE,))
        orchestrator = Orchestrator(
            self.fixture.root, self.fixture.backend, "worker", "reviewer", worker, reviewer,
        )
        orchestrator.run_once("run")
        orchestrator.run_once("run")
        approved = orchestrator.run_once("run").state
        assert approved is not None
        evidence = self.fixture.backend.load_evidence(self.fixture.ticket_id)
        self.fixture.backend.set_evidence(
            self.fixture.ticket_id, replace(evidence, externally_completed=True)
        )

        completed = orchestrator.reconcile(approved)
        self.assertEqual(completed.stage, OrchestrationStage.COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(len(worker.requests), 1)
        self.assertEqual(len(reviewer.requests), 1)


if __name__ == "__main__":
    unittest.main()
