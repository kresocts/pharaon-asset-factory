"""Prepare and validate a reviewer package from deterministic local JSON evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from worker.contract import (
    AttemptResult, AttemptStatus, CriterionEvidence, EvidenceStatus, TestEvidence,
)

from .contract import CheckConclusion, CheckResult, PriorReviewComment, PullRequestEvidence
from .workflow import ReviewerState, ReviewerWorkflow


def _attempt(value: dict[str, Any] | None) -> AttemptResult | None:
    if value is None:
        return None
    return AttemptResult(
        schema_version=value["schema_version"], ticket_id=value["ticket_id"],
        branch=value["branch"], base_branch=value["base_branch"], attempt=value["attempt"],
        status=AttemptStatus(value["status"]), commit_sha=value.get("commit_sha"),
        pr_reference=value.get("pr_reference"),
        changed_files=tuple(value.get("changed_files", ())),
        change_summary=value.get("change_summary", ""),
        tests=tuple(
            TestEvidence(item["command"], EvidenceStatus(item["status"]), item["summary"])
            for item in value.get("tests", ())
        ),
        acceptance_criteria=tuple(
            CriterionEvidence(
                item["criterion"], EvidenceStatus(item["status"]),
                item.get("implementation_evidence", ""), item.get("test_evidence", ""),
            )
            for item in value.get("acceptance_criteria", ())
        ),
        blockers=tuple(value.get("blockers", ())), failures=tuple(value.get("failures", ())),
        scope_deviations=tuple(value.get("scope_deviations", ())),
    )


def _pull_request(value: dict[str, Any] | None) -> PullRequestEvidence | None:
    return PullRequestEvidence(**value) if value is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="local JSON evidence package")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.package.read_text(encoding="utf-8"))
        checks_value = value.get("ci_checks")
        comments_value = value.get("prior_review_comments")
        preparation = ReviewerWorkflow(args.root).prepare(
            ticket_id=value["ticket_id"],
            worker_attempt=_attempt(value.get("worker_attempt")),
            pull_request=_pull_request(value.get("pull_request")),
            pr_diff=value.get("pr_diff"),
            ci_checks=None if checks_value is None else tuple(
                CheckResult(
                    name=item["name"], required=item["required"],
                    conclusion=CheckConclusion(item["conclusion"]), details=item["details"],
                )
                for item in checks_value
            ),
            prior_review_comments=None if comments_value is None else tuple(
                PriorReviewComment(**item) for item in comments_value
            ),
            implementation_worker_identity=value.get("implementation_worker_identity"),
            reviewer_identity=value.get("reviewer_identity"),
        )
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({
            "state": ReviewerState.REVIEW_CONTEXT_INVALID.value,
            "context": None,
            "reasons": [str(error)],
        }, indent=2, sort_keys=True))
        return 2
    print(json.dumps(preparation.to_dict(), indent=2, sort_keys=True))
    return 0 if preparation.state == ReviewerState.READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
