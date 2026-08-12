"""Durable state/evidence boundaries and deterministic local/fake implementations."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

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
from validation.validate_repository import Ticket, validate_repository
from worker.contract import (
    AttemptResult,
    AttemptStatus,
    CriterionEvidence,
    EvidenceStatus,
    TestEvidence,
)

from .contract import WorkflowState


class ClaimOutcome(str, Enum):
    ACQUIRED = "ACQUIRED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"


@dataclass(frozen=True)
class ClaimResult:
    outcome: ClaimOutcome
    owner: str
    token: str | None = None


@dataclass(frozen=True)
class WorkflowEvidence:
    worker_result: AttemptResult | None = None
    pull_request: PullRequestEvidence | None = None
    pr_diff: str | None = None
    ci_checks: tuple[CheckResult, ...] | None = None
    prior_review_comments: tuple[PriorReviewComment, ...] | None = None
    reviewer_result: ReviewResult | None = None
    worker_active: bool | None = None
    reviewer_active: bool | None = None
    externally_completed: bool = False


class StateBackend(Protocol):
    def list_tickets(self) -> tuple[Ticket, ...]: ...
    def is_claimed(self, ticket_id: str) -> bool: ...
    def acquire_claim(self, ticket_id: str, owner: str, token: str) -> ClaimResult: ...
    def load_state(self, ticket_id: str) -> WorkflowState | None: ...
    def save_state(self, state: WorkflowState, expected_revision: int | None) -> None: ...
    def set_ticket_status(self, ticket_id: str, status: str) -> None: ...
    def load_evidence(self, ticket_id: str) -> WorkflowEvidence: ...
    def save_worker_result(self, ticket_id: str, result: AttemptResult) -> None: ...
    def save_reviewer_result(self, ticket_id: str, result: ReviewResult) -> None: ...


class InMemoryBackend:
    """Thread-safe compare-and-set fake that accurately models competing claims."""

    def __init__(self, tickets: tuple[Ticket, ...]):
        self.tickets = tickets
        self.states: dict[str, WorkflowState] = {}
        self.claims: dict[str, tuple[str, str]] = {}
        self.evidence: dict[str, WorkflowEvidence] = {}
        self._lock = threading.Lock()

    def list_tickets(self) -> tuple[Ticket, ...]:
        return self.tickets

    def is_claimed(self, ticket_id: str) -> bool:
        with self._lock:
            return ticket_id in self.claims

    def acquire_claim(self, ticket_id: str, owner: str, token: str) -> ClaimResult:
        with self._lock:
            existing = self.claims.get(ticket_id)
            if existing:
                return ClaimResult(ClaimOutcome.ALREADY_CLAIMED, existing[0], existing[1])
            self.claims[ticket_id] = (owner, token)
            return ClaimResult(ClaimOutcome.ACQUIRED, owner, token)

    def load_state(self, ticket_id: str) -> WorkflowState | None:
        with self._lock:
            return self.states.get(ticket_id)

    def save_state(self, state: WorkflowState, expected_revision: int | None) -> None:
        with self._lock:
            current = self.states.get(state.ticket_id)
            actual = current.revision if current else None
            if actual != expected_revision:
                raise RuntimeError(
                    f"workflow state compare-and-set failed: expected {expected_revision}, found {actual}"
                )
            self.states[state.ticket_id] = state

    def set_ticket_status(self, ticket_id: str, status: str) -> None:
        with self._lock:
            self.tickets = tuple(
                replace(ticket, status=status) if ticket.ticket_id == ticket_id else ticket
                for ticket in self.tickets
            )

    def load_evidence(self, ticket_id: str) -> WorkflowEvidence:
        with self._lock:
            return self.evidence.get(ticket_id, WorkflowEvidence())

    def set_evidence(self, ticket_id: str, evidence: WorkflowEvidence) -> None:
        with self._lock:
            self.evidence[ticket_id] = evidence

    def save_worker_result(self, ticket_id: str, result: AttemptResult) -> None:
        with self._lock:
            current = self.evidence.get(ticket_id, WorkflowEvidence())
            self.evidence[ticket_id] = replace(current, worker_result=result, worker_active=False)

    def save_reviewer_result(self, ticket_id: str, result: ReviewResult) -> None:
        with self._lock:
            current = self.evidence.get(ticket_id, WorkflowEvidence())
            self.evidence[ticket_id] = replace(current, reviewer_result=result, reviewer_active=False)


class RepositoryStateBackend:
    """Repository file backend with atomic local claims and state CAS.

    A GitHub adapter can implement the same boundary with issue/PR compare-and-set.
    This backend is meaningful for one shared checkout and persists across process restarts.
    """

    def __init__(self, root: Path, state_directory: Path | None = None):
        self.root = root.resolve()
        self.directory = (state_directory or self.root / ".orchestration").resolve()
        self.claim_directory = self.directory / "claims"
        self.state_directory = self.directory / "states"
        self.evidence_directory = self.directory / "evidence"

    def _ensure_directories(self) -> None:
        self.claim_directory.mkdir(parents=True, exist_ok=True)
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.evidence_directory.mkdir(parents=True, exist_ok=True)

    def list_tickets(self) -> tuple[Ticket, ...]:
        return tuple(validate_repository(self.root))

    def _claim_path(self, ticket_id: str) -> Path:
        return self.claim_directory / f"{ticket_id}.json"

    def _state_path(self, ticket_id: str) -> Path:
        return self.state_directory / f"{ticket_id}.json"

    def is_claimed(self, ticket_id: str) -> bool:
        return self._claim_path(ticket_id).exists()

    def acquire_claim(self, ticket_id: str, owner: str, token: str) -> ClaimResult:
        self._ensure_directories()
        path = self._claim_path(ticket_id)
        payload = json.dumps({"ticket_id": ticket_id, "owner": owner, "token": token}, sort_keys=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            value = json.loads(path.read_text(encoding="utf-8"))
            return ClaimResult(ClaimOutcome.ALREADY_CLAIMED, value["owner"], value.get("token"))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
        return ClaimResult(ClaimOutcome.ACQUIRED, owner, token)

    def load_state(self, ticket_id: str) -> WorkflowState | None:
        path = self._state_path(ticket_id)
        if not path.exists():
            return None
        return WorkflowState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _atomic_json(self, path: Path, value: dict[str, object]) -> None:
        self._ensure_directories()
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def save_state(self, state: WorkflowState, expected_revision: int | None) -> None:
        current = self.load_state(state.ticket_id)
        actual = current.revision if current else None
        if actual != expected_revision:
            raise RuntimeError(
                f"workflow state compare-and-set failed: expected {expected_revision}, found {actual}"
            )
        self._atomic_json(self._state_path(state.ticket_id), state.to_dict())

    def set_ticket_status(self, ticket_id: str, status: str) -> None:
        import re
        path = self.root / "tickets" / f"{ticket_id}.md"
        text = path.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^status:\s*[A-Z_]+\s*$", f"status: {status}", text, count=1
        )
        if count != 1:
            raise RuntimeError(f"could not update canonical status for {ticket_id}")
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(updated, encoding="utf-8", newline="\n")
        os.replace(temporary, path)

    def _evidence_path(self, ticket_id: str) -> Path:
        return self.evidence_directory / f"{ticket_id}.json"

    def load_evidence(self, ticket_id: str) -> WorkflowEvidence:
        path = self._evidence_path(ticket_id)
        if not path.exists():
            return WorkflowEvidence()
        value = json.loads(path.read_text(encoding="utf-8"))
        return _evidence_from_dict(value)

    def _save_evidence(self, ticket_id: str, evidence: WorkflowEvidence) -> None:
        self._atomic_json(self._evidence_path(ticket_id), _evidence_to_dict(evidence))

    def save_worker_result(self, ticket_id: str, result: AttemptResult) -> None:
        current = self.load_evidence(ticket_id)
        self._save_evidence(ticket_id, replace(current, worker_result=result, worker_active=False))

    def save_reviewer_result(self, ticket_id: str, result: ReviewResult) -> None:
        current = self.load_evidence(ticket_id)
        self._save_evidence(ticket_id, replace(current, reviewer_result=result, reviewer_active=False))


def _attempt_from_dict(value: dict[str, object] | None) -> AttemptResult | None:
    if value is None:
        return None
    tests = value.get("tests", ())
    criteria = value.get("acceptance_criteria", ())
    assert isinstance(tests, list) and isinstance(criteria, list)
    return AttemptResult(
        schema_version=int(value["schema_version"]), ticket_id=str(value["ticket_id"]),
        branch=str(value["branch"]), base_branch=str(value["base_branch"]),
        attempt=int(value["attempt"]), status=AttemptStatus(str(value["status"])),
        commit_sha=value.get("commit_sha") if isinstance(value.get("commit_sha"), str) else None,
        pr_reference=value.get("pr_reference") if isinstance(value.get("pr_reference"), str) else None,
        changed_files=tuple(str(item) for item in value.get("changed_files", ())),
        change_summary=str(value.get("change_summary", "")),
        tests=tuple(TestEvidence(str(item["command"]), EvidenceStatus(str(item["status"])), str(item["summary"])) for item in tests),
        acceptance_criteria=tuple(
            CriterionEvidence(str(item["criterion"]), EvidenceStatus(str(item["status"])), str(item.get("implementation_evidence", "")), str(item.get("test_evidence", "")))
            for item in criteria
        ),
        blockers=tuple(str(item) for item in value.get("blockers", ())),
        failures=tuple(str(item) for item in value.get("failures", ())),
        scope_deviations=tuple(str(item) for item in value.get("scope_deviations", ())),
    )


def _review_from_dict(value: dict[str, object] | None) -> ReviewResult | None:
    if value is None:
        return None
    findings = value.get("criterion_findings", ())
    assert isinstance(findings, list)
    return ReviewResult(
        schema_version=int(value["schema_version"]), ticket_id=str(value["ticket_id"]),
        pr_reference=str(value["pr_reference"]), reviewed_commit_sha=str(value["reviewed_commit_sha"]),
        reviewer_identity=str(value["reviewer_identity"]),
        implementation_worker_identity=str(value["implementation_worker_identity"]),
        decision=ReviewDecision(str(value["decision"])),
        criterion_findings=tuple(
            CriterionFinding(str(item["criterion"]), FindingStatus(str(item["status"])), str(item["evidence"]), str(item.get("actionable_reason", "")))
            for item in findings
        ),
        ci_assessment=str(value.get("ci_assessment", "")),
        actionable_reasons=tuple(str(item) for item in value.get("actionable_reasons", ())),
        prior_comment_disposition=tuple(str(item) for item in value.get("prior_comment_disposition", ())),
        summary=str(value.get("summary", "")),
    )


def _evidence_to_dict(evidence: WorkflowEvidence) -> dict[str, object]:
    return {
        "worker_result": evidence.worker_result.to_dict() if evidence.worker_result else None,
        "pull_request": evidence.pull_request.__dict__ if evidence.pull_request else None,
        "pr_diff": evidence.pr_diff,
        "ci_checks": [
            {"name": item.name, "required": item.required, "conclusion": item.conclusion.value, "details": item.details}
            for item in evidence.ci_checks or ()
        ] if evidence.ci_checks is not None else None,
        "prior_review_comments": [item.__dict__ for item in evidence.prior_review_comments or ()]
        if evidence.prior_review_comments is not None else None,
        "reviewer_result": evidence.reviewer_result.to_dict() if evidence.reviewer_result else None,
        "worker_active": evidence.worker_active,
        "reviewer_active": evidence.reviewer_active,
        "externally_completed": evidence.externally_completed,
    }


def _evidence_from_dict(value: dict[str, object]) -> WorkflowEvidence:
    pr_value = value.get("pull_request")
    checks_value = value.get("ci_checks")
    comments_value = value.get("prior_review_comments")
    return WorkflowEvidence(
        worker_result=_attempt_from_dict(value.get("worker_result")),  # type: ignore[arg-type]
        pull_request=PullRequestEvidence(**pr_value) if isinstance(pr_value, dict) else None,
        pr_diff=value.get("pr_diff") if isinstance(value.get("pr_diff"), str) else None,
        ci_checks=None if checks_value is None else tuple(
            CheckResult(str(item["name"]), bool(item["required"]), CheckConclusion(str(item["conclusion"])), str(item["details"]))
            for item in checks_value  # type: ignore[union-attr]
        ),
        prior_review_comments=None if comments_value is None else tuple(
            PriorReviewComment(**item) for item in comments_value  # type: ignore[arg-type,union-attr]
        ),
        reviewer_result=_review_from_dict(value.get("reviewer_result")),  # type: ignore[arg-type]
        worker_active=value.get("worker_active") if isinstance(value.get("worker_active"), bool) else None,
        reviewer_active=value.get("reviewer_active") if isinstance(value.get("reviewer_active"), bool) else None,
        externally_completed=bool(value.get("externally_completed", False)),
    )
