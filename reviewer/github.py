"""GitHub CLI boundary for reading PR evidence and posting validated reviews."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .contract import (
    CheckConclusion, CheckResult, PriorReviewComment, PullRequestEvidence,
    ReviewerContext, ReviewResult,
)
from .workflow import ReviewerWorkflow


class GitHubCli:
    """Review-only adapter; intentionally exposes no checkout, edit, push, or merge operation."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            ("gh", *arguments), cwd=self.root, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def authenticated_identity(self) -> str:
        return self._run("api", "user", "--jq", ".login")

    def pull_request(self, number: int, ticket_id: str) -> PullRequestEvidence:
        value = json.loads(self._run(
            "pr", "view", str(number), "--json",
            "number,url,headRefName,baseRefName,headRefOid",
        ))
        return PullRequestEvidence(
            number=value["number"], url=value["url"], ticket_id=ticket_id,
            head_branch=value["headRefName"], base_branch=value["baseRefName"],
            head_sha=value["headRefOid"],
        )

    def pull_request_diff(self, number: int) -> str:
        return self._run("pr", "diff", str(number))

    def checks(self, number: int) -> tuple[CheckResult, ...]:
        values = json.loads(self._run(
            "pr", "checks", str(number), "--json", "name,bucket,description",
        ))
        conclusions = {
            "pass": CheckConclusion.PASS,
            "fail": CheckConclusion.FAIL,
            "cancel": CheckConclusion.FAIL,
            "skipping": CheckConclusion.FAIL,
            "pending": CheckConclusion.FAIL,
        }
        return tuple(
            CheckResult(
                name=item["name"], required=True,
                conclusion=conclusions.get(item["bucket"], CheckConclusion.FAIL),
                details=item.get("description") or item["bucket"],
            )
            for item in values
        )

    def prior_review_comments(self, number: int) -> tuple[PriorReviewComment, ...]:
        value: dict[str, Any] = json.loads(self._run(
            "pr", "view", str(number), "--json", "reviews,comments",
        ))
        repository = self._run(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner",
        )
        inline: list[dict[str, Any]] = json.loads(self._run(
            "api", f"repos/{repository}/pulls/{number}/comments", "--paginate",
        ))

        comments = [
            PriorReviewComment(
                author=item["author"]["login"], body=item["body"],
                commit_sha=item.get("commit", {}).get("oid"),
            )
            for group in ("reviews", "comments")
            for item in value.get(group, ())
            if item.get("body")
        ]
        comments.extend(
            PriorReviewComment(
                author=item["user"]["login"], body=item["body"],
                commit_sha=item.get("commit_id"), path=item.get("path"),
                line=item.get("line") or item.get("original_line"),
            )
            for item in inline
            if item.get("body")
        )

        return tuple(comments)

    def submit_review(
        self,
        workflow: ReviewerWorkflow,
        context: ReviewerContext,
        result: ReviewResult,
    ) -> None:
        workflow.validate_for_posting(context, result)
        authenticated = self.authenticated_identity()
        if authenticated != context.reviewer_identity:
            raise ValueError(
                f"authenticated GitHub identity {authenticated!r} does not match reviewer identity"
            )
        flag = "--approve" if result.decision.value == "APPROVE" else "--request-changes"
        body = json.dumps(result.to_dict(), indent=2, sort_keys=True)
        self._run("pr", "review", str(context.pull_request.number), flag, "--body", body)
