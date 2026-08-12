from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reviewer.contract import (
    CheckConclusion, CheckResult, CriterionFinding, FindingStatus, PriorReviewComment,
    PullRequestEvidence, ReviewDecision,
)
from reviewer.github import GitHubCli
from reviewer.workflow import ReviewerState, ReviewerWorkflow
from worker.contract import (
    AttemptResult, AttemptStatus, CriterionEvidence, EvidenceStatus, TestEvidence,
)


TICKET = """---
id: T-0004
title: Define reviewer-agent workflow
status: REVIEW
dependencies: []
priority: 1
---

# T-0004 - Define reviewer-agent workflow

## Goal
Implement independent review.
## Context
Fixture context.
## Dependencies
None.
## Allowed scope
Reviewer workflow only.
## Acceptance criteria
- Reviewer input is complete.
- Self-review is prevented.
## Required tests
- `python -m unittest`
## Out of scope
Merging.
## Implementation notes
Fixture.
"""


class ReviewerFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "tickets").mkdir()
        (self.root / "tickets" / "T-0004.md").write_text(TICKET, encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "Review unchanged criteria. Never modify or merge implementation.\n", encoding="utf-8"
        )
        self.workflow = ReviewerWorkflow(self.root)
        self.criteria = ("Reviewer input is complete.", "Self-review is prevented.")
        self.url = "https://example.test/pull/4"
        self.attempt = AttemptResult(
            schema_version=1, ticket_id="T-0004",
            branch="ticket/T-0004-define-reviewer-agent-workflow", base_branch="main",
            attempt=1, status=AttemptStatus.REVIEW_READY, commit_sha="abc123",
            pr_reference=self.url, changed_files=("reviewer/workflow.py",),
            change_summary="Implemented review workflow.",
            tests=(TestEvidence("python -m unittest", EvidenceStatus.PASS, "all passed"),),
            acceptance_criteria=tuple(
                CriterionEvidence(item, EvidenceStatus.PASS, "implemented", "tested")
                for item in self.criteria
            ),
        )
        self.pr = PullRequestEvidence(
            number=4, url=self.url, ticket_id="T-0004",
            head_branch="ticket/T-0004-define-reviewer-agent-workflow",
            base_branch="main", head_sha="abc123",
        )
        self.checks = (CheckResult("baseline", True, CheckConclusion.PASS, "passed"),)
        self.comments = (
            PriorReviewComment("reviewer-old", "Please cover identity checks.", "oldsha"),
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def prepare(self, **overrides: object):
        values = {
            "ticket_id": "T-0004",
            "worker_attempt": self.attempt,
            "pull_request": self.pr,
            "pr_diff": "diff --git a/reviewer/workflow.py b/reviewer/workflow.py\n+review",
            "ci_checks": self.checks,
            "prior_review_comments": self.comments,
            "implementation_worker_identity": "worker-a",
            "reviewer_identity": "reviewer-b",
        }
        values.update(overrides)
        return self.workflow.prepare(**values)

    def findings(self, failed: int | None = None) -> tuple[CriterionFinding, ...]:
        return tuple(
            CriterionFinding(
                criterion, FindingStatus.UNSATISFIED if index == failed else FindingStatus.SATISFIED,
                "observable diff and test evidence",
                "Add a deterministic identity comparison before review." if index == failed else "",
            )
            for index, criterion in enumerate(self.criteria)
        )


class ReviewerWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReviewerFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_approval_evaluates_every_criterion_and_is_valid_for_posting(self) -> None:
        before = tuple(self.fixture.root.rglob("*"))
        preparation = self.fixture.prepare()
        self.assertEqual(preparation.state, ReviewerState.READY)
        assert preparation.context is not None

        execution = self.fixture.workflow.review(
            preparation.context, self.fixture.findings(), "All ticket criteria are satisfied.",
            ("Earlier identity request is addressed by the explicit comparison.",),
        )

        self.assertEqual(execution.state, ReviewerState.REVIEW_COMPLETE)
        assert execution.result is not None
        self.assertEqual(execution.result.decision, ReviewDecision.APPROVE)
        self.assertEqual(len(execution.result.criterion_findings), len(self.fixture.criteria))
        self.fixture.workflow.validate_for_posting(preparation.context, execution.result)
        self.assertEqual(tuple(self.fixture.root.rglob("*")), before)

    def test_unsatisfied_criterion_requests_precise_changes(self) -> None:
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        execution = self.fixture.workflow.review(
            preparation.context, self.fixture.findings(failed=1), "Identity control is incomplete."
        )
        assert execution.result is not None
        self.assertEqual(execution.result.decision, ReviewDecision.REQUEST_CHANGES)
        self.assertEqual(execution.result.criterion_findings[1].criterion, self.fixture.criteria[1])
        self.assertIn("identity comparison", execution.result.actionable_reasons[0])

    def test_missing_context_stops_before_review_or_post(self) -> None:
        preparation = self.fixture.prepare(pr_diff=None)
        self.assertEqual(preparation.state, ReviewerState.REVIEW_CONTEXT_INVALID)
        self.assertIsNone(preparation.context)
        self.assertIn("missing PR diff", preparation.reasons)

    def test_failing_required_ci_requests_changes_and_names_check(self) -> None:
        checks = (CheckResult("baseline", True, CheckConclusion.FAIL, "unit test failed"),)
        preparation = self.fixture.prepare(ci_checks=checks)
        self.assertEqual(preparation.state, ReviewerState.READY)
        assert preparation.context is not None
        execution = self.fixture.workflow.review(
            preparation.context, self.fixture.findings(), "Code evidence is otherwise complete."
        )
        assert execution.result is not None
        self.assertEqual(execution.result.decision, ReviewDecision.REQUEST_CHANGES)
        self.assertEqual(
            execution.result.actionable_reasons,
            ("Required CI check 'baseline' failed: unit test failed",),
        )

    def test_self_review_is_rejected_before_decision_or_post(self) -> None:
        preparation = self.fixture.prepare(
            implementation_worker_identity="same", reviewer_identity="same"
        )
        self.assertEqual(preparation.state, ReviewerState.REVIEW_CONTEXT_INVALID)
        self.assertIsNone(preparation.context)
        self.assertIn("self-review is prohibited", preparation.reasons[0])

    def test_mismatched_ticket_branch_and_commit_are_rejected(self) -> None:
        wrong_attempt = replace(self.fixture.attempt, ticket_id="T-0003")
        preparation = self.fixture.prepare(worker_attempt=wrong_attempt)
        self.assertEqual(preparation.state, ReviewerState.REVIEW_CONTEXT_INVALID)
        self.assertTrue(any("ticket/branch" in reason for reason in preparation.reasons))

        wrong_pr = replace(self.fixture.pr, head_sha="different")
        preparation = self.fixture.prepare(pull_request=wrong_pr)
        self.assertTrue(any("commit" in reason for reason in preparation.reasons))

    def test_acceptance_criteria_and_prior_comments_are_preserved(self) -> None:
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        self.assertEqual(preparation.context.acceptance_criteria, self.fixture.criteria)
        self.assertEqual(preparation.context.prior_review_comments, self.fixture.comments)
        self.assertIn("Never modify", preparation.context.repository_rules)
        serialized = preparation.context.to_dict()
        self.assertEqual(serialized["acceptance_criteria"], self.fixture.criteria)

    def test_missing_or_reordered_criterion_is_execution_failure(self) -> None:
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        execution = self.fixture.workflow.review(
            preparation.context, tuple(reversed(self.fixture.findings())), "Looks good."
        )
        self.assertEqual(execution.state, ReviewerState.REVIEW_EXECUTION_FAILED)
        self.assertIsNone(execution.result)

    def test_unsatisfied_finding_requires_actionable_reason(self) -> None:
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        findings = replace(self.fixture.findings()[0], status=FindingStatus.UNSATISFIED)
        execution = self.fixture.workflow.review(
            preparation.context, (findings, self.fixture.findings()[1]), "Needs work."
        )
        self.assertEqual(execution.state, ReviewerState.REVIEW_EXECUTION_FAILED)
        self.assertIn("actionable reason", execution.reasons[0])

    def test_posting_rejects_malformed_decision_and_inconsistent_approval(self) -> None:
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        approved = self.fixture.workflow.review(
            preparation.context, self.fixture.findings(), "Approved."
        ).result
        assert approved is not None

        with self.assertRaisesRegex(ValueError, "decision is invalid"):
            self.fixture.workflow.validate_for_posting(
                preparation.context, replace(approved, decision="MAYBE")  # type: ignore[arg-type]
            )
        failed_finding = replace(
            approved.criterion_findings[0], status=FindingStatus.UNSATISFIED,
            actionable_reason="Fix the missing evidence.",
        )
        with self.assertRaisesRegex(ValueError, "APPROVE requires"):
            self.fixture.workflow.validate_for_posting(
                preparation.context,
                replace(approved, criterion_findings=(failed_finding, approved.criterion_findings[1])),
            )

    def test_request_changes_without_reason_is_rejected_for_posting(self) -> None:
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        execution = self.fixture.workflow.review(
            preparation.context, self.fixture.findings(failed=0), "Needs changes."
        )
        assert execution.result is not None
        with self.assertRaisesRegex(ValueError, "actionable reason"):
            self.fixture.workflow.validate_for_posting(
                preparation.context, replace(execution.result, actionable_reasons=())
            )

    def test_infrastructure_failure_is_distinct_from_context_and_review_failure(self) -> None:
        checks = (CheckResult("runner", True, CheckConclusion.INFRA_FAILURE, "runner unavailable"),)
        preparation = self.fixture.prepare(ci_checks=checks)
        self.assertEqual(preparation.state, ReviewerState.REVIEW_INFRA_FAILURE)
        self.assertIsNone(preparation.context)

        worker_failure = replace(
            self.fixture.attempt, status=AttemptStatus.INFRA_FAILURE,
            blockers=("network unavailable",),
        )
        preparation = self.fixture.prepare(worker_attempt=worker_failure)
        self.assertEqual(preparation.state, ReviewerState.REVIEW_CONTEXT_INVALID)
        self.assertTrue(any("worker attempt is INFRA_FAILURE" in item for item in preparation.reasons))


class GitHubBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReviewerFixture()
        preparation = self.fixture.prepare()
        assert preparation.context is not None
        self.context = preparation.context
        result = self.fixture.workflow.review(
            self.context, self.fixture.findings(), "Approved."
        ).result
        assert result is not None
        self.result = result

    def tearDown(self) -> None:
        self.fixture.close()

    def test_prior_comments_include_review_bodies_and_inline_comments(self) -> None:
        github = GitHubCli(Path("."))
        reviews = (
            '{"reviews":[{"author":{"login":"old-reviewer"},"body":"review body",'
            '"commit":{"oid":"oldsha"}}],"comments":[]}'
        )
        inline = (
            '[{"user":{"login":"inline-reviewer"},"body":"fix this",'
            '"commit_id":"newsha","path":"reviewer/workflow.py","line":7}]'
        )
        with patch.object(github, "_run", side_effect=[reviews, "owner/repo", inline]):
            comments = github.prior_review_comments(4)

        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0].body, "review body")
        self.assertEqual(comments[1].path, "reviewer/workflow.py")
        self.assertEqual(comments[1].line, 7)


    def test_submit_posts_only_after_validation_and_authenticated_identity_check(self) -> None:
        github = GitHubCli(Path("."))
        with patch.object(github, "_run", side_effect=["reviewer-b", ""]) as run:
            github.submit_review(self.fixture.workflow, self.context, self.result)
        self.assertEqual(run.call_args_list[1].args[:4], ("pr", "review", "4", "--approve"))
        self.assertFalse(hasattr(github, "merge"))
        self.assertFalse(hasattr(github, "checkout"))
        self.assertFalse(hasattr(github, "push"))

    def test_submit_rejects_worker_or_unexpected_github_authority(self) -> None:
        github = GitHubCli(Path("."))
        with patch.object(github, "_run", return_value="worker-a") as run:
            with self.assertRaisesRegex(ValueError, "does not match reviewer identity"):
                github.submit_review(self.fixture.workflow, self.context, self.result)
        run.assert_called_once_with("api", "user", "--jq", ".login")


if __name__ == "__main__":
    unittest.main()
