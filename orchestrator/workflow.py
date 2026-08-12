"""Run-once orchestration, deterministic selection, dispatch, and reconciliation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from reviewer.contract import CheckConclusion, ReviewDecision, ReviewerContext
from reviewer.workflow import ReviewerWorkflow
from validation.validate_repository import Ticket
from worker.contract import AttemptResult, AttemptStatus, WorkerContext
from worker.repository import (
    acceptance_criteria,
    canonical_branch,
    load_all_tickets,
    required_tests,
)
from worker.workflow import PreparationState, WorkerWorkflow

from .contract import (
    FailureCategory,
    OrchestrationStage,
    RetryPolicy,
    WorkflowState,
    ticket_status_for,
)
from .dispatch import (
    DispatchError,
    ReviewerDispatchRequest,
    ReviewerDispatcher,
    WorkerDispatchRequest,
    WorkerDispatcher,
)
from .persistence import ClaimOutcome, StateBackend, WorkflowEvidence


TERMINAL_STAGES = {
    OrchestrationStage.BLOCKED,
    OrchestrationStage.COMPLETED,
}


@dataclass(frozen=True)
class RunResult:
    action: str
    state: WorkflowState | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "state": self.state.to_dict() if self.state else None,
            "reasons": list(self.reasons),
        }


def readiness_reasons(ticket: Ticket, tickets: dict[str, Ticket]) -> tuple[str, ...]:
    reasons: list[str] = []
    if ticket.status != "READY":
        reasons.append(f"{ticket.ticket_id} status is {ticket.status}, not READY")
    for dependency_id in ticket.dependencies:
        dependency = tickets.get(dependency_id)
        if dependency is None:
            reasons.append(f"dependency does not exist: {dependency_id}")
        elif dependency.status != "DONE":
            reasons.append(f"dependency {dependency_id} is {dependency.status}, not DONE")
    return tuple(reasons)


def select_ready_ticket(
    tickets: tuple[Ticket, ...], claimed_ticket_ids: frozenset[str] = frozenset()
) -> Ticket | None:
    """Select by ascending priority then ticket ID, never enumeration order."""
    by_id = {ticket.ticket_id: ticket for ticket in tickets}
    eligible = (
        ticket for ticket in tickets
        if ticket.ticket_id not in claimed_ticket_ids and not readiness_reasons(ticket, by_id)
    )
    return next(iter(sorted(eligible, key=lambda ticket: (ticket.priority, ticket.ticket_id))), None)


class Orchestrator:
    def __init__(
        self,
        root: Path,
        backend: StateBackend,
        worker_identity: str,
        reviewer_identity: str,
        worker_dispatcher: WorkerDispatcher | None = None,
        reviewer_dispatcher: ReviewerDispatcher | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
    ):
        self.root = root.resolve()
        self.backend = backend
        self.worker_identity = worker_identity.strip()
        self.reviewer_identity = reviewer_identity.strip()
        self.worker_dispatcher = worker_dispatcher
        self.reviewer_dispatcher = reviewer_dispatcher
        self.retry_policy = retry_policy
        if not self.worker_identity or not self.reviewer_identity:
            raise ValueError("worker and reviewer identities are required")
        if self.worker_identity == self.reviewer_identity:
            raise ValueError("worker and reviewer identities must remain different")

    def list_ready(self) -> tuple[Ticket, ...]:
        tickets = self.backend.list_tickets()
        by_id = {ticket.ticket_id: ticket for ticket in tickets}
        return tuple(sorted(
            (
                ticket for ticket in tickets
                if not self.backend.is_claimed(ticket.ticket_id)
                and not readiness_reasons(ticket, by_id)
            ),
            key=lambda ticket: (ticket.priority, ticket.ticket_id),
        ))

    def claim_next(self, owner: str, token: str | None = None) -> RunResult:
        tickets = self.backend.list_tickets()
        selected = select_ready_ticket(
            tickets,
            frozenset(ticket.ticket_id for ticket in tickets if self.backend.is_claimed(ticket.ticket_id)),
        )
        if selected is None:
            return RunResult("NO_READY_TICKET")
        claim_token = token or uuid.uuid4().hex
        claim = self.backend.acquire_claim(selected.ticket_id, owner, claim_token)
        if claim.outcome != ClaimOutcome.ACQUIRED:
            return RunResult(
                "CLAIM_CONFLICT", reasons=(f"{selected.ticket_id} already claimed by {claim.owner}",)
            )
        initial = WorkflowState(
            schema_version=1, revision=0, ticket_id=selected.ticket_id,
            stage=OrchestrationStage.READY,
            canonical_branch=canonical_branch(selected.ticket_id, selected.title),
            claim_owner=owner, claim_token=claim_token,
            worker_identity=self.worker_identity, reviewer_identity=self.reviewer_identity,
        )
        claimed = initial.transition(
            OrchestrationStage.CLAIMED,
            f"selected by priority {selected.priority}, then ticket ID; atomic claim acquired",
            actor=owner,
        )
        self.backend.save_state(claimed, expected_revision=None)
        self.backend.set_ticket_status(selected.ticket_id, ticket_status_for(claimed.stage))
        return RunResult("CLAIMED", claimed)

    def run_once(self, owner: str) -> RunResult:
        tickets = self.backend.list_tickets()
        active = sorted(
            (
                state for ticket in tickets
                if (state := self.backend.load_state(ticket.ticket_id)) is not None
                and state.stage not in TERMINAL_STAGES
            ),
            key=lambda state: state.ticket_id,
        )
        if not active:
            return self.claim_next(owner)
        state = active[0]
        reconciled = self.reconcile(state)
        if reconciled is not None:
            return RunResult("RECONCILED", reconciled)
        if state.stage in {
            OrchestrationStage.CLAIMED,
            OrchestrationStage.WORKER_FAILED,
            OrchestrationStage.CHANGES_REQUESTED,
        }:
            if self.worker_dispatcher is None:
                return RunResult("WORKER_DISPATCH_REQUIRED", state)
            return RunResult("WORKER_DISPATCHED", self._dispatch_worker(state))
        if state.stage == OrchestrationStage.REVIEW_PENDING:
            if self.reviewer_dispatcher is None:
                return RunResult("REVIEWER_DISPATCH_REQUIRED", state)
            return RunResult("REVIEWER_DISPATCHED", self._dispatch_reviewer(state))
        return RunResult("WAITING_FOR_EVIDENCE", state)

    def reconcile(self, state: WorkflowState) -> WorkflowState | None:
        ticket = next(
            (item for item in self.backend.list_tickets() if item.ticket_id == state.ticket_id), None
        )
        evidence = self.backend.load_evidence(state.ticket_id)
        if (ticket is not None and ticket.status == "DONE") or evidence.externally_completed:
            if state.stage == OrchestrationStage.COMPLETED:
                return None
            return self._transition(state, OrchestrationStage.COMPLETED, "external merge/completion evidence observed")
        if state.stage == OrchestrationStage.WORKER_RUNNING:
            if evidence.worker_result is not None:
                preparation = WorkerWorkflow(self.root).prepare(state.ticket_id)
                if preparation.state != PreparationState.READY or preparation.context is None:
                    return self._worker_failure(
                        state, FailureCategory.VALIDATION,
                        "; ".join(preparation.reasons), consume_attempt=True,
                        reported_attempt=evidence.worker_result.attempt,
                    )
                try:
                    WorkerWorkflow(self.root).validate_result(preparation.context, evidence.worker_result)
                    if evidence.worker_result.attempt != (state.active_attempt or state.worker_attempts + 1):
                        raise ValueError("persisted worker result has an unexpected attempt number")
                except ValueError as error:
                    return self._worker_failure(
                        state, FailureCategory.VALIDATION, str(error), consume_attempt=True,
                        reported_attempt=evidence.worker_result.attempt,
                    )

                return self._handle_worker_result(state, evidence.worker_result)
            if evidence.worker_active is False:
                return self._worker_failure(
                    state, FailureCategory.INFRASTRUCTURE,
                    "worker process disappeared without a persisted result", consume_attempt=False,
                )
        if state.stage == OrchestrationStage.REVIEW_RUNNING:
            if evidence.reviewer_result is not None:
                try:
                    context = self._reviewer_context(state, evidence)
                    ReviewerWorkflow(self.root).validate_for_posting(
                        context, evidence.reviewer_result
                    )
                except DispatchError as error:
                    return self._review_failure(state, error.category, str(error))
                except (KeyError, OSError, ValueError) as error:
                    return self._review_failure(
                        state, FailureCategory.REVIEWER_EXECUTION, str(error)
                    )

                return self._handle_review_result(state, evidence.reviewer_result)
            if evidence.reviewer_active is False:
                return self._review_failure(
                    state, FailureCategory.INFRASTRUCTURE,
                    "reviewer process disappeared without a persisted result",
                )
        return None

    def _save(self, previous: WorkflowState, updated: WorkflowState) -> WorkflowState:
        self.backend.save_state(updated, expected_revision=previous.revision)
        self.backend.set_ticket_status(updated.ticket_id, ticket_status_for(updated.stage))
        return updated

    def _transition(
        self, state: WorkflowState, target: OrchestrationStage, reason: str,
        failure: FailureCategory | None = None, **changes: object,
    ) -> WorkflowState:
        return self._save(
            state,
            state.transition(target, reason, failure_category=failure, **changes),
        )

    def _dispatch_worker(self, state: WorkflowState) -> WorkflowState:
        preparation = WorkerWorkflow(self.root).prepare(state.ticket_id)
        if preparation.state != PreparationState.READY or preparation.context is None:
            return self._block(
                state, FailureCategory.VALIDATION,
                "; ".join(preparation.reasons) or "worker context could not be prepared",
            )
        next_attempt = state.worker_attempts + 1
        if next_attempt > self.retry_policy.max_worker_attempts:
            return self._block(state, FailureCategory.IMPLEMENTATION, "worker attempt budget exhausted")
        running = self._transition(
            state, OrchestrationStage.WORKER_RUNNING,
            f"dispatching worker attempt {next_attempt}",
            active_attempt=next_attempt,
        )
        request = WorkerDispatchRequest(
            context=preparation.context, attempt=next_attempt,
            worker_identity=self.worker_identity, claim_token=state.claim_token,
            prior_review_feedback=state.prior_requested_changes,
        )
        assert self.worker_dispatcher is not None
        try:
            result = self.worker_dispatcher.dispatch(request)
            WorkerWorkflow(self.root).validate_result(preparation.context, result)
            if result.attempt != next_attempt:
                raise ValueError("worker result attempt does not match dispatched attempt")
        except DispatchError as error:
            return self._worker_failure(
                running, error.category, str(error),
                consume_attempt=error.category != FailureCategory.INFRASTRUCTURE,
            )
        except ValueError as error:
            return self._worker_failure(
                running, FailureCategory.VALIDATION, str(error), consume_attempt=True,
            )
        self.backend.save_worker_result(state.ticket_id, result)
        return self._handle_worker_result(running, result)

    def _handle_worker_result(self, state: WorkflowState, result: AttemptResult) -> WorkflowState:
        if result.status == AttemptStatus.REVIEW_READY:
            return self._transition(
                state, OrchestrationStage.REVIEW_PENDING,
                f"worker attempt {result.attempt} produced review-ready evidence",
                worker_attempts=result.attempt, pr_reference=result.pr_reference,
                current_commit_sha=result.commit_sha, latest_worker_status=result.status.value,
                active_attempt=None,
            )
        categories = {
            AttemptStatus.IMPLEMENTATION_FAILED: FailureCategory.IMPLEMENTATION,
            AttemptStatus.VALIDATION_FAILED: FailureCategory.VALIDATION,
            AttemptStatus.INFRA_FAILURE: FailureCategory.INFRASTRUCTURE,
            AttemptStatus.BLOCKED: FailureCategory.IMPLEMENTATION,
        }
        category = categories[result.status]
        reason = "; ".join(result.failures + result.blockers) or result.status.value
        if result.status == AttemptStatus.BLOCKED:
            return self._block(state, category, reason, worker_attempts=result.attempt)
        return self._worker_failure(
            state, category, reason,
            consume_attempt=category != FailureCategory.INFRASTRUCTURE,
            reported_attempt=result.attempt,
        )

    def _worker_failure(
        self, state: WorkflowState, category: FailureCategory, reason: str,
        consume_attempt: bool, reported_attempt: int | None = None,
    ) -> WorkflowState:
        attempts = max(state.worker_attempts, reported_attempt or 0) if consume_attempt else state.worker_attempts
        infra = state.infrastructure_retries + (category == FailureCategory.INFRASTRUCTURE)
        exhausted = (
            attempts >= self.retry_policy.max_worker_attempts
            if category != FailureCategory.INFRASTRUCTURE
            else infra >= self.retry_policy.max_infrastructure_retries
        )
        if exhausted:
            return self._block(
                state, category, f"{reason}; retry budget exhausted",
                worker_attempts=attempts, infrastructure_retries=infra,
            )
        return self._transition(
            state, OrchestrationStage.WORKER_FAILED, reason, category,
            worker_attempts=attempts, infrastructure_retries=infra,
            latest_worker_status=category.value,
            active_attempt=None,
        )

    def _reviewer_context(self, state: WorkflowState, evidence: WorkflowEvidence) -> ReviewerContext:
        if evidence.worker_result is None or evidence.pull_request is None:
            raise ValueError("missing worker result or pull request evidence")
        if evidence.pr_diff is None or not evidence.pr_diff.strip():
            raise ValueError("missing PR diff evidence")
        if evidence.ci_checks is None or not evidence.ci_checks:
            raise ValueError("missing CI evidence")
        if evidence.prior_review_comments is None:
            raise ValueError("missing prior review comment evidence")
        if any(check.conclusion == CheckConclusion.INFRA_FAILURE for check in evidence.ci_checks):
            raise DispatchError(FailureCategory.INFRASTRUCTURE, "CI infrastructure failure")
        documents = load_all_tickets(self.root)
        document = documents[state.ticket_id]
        worker_context = self._worker_context(document, state)
        WorkerWorkflow(self.root).validate_result(worker_context, evidence.worker_result)
        pr = evidence.pull_request
        if pr.ticket_id != state.ticket_id or pr.head_branch != state.canonical_branch:
            raise ValueError("PR ticket/branch does not match orchestration state")
        if pr.base_branch != "main" or pr.head_sha != evidence.worker_result.commit_sha:
            raise ValueError("PR base or commit does not match worker evidence")
        if evidence.worker_result.pr_reference not in (pr.url, f"#{pr.number}"):
            raise ValueError("PR reference does not match worker evidence")
        return ReviewerContext(
            schema_version=1, ticket_id=state.ticket_id, title=document.metadata.title,
            goal=document.sections["Goal"], allowed_scope=document.sections["Allowed scope"],
            acceptance_criteria=acceptance_criteria(document),
            required_tests=required_tests(document), out_of_scope=document.sections["Out of scope"],
            repository_rules=(self.root / "AGENTS.md").read_text(encoding="utf-8"),
            architecture_references=("architecture/overview.md", "architecture/security.md"),
            worker_attempt=evidence.worker_result, pull_request=pr, pr_diff=evidence.pr_diff,
            ci_checks=evidence.ci_checks, prior_review_comments=evidence.prior_review_comments,
            implementation_worker_identity=self.worker_identity,
            reviewer_identity=self.reviewer_identity,
        )

    def _worker_context(self, document: object, state: WorkflowState) -> WorkerContext:
        metadata = document.metadata  # type: ignore[attr-defined]
        sections = document.sections  # type: ignore[attr-defined]
        return WorkerContext(
            schema_version=1, ticket_id=state.ticket_id, title=metadata.title,
            goal=sections["Goal"], ticket_status=metadata.status,
            dependencies=metadata.dependencies, allowed_scope=sections["Allowed scope"],
            acceptance_criteria=acceptance_criteria(document), required_tests=required_tests(document),
            out_of_scope=sections["Out of scope"],
            repository_rules=(self.root / "AGENTS.md").read_text(encoding="utf-8"),
            documentation_references=(), branch=state.canonical_branch, base_branch="main",
            expected_pr_behavior="reuse one canonical PR; never approve or merge",
        )

    def _dispatch_reviewer(self, state: WorkflowState) -> WorkflowState:
        evidence = self.backend.load_evidence(state.ticket_id)
        try:
            context = self._reviewer_context(state, evidence)
        except DispatchError as error:
            return self._review_failure(state, error.category, str(error))
        except (KeyError, OSError, ValueError) as error:
            return self._review_failure(state, FailureCategory.REVIEWER_EXECUTION, str(error))
        running = self._transition(
            state, OrchestrationStage.REVIEW_RUNNING,
            f"dispatching independent reviewer {self.reviewer_identity}",
        )
        assert self.reviewer_dispatcher is not None
        try:
            result = self.reviewer_dispatcher.dispatch(
                ReviewerDispatchRequest(context, self.reviewer_identity, state.claim_token)
            )
            ReviewerWorkflow(self.root).validate_for_posting(context, result)
        except DispatchError as error:
            return self._review_failure(running, error.category, str(error))
        except ValueError as error:
            return self._review_failure(running, FailureCategory.REVIEWER_EXECUTION, str(error))
        self.backend.save_reviewer_result(state.ticket_id, result)
        return self._handle_review_result(running, result)

    def _handle_review_result(self, state: WorkflowState, result: object) -> WorkflowState:
        decision = result.decision  # type: ignore[attr-defined]
        if result.reviewer_identity == result.implementation_worker_identity:  # type: ignore[attr-defined]
            return self._review_failure(state, FailureCategory.REVIEWER_EXECUTION, "self-review is prohibited")
        if result.reviewed_commit_sha != state.current_commit_sha:  # type: ignore[attr-defined]
            return self._review_failure(state, FailureCategory.REVIEWER_EXECUTION, "reviewed commit is stale")
        if decision == ReviewDecision.APPROVE:
            return self._transition(
                state, OrchestrationStage.APPROVED_AWAITING_MERGE,
                "independent reviewer approved current commit; human merge remains required",
                latest_reviewer_decision=decision.value,
            )
        if decision == ReviewDecision.REQUEST_CHANGES:
            feedback = tuple(result.actionable_reasons)  # type: ignore[attr-defined]
            return self._transition(
                state, OrchestrationStage.CHANGES_REQUESTED,
                "reviewer requested actionable changes on the canonical PR",
                latest_reviewer_decision=decision.value,
                prior_requested_changes=state.prior_requested_changes + feedback,
            )
        return self._review_failure(state, FailureCategory.REVIEWER_EXECUTION, "invalid reviewer decision")

    def _review_failure(
        self, state: WorkflowState, category: FailureCategory, reason: str,
    ) -> WorkflowState:
        infra = state.infrastructure_retries + (category == FailureCategory.INFRASTRUCTURE)
        reviewer_retries = state.reviewer_retries + (category == FailureCategory.REVIEWER_EXECUTION)
        exhausted = (
            infra >= self.retry_policy.max_infrastructure_retries
            if category == FailureCategory.INFRASTRUCTURE
            else reviewer_retries >= self.retry_policy.max_reviewer_retries
        )
        if exhausted:
            return self._block(
                state, category, f"{reason}; retry budget exhausted",
                infrastructure_retries=infra, reviewer_retries=reviewer_retries,
            )
        return self._transition(
            state, OrchestrationStage.REVIEW_PENDING, reason, category,
            infrastructure_retries=infra, reviewer_retries=reviewer_retries,
        )

    def _block(
        self, state: WorkflowState, category: FailureCategory, reason: str, **changes: object,
    ) -> WorkflowState:
        return self._transition(
            state, OrchestrationStage.BLOCKED, reason, category,
            blocked_reason=reason, active_attempt=None, **changes,
        )
