"""Machine-readable reviewer input and observable result contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from worker.contract import AttemptResult


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class FindingStatus(str, Enum):
    SATISFIED = "SATISFIED"
    UNSATISFIED = "UNSATISFIED"


class CheckConclusion(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INFRA_FAILURE = "INFRA_FAILURE"


@dataclass(frozen=True)
class PullRequestEvidence:
    number: int
    url: str
    ticket_id: str
    head_branch: str
    base_branch: str
    head_sha: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    required: bool
    conclusion: CheckConclusion
    details: str


@dataclass(frozen=True)
class PriorReviewComment:
    author: str
    body: str
    commit_sha: str | None = None
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ReviewerContext:
    """Deliberate review package reconstructed from repository and PR state."""

    schema_version: int
    ticket_id: str
    title: str
    goal: str
    allowed_scope: str
    acceptance_criteria: tuple[str, ...]
    required_tests: tuple[str, ...]
    out_of_scope: str
    repository_rules: str
    architecture_references: tuple[str, ...]
    worker_attempt: AttemptResult
    pull_request: PullRequestEvidence
    pr_diff: str
    ci_checks: tuple[CheckResult, ...]
    prior_review_comments: tuple[PriorReviewComment, ...]
    implementation_worker_identity: str
    reviewer_identity: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["worker_attempt"] = self.worker_attempt.to_dict()
        for check in value["ci_checks"]:
            check["conclusion"] = check["conclusion"].value
        return value


@dataclass(frozen=True)
class CriterionFinding:
    criterion: str
    status: FindingStatus
    evidence: str
    actionable_reason: str = ""


@dataclass(frozen=True)
class ReviewResult:
    """Observable findings and audit metadata; no private reasoning is stored."""

    schema_version: int
    ticket_id: str
    pr_reference: str
    reviewed_commit_sha: str
    reviewer_identity: str
    implementation_worker_identity: str
    decision: ReviewDecision
    criterion_findings: tuple[CriterionFinding, ...]
    ci_assessment: str
    actionable_reasons: tuple[str, ...]
    prior_comment_disposition: tuple[str, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["decision"] = self.decision.value
        for finding in value["criterion_findings"]:
            finding["status"] = finding["status"].value
        return value
