"""Deterministic preparation and evidence validation for disposable workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .contract import AttemptResult, AttemptStatus, EvidenceStatus, WorkerContext
from .repository import acceptance_criteria, canonical_branch, load_all_tickets, require_ticket_id, required_tests


class PreparationState(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


@dataclass(frozen=True)
class Preparation:
    state: PreparationState
    context: WorkerContext | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "context": self.context.to_dict() if self.context else None,
            "reasons": list(self.reasons),
        }


class WorkerWorkflow:
    RUNNABLE_STATUSES = {"READY", "IN_PROGRESS"}

    def __init__(self, root: Path):
        self.root = root.resolve()

    def prepare(self, ticket_id: str) -> Preparation:
        try:
            require_ticket_id(ticket_id)
            tickets = load_all_tickets(self.root)
        except (OSError, UnicodeError, ValueError) as error:
            return Preparation(PreparationState.VALIDATION_FAILED, reasons=(str(error),))
        if ticket_id not in tickets:
            return Preparation(PreparationState.VALIDATION_FAILED, reasons=(f"unknown ticket: {ticket_id}",))

        document = tickets[ticket_id]
        ticket = document.metadata
        reasons: list[str] = []
        if ticket.status == "DONE":
            reasons.append(f"{ticket_id} is already DONE")
        elif ticket.status not in self.RUNNABLE_STATUSES:
            reasons.append(f"{ticket_id} status is {ticket.status}, not READY or IN_PROGRESS")
        for dependency_id in ticket.dependencies:
            dependency = tickets.get(dependency_id)
            if dependency is None:
                reasons.append(f"dependency does not exist: {dependency_id}")
            elif dependency.metadata.status != "DONE":
                reasons.append(f"dependency {dependency_id} is {dependency.metadata.status}, not DONE")
        if reasons:
            return Preparation(PreparationState.BLOCKED, reasons=tuple(reasons))

        criteria = acceptance_criteria(document)
        tests = required_tests(document)
        if not criteria or not tests:
            missing = "acceptance criteria" if not criteria else "required tests"
            return Preparation(
                PreparationState.VALIDATION_FAILED,
                reasons=(f"{ticket_id} has no parseable {missing}",),
            )

        context = WorkerContext(
            schema_version=1, ticket_id=ticket.ticket_id, title=ticket.title,
            goal=document.sections["Goal"], ticket_status=ticket.status,
            dependencies=ticket.dependencies, allowed_scope=document.sections["Allowed scope"],
            acceptance_criteria=criteria, required_tests=tests,
            out_of_scope=document.sections["Out of scope"],
            repository_rules=(self.root / "AGENTS.md").read_text(encoding="utf-8"),
            documentation_references=(
                "PLAN.md", "tickets/README.md", f"tickets/{ticket.ticket_id}.md",
                "architecture/overview.md", "architecture/security.md",
            ),
            branch=canonical_branch(ticket.ticket_id, ticket.title), base_branch="main",
            expected_pr_behavior=(
                "Create or reuse exactly one pull request from the canonical ticket branch to main; "
                "never approve or merge it from the worker workflow."
            ),
        )
        return Preparation(PreparationState.READY, context=context)

    def validate_result(self, context: WorkerContext, result: AttemptResult) -> None:
        if result.schema_version != 1:
            raise ValueError("unsupported attempt result schema version")
        if result.ticket_id != context.ticket_id or result.branch != context.branch:
            raise ValueError("result ticket/branch does not match the assigned worker context")
        if result.base_branch != context.base_branch or result.attempt < 1:
            raise ValueError("result base branch or attempt number is invalid")
        expected = context.acceptance_criteria
        actual = tuple(item.criterion for item in result.acceptance_criteria)
        if actual != expected:
            raise ValueError("result must preserve and address every acceptance criterion in order")
        if result.status == AttemptStatus.REVIEW_READY:
            if any(item.status != EvidenceStatus.PASS for item in result.acceptance_criteria):
                raise ValueError("REVIEW_READY requires PASS evidence for every acceptance criterion")
            if not result.tests or any(test.status != EvidenceStatus.PASS for test in result.tests):
                raise ValueError("REVIEW_READY requires passing test evidence")
        elif not (result.blockers or result.failures):
            raise ValueError("a non-success result requires an observable blocker or failure")
