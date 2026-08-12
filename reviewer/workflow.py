"""Deterministic preparation, decision, and posting safeguards for reviewers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from worker.contract import AttemptResult, AttemptStatus, WorkerContext
from worker.repository import acceptance_criteria, canonical_branch, load_all_tickets, required_tests
from worker.workflow import WorkerWorkflow

from .contract import (
    CheckConclusion, CheckResult, CriterionFinding, FindingStatus, PriorReviewComment,
    PullRequestEvidence, ReviewDecision, ReviewerContext, ReviewResult,
)


class ReviewerState(str, Enum):
    READY = "READY"
    REVIEW_COMPLETE = "REVIEW_COMPLETE"
    REVIEW_CONTEXT_INVALID = "REVIEW_CONTEXT_INVALID"
    REVIEW_INFRA_FAILURE = "REVIEW_INFRA_FAILURE"
    REVIEW_EXECUTION_FAILED = "REVIEW_EXECUTION_FAILED"


@dataclass(frozen=True)
class ReviewPreparation:
    state: ReviewerState
    context: ReviewerContext | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "context": self.context.to_dict() if self.context else None,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ReviewExecution:
    state: ReviewerState
    result: ReviewResult | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "result": self.result.to_dict() if self.result else None,
            "reasons": list(self.reasons),
        }


class ReviewerWorkflow:
    REVIEWABLE_TICKET_STATUSES = {"IN_PROGRESS", "REVIEW"}

    def __init__(self, root: Path):
        self.root = root.resolve()

    def prepare(
        self,
        ticket_id: str,
        worker_attempt: AttemptResult | None,
        pull_request: PullRequestEvidence | None,
        pr_diff: str | None,
        ci_checks: tuple[CheckResult, ...] | None,
        prior_review_comments: tuple[PriorReviewComment, ...] | None,
        implementation_worker_identity: str | None,
        reviewer_identity: str | None,
    ) -> ReviewPreparation:
        try:
            tickets = load_all_tickets(self.root)
        except (OSError, UnicodeError, ValueError) as error:
            return ReviewPreparation(ReviewerState.REVIEW_CONTEXT_INVALID, reasons=(str(error),))
        document = tickets.get(ticket_id)
        if document is None:
            return ReviewPreparation(
                ReviewerState.REVIEW_CONTEXT_INVALID, reasons=(f"missing canonical ticket: {ticket_id}",)
            )

        criteria = acceptance_criteria(document)
        tests = required_tests(document)
        reasons: list[str] = []
        if document.metadata.status not in self.REVIEWABLE_TICKET_STATUSES:
            reasons.append(f"ticket {ticket_id} status {document.metadata.status} is not reviewable")
        if not criteria:
            reasons.append("canonical ticket has no acceptance criteria")
        if worker_attempt is None:
            reasons.append("missing worker attempt evidence")
        if pull_request is None:
            reasons.append("missing pull request metadata")
        if pr_diff is None or not pr_diff.strip():
            reasons.append("missing PR diff")
        if ci_checks is None or not ci_checks:
            reasons.append("missing CI/check context")
        if prior_review_comments is None:
            reasons.append("missing prior review comments context")
        worker_identity = (implementation_worker_identity or "").strip()
        reviewer = (reviewer_identity or "").strip()
        if not worker_identity or not reviewer:
            reasons.append("missing implementation worker or reviewer identity")
        elif worker_identity == reviewer:
            reasons.append("self-review is prohibited: reviewer identity matches implementation worker identity")

        expected_branch = canonical_branch(ticket_id, document.metadata.title)
        if worker_attempt is not None:
            if worker_attempt.ticket_id != ticket_id or worker_attempt.branch != expected_branch:
                reasons.append("worker attempt ticket/branch does not match canonical ticket")
            if worker_attempt.status != AttemptStatus.REVIEW_READY:
                reasons.append(f"worker attempt is {worker_attempt.status.value}, not REVIEW_READY")
        if pull_request is not None:
            if (
                pull_request.ticket_id != ticket_id
                or pull_request.head_branch != expected_branch
                or pull_request.base_branch != "main"
            ):
                reasons.append("PR ticket/branch/base does not match canonical ticket")
            if worker_attempt is not None:
                if worker_attempt.commit_sha != pull_request.head_sha:
                    reasons.append("worker attempt commit does not match PR head commit")
                if worker_attempt.pr_reference not in (pull_request.url, f"#{pull_request.number}"):
                    reasons.append("worker attempt PR reference does not match PR metadata")

        if not reasons and worker_attempt is not None:
            worker_context = WorkerContext(
                schema_version=1, ticket_id=ticket_id, title=document.metadata.title,
                goal=document.sections["Goal"], ticket_status=document.metadata.status,
                dependencies=document.metadata.dependencies,
                allowed_scope=document.sections["Allowed scope"], acceptance_criteria=criteria,
                required_tests=tests, out_of_scope=document.sections["Out of scope"],
                repository_rules=(self.root / "AGENTS.md").read_text(encoding="utf-8"),
                documentation_references=(), branch=expected_branch, base_branch="main",
                expected_pr_behavior="independent review required",
            )
            try:
                WorkerWorkflow(self.root).validate_result(worker_context, worker_attempt)
            except ValueError as error:
                reasons.append(f"invalid worker attempt evidence: {error}")
        if reasons:
            return ReviewPreparation(ReviewerState.REVIEW_CONTEXT_INVALID, reasons=tuple(reasons))

        assert worker_attempt is not None and pull_request is not None
        assert pr_diff is not None and ci_checks is not None and prior_review_comments is not None
        infra_checks = tuple(check.name for check in ci_checks if check.conclusion == CheckConclusion.INFRA_FAILURE)
        if infra_checks:
            return ReviewPreparation(
                ReviewerState.REVIEW_INFRA_FAILURE,
                reasons=(f"CI infrastructure failure reported by: {', '.join(infra_checks)}",),
            )
        context = ReviewerContext(
            schema_version=1, ticket_id=ticket_id, title=document.metadata.title,
            goal=document.sections["Goal"], allowed_scope=document.sections["Allowed scope"],
            acceptance_criteria=criteria, required_tests=tests,
            out_of_scope=document.sections["Out of scope"],
            repository_rules=(self.root / "AGENTS.md").read_text(encoding="utf-8"),
            architecture_references=("architecture/overview.md", "architecture/security.md"),
            worker_attempt=worker_attempt, pull_request=pull_request, pr_diff=pr_diff,
            ci_checks=ci_checks, prior_review_comments=prior_review_comments,
            implementation_worker_identity=worker_identity, reviewer_identity=reviewer,
        )
        return ReviewPreparation(ReviewerState.READY, context=context)

    def review(
        self,
        context: ReviewerContext,
        findings: tuple[CriterionFinding, ...],
        summary: str,
        prior_comment_disposition: tuple[str, ...] = (),
    ) -> ReviewExecution:
        errors: list[str] = []
        actual = tuple(finding.criterion for finding in findings)
        if actual != context.acceptance_criteria:
            errors.append("findings must preserve and evaluate every acceptance criterion in order")
        for finding in findings:
            if not finding.evidence.strip():
                errors.append(f"criterion finding lacks evidence: {finding.criterion}")
            if finding.status == FindingStatus.UNSATISFIED and not finding.actionable_reason.strip():
                errors.append(f"unsatisfied criterion lacks an actionable reason: {finding.criterion}")
        if not summary.strip():
            errors.append("review summary is required")
        if context.reviewer_identity == context.implementation_worker_identity:
            errors.append("self-review is prohibited")
        if errors:
            return ReviewExecution(ReviewerState.REVIEW_EXECUTION_FAILED, reasons=tuple(errors))

        failing_checks = tuple(
            check for check in context.ci_checks
            if check.required and check.conclusion == CheckConclusion.FAIL
        )
        unsatisfied = tuple(finding for finding in findings if finding.status == FindingStatus.UNSATISFIED)
        actionable = [finding.actionable_reason for finding in unsatisfied]
        actionable.extend(
            f"Required CI check '{check.name}' failed: {check.details}"
            for check in failing_checks
        )
        decision = (
            ReviewDecision.REQUEST_CHANGES if unsatisfied or failing_checks else ReviewDecision.APPROVE
        )
        result = ReviewResult(
            schema_version=1, ticket_id=context.ticket_id,
            pr_reference=context.pull_request.url,
            reviewed_commit_sha=context.pull_request.head_sha,
            reviewer_identity=context.reviewer_identity,
            implementation_worker_identity=context.implementation_worker_identity,
            decision=decision, criterion_findings=findings,
            ci_assessment=self._ci_assessment(context.ci_checks),
            actionable_reasons=tuple(actionable),
            prior_comment_disposition=prior_comment_disposition, summary=summary.strip(),
        )
        try:
            self.validate_for_posting(context, result)
        except ValueError as error:
            return ReviewExecution(ReviewerState.REVIEW_EXECUTION_FAILED, reasons=(str(error),))
        return ReviewExecution(ReviewerState.REVIEW_COMPLETE, result=result)

    @staticmethod
    def _ci_assessment(checks: tuple[CheckResult, ...]) -> str:
        parts = [f"{check.name}: {check.conclusion.value} ({check.details})" for check in checks]
        return "; ".join(parts)

    def validate_for_posting(self, context: ReviewerContext, result: ReviewResult) -> None:
        if result.schema_version != 1:
            raise ValueError("unsupported review result schema version")
        if result.ticket_id != context.ticket_id or result.pr_reference != context.pull_request.url:
            raise ValueError("review result ticket/PR does not match reviewer context")
        if result.reviewed_commit_sha != context.pull_request.head_sha:
            raise ValueError("reviewed commit is not the intended PR revision")
        if result.reviewer_identity != context.reviewer_identity:
            raise ValueError("reviewer identity does not match reviewer context")
        if result.implementation_worker_identity != context.implementation_worker_identity:
            raise ValueError("implementation worker identity does not match reviewer context")
        if result.reviewer_identity == result.implementation_worker_identity:
            raise ValueError("self-review is prohibited")
        if not isinstance(result.decision, ReviewDecision):
            raise ValueError("review decision is invalid")
        if tuple(item.criterion for item in result.criterion_findings) != context.acceptance_criteria:
            raise ValueError("review result does not preserve every acceptance criterion")
        unsatisfied = tuple(
            item for item in result.criterion_findings if item.status == FindingStatus.UNSATISFIED
        )
        failed_required_ci = any(
            check.required and check.conclusion != CheckConclusion.PASS for check in context.ci_checks
        )
        if result.decision == ReviewDecision.APPROVE and (unsatisfied or failed_required_ci):
            raise ValueError("APPROVE requires all criteria and required CI checks to be satisfied")
        if result.decision == ReviewDecision.REQUEST_CHANGES:
            if not result.actionable_reasons or any(not reason.strip() for reason in result.actionable_reasons):
                raise ValueError("REQUEST_CHANGES requires at least one precise actionable reason")
