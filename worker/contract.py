"""Machine-readable worker input and observable result contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class AttemptStatus(str, Enum):
    """Terminal, recoverable outcomes of one disposable worker attempt."""

    BLOCKED = "BLOCKED"
    IMPLEMENTATION_FAILED = "IMPLEMENTATION_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INFRA_FAILURE = "INFRA_FAILURE"
    REVIEW_READY = "REVIEW_READY"


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True)
class WorkerContext:
    schema_version: int
    ticket_id: str
    title: str
    goal: str
    ticket_status: str
    dependencies: tuple[str, ...]
    allowed_scope: str
    acceptance_criteria: tuple[str, ...]
    required_tests: tuple[str, ...]
    out_of_scope: str
    repository_rules: str
    documentation_references: tuple[str, ...]
    branch: str
    base_branch: str
    expected_pr_behavior: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TestEvidence:
    command: str
    status: EvidenceStatus
    summary: str


@dataclass(frozen=True)
class CriterionEvidence:
    criterion: str
    status: EvidenceStatus
    implementation_evidence: str = ""
    test_evidence: str = ""


@dataclass(frozen=True)
class AttemptResult:
    """Only audit evidence is recorded; private reasoning is deliberately absent."""

    schema_version: int
    ticket_id: str
    branch: str
    base_branch: str
    attempt: int
    status: AttemptStatus
    commit_sha: str | None = None
    pr_reference: str | None = None
    changed_files: tuple[str, ...] = ()
    change_summary: str = ""
    tests: tuple[TestEvidence, ...] = ()
    acceptance_criteria: tuple[CriterionEvidence, ...] = ()
    blockers: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    scope_deviations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        for test in value["tests"]:
            test["status"] = test["status"].value
        for criterion in value["acceptance_criteria"]:
            criterion["status"] = criterion["status"].value
        return value
