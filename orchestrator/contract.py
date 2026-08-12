"""Inspectable orchestration state persisted outside the disposable process."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any


class OrchestrationStage(str, Enum):
    READY = "READY"
    CLAIMED = "CLAIMED"
    WORKER_RUNNING = "WORKER_RUNNING"
    WORKER_FAILED = "WORKER_FAILED"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEW_RUNNING = "REVIEW_RUNNING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED_AWAITING_MERGE = "APPROVED_AWAITING_MERGE"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"


class FailureCategory(str, Enum):
    IMPLEMENTATION = "IMPLEMENTATION"
    VALIDATION = "VALIDATION"
    REVIEWER_EXECUTION = "REVIEWER_EXECUTION"
    INFRASTRUCTURE = "INFRASTRUCTURE"


LEGAL_TRANSITIONS: dict[OrchestrationStage, frozenset[OrchestrationStage]] = {
    OrchestrationStage.READY: frozenset({OrchestrationStage.CLAIMED, OrchestrationStage.COMPLETED}),
    OrchestrationStage.CLAIMED: frozenset({
        OrchestrationStage.WORKER_RUNNING, OrchestrationStage.BLOCKED,
        OrchestrationStage.COMPLETED,
    }),
    OrchestrationStage.WORKER_RUNNING: frozenset({
        OrchestrationStage.WORKER_FAILED, OrchestrationStage.REVIEW_PENDING,
        OrchestrationStage.BLOCKED, OrchestrationStage.COMPLETED,
    }),
    OrchestrationStage.WORKER_FAILED: frozenset({
        OrchestrationStage.WORKER_RUNNING, OrchestrationStage.BLOCKED,
        OrchestrationStage.COMPLETED,
    }),
    OrchestrationStage.REVIEW_PENDING: frozenset({
        OrchestrationStage.REVIEW_RUNNING, OrchestrationStage.BLOCKED,
        OrchestrationStage.REVIEW_PENDING, OrchestrationStage.COMPLETED,
    }),
    OrchestrationStage.REVIEW_RUNNING: frozenset({
        OrchestrationStage.REVIEW_PENDING, OrchestrationStage.CHANGES_REQUESTED,
        OrchestrationStage.APPROVED_AWAITING_MERGE, OrchestrationStage.BLOCKED,
        OrchestrationStage.COMPLETED,
    }),
    OrchestrationStage.CHANGES_REQUESTED: frozenset({
        OrchestrationStage.WORKER_RUNNING, OrchestrationStage.BLOCKED,
        OrchestrationStage.COMPLETED,
    }),
    OrchestrationStage.APPROVED_AWAITING_MERGE: frozenset({
        OrchestrationStage.COMPLETED, OrchestrationStage.BLOCKED,
    }),
    OrchestrationStage.BLOCKED: frozenset({OrchestrationStage.COMPLETED}),
    OrchestrationStage.COMPLETED: frozenset(),
}


@dataclass(frozen=True)
class RetryPolicy:
    max_worker_attempts: int = 3
    max_infrastructure_retries: int = 2
    max_reviewer_retries: int = 2

    def __post_init__(self) -> None:
        if min(
            self.max_worker_attempts,
            self.max_infrastructure_retries,
            self.max_reviewer_retries,
        ) < 1:
            raise ValueError("all retry limits must be positive and finite")


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    stage: OrchestrationStage
    reason: str
    actor: str
    attempt: int
    failure_category: FailureCategory | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["stage"] = self.stage.value
        value["failure_category"] = (
            self.failure_category.value if self.failure_category else None
        )
        return value


@dataclass(frozen=True)
class WorkflowState:
    schema_version: int
    revision: int
    ticket_id: str
    stage: OrchestrationStage
    canonical_branch: str
    claim_owner: str
    claim_token: str
    worker_attempts: int = 0
    active_attempt: int | None = None
    infrastructure_retries: int = 0
    reviewer_retries: int = 0
    worker_identity: str | None = None
    reviewer_identity: str | None = None
    pr_reference: str | None = None
    current_commit_sha: str | None = None
    latest_worker_status: str | None = None
    latest_reviewer_decision: str | None = None
    prior_requested_changes: tuple[str, ...] = ()
    last_failure: FailureCategory | None = None
    blocked_reason: str | None = None
    history: tuple[AuditEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported workflow state schema version")
        if self.revision < 0 or self.worker_attempts < 0:
            raise ValueError("state revisions and counters cannot be negative")
        if self.infrastructure_retries < 0 or self.reviewer_retries < 0:
            raise ValueError("retry counters cannot be negative")
        if self.worker_identity and self.worker_identity == self.reviewer_identity:
            raise ValueError("worker and reviewer identities must remain different")

    def transition(
        self,
        target: OrchestrationStage,
        reason: str,
        actor: str = "orchestrator",
        failure_category: FailureCategory | None = None,
        **changes: Any,
    ) -> WorkflowState:
        if target not in LEGAL_TRANSITIONS[self.stage]:
            raise ValueError(f"illegal orchestration transition: {self.stage.value} -> {target.value}")
        if not reason.strip():
            raise ValueError("transition reason is required for audit evidence")
        audit_attempt = changes.get("active_attempt", self.active_attempt)
        if audit_attempt is None:
            audit_attempt = changes.get("worker_attempts", self.worker_attempts)

        event = AuditEvent(
            sequence=len(self.history) + 1,
            stage=target,
            reason=reason.strip(),
            actor=actor.strip() or "orchestrator",
            attempt=int(audit_attempt),
            failure_category=failure_category,
        )
        return replace(
            self,
            revision=self.revision + 1,
            stage=target,
            history=self.history + (event,),
            last_failure=failure_category,
            **changes,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["stage"] = self.stage.value
        value["last_failure"] = self.last_failure.value if self.last_failure else None
        value["history"] = [event.to_dict() for event in self.history]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowState:
        events = tuple(
            AuditEvent(
                sequence=item["sequence"], stage=OrchestrationStage(item["stage"]),
                reason=item["reason"], actor=item["actor"], attempt=item["attempt"],
                failure_category=(
                    FailureCategory(item["failure_category"])
                    if item.get("failure_category") else None
                ),
            )
            for item in value.get("history", ())
        )
        return cls(
            schema_version=value["schema_version"], revision=value["revision"],
            ticket_id=value["ticket_id"], stage=OrchestrationStage(value["stage"]),
            canonical_branch=value["canonical_branch"], claim_owner=value["claim_owner"],
            active_attempt=value.get("active_attempt"),
            claim_token=value["claim_token"], worker_attempts=value.get("worker_attempts", 0),
            infrastructure_retries=value.get("infrastructure_retries", 0),
            reviewer_retries=value.get("reviewer_retries", 0),
            worker_identity=value.get("worker_identity"),
            reviewer_identity=value.get("reviewer_identity"),
            pr_reference=value.get("pr_reference"),
            current_commit_sha=value.get("current_commit_sha"),
            latest_worker_status=value.get("latest_worker_status"),
            latest_reviewer_decision=value.get("latest_reviewer_decision"),
            prior_requested_changes=tuple(value.get("prior_requested_changes", ())),
            last_failure=(FailureCategory(value["last_failure"]) if value.get("last_failure") else None),
            blocked_reason=value.get("blocked_reason"), history=events,
        )


def ticket_status_for(stage: OrchestrationStage) -> str:
    if stage == OrchestrationStage.READY:
        return "READY"
    if stage in {
        OrchestrationStage.CLAIMED, OrchestrationStage.WORKER_RUNNING,
        OrchestrationStage.WORKER_FAILED, OrchestrationStage.CHANGES_REQUESTED,
    }:
        return "IN_PROGRESS"
    if stage in {
        OrchestrationStage.REVIEW_PENDING, OrchestrationStage.REVIEW_RUNNING,
        OrchestrationStage.APPROVED_AWAITING_MERGE,
    }:
        return "REVIEW"
    if stage == OrchestrationStage.BLOCKED:
        return "BLOCKED"
    return "DONE"
