# Reviewer workflow

The reviewer workflow reconstructs one deliberate review package from the canonical ticket, the existing `worker.contract.AttemptResult`, pull-request metadata and diff, CI checks, prior comments, and explicit worker/reviewer identities. It does not call an AI provider, modify implementation, change acceptance criteria, select tickets, approve as the worker, or merge pull requests.

The deterministic core lives in `reviewer.workflow.ReviewerWorkflow`. Preparation validates ticket, branch, PR, and commit alignment; verifies the worker attempt with the T-0003 contract; preserves every ticket criterion in order; requires CI and prior-comment context; and rejects equal worker/reviewer identities before any decision. A successful review decision is exactly `APPROVE` or `REQUEST_CHANGES`. Approval requires every criterion and required CI check to pass. A change request requires precise actionable reasons.

Preparation/execution failures are separate from decisions:

- `REVIEW_CONTEXT_INVALID` - required or mutually consistent evidence is absent.
- `REVIEW_INFRA_FAILURE` - supplied evidence explicitly identifies CI infrastructure failure.
- `REVIEW_EXECUTION_FAILED` - complete context exists but the proposed findings/result are malformed.
- `READY` and `REVIEW_COMPLETE` are successful workflow states.

Review results contain only observable findings, concise conclusions, actionable reasons, and audit metadata. Prior comments are part of every context, so attempt 2 can be reviewed by a fresh process without memory of attempt 1.

## Local preparation

Prepare and validate a package without GitHub credentials, API keys, paid services, or an LLM:

```bash
python -m reviewer.cli path/to/reviewer-package.json
```

The JSON package has these top-level fields:

```json
{
  "ticket_id": "T-0004",
  "worker_attempt": {"schema_version": 1, "status": "REVIEW_READY"},
  "pull_request": {
    "number": 4,
    "url": "https://example.test/pull/4",
    "ticket_id": "T-0004",
    "head_branch": "ticket/T-0004-define-reviewer-agent-workflow",
    "base_branch": "main",
    "head_sha": "abc123"
  },
  "pr_diff": "diff --git ...",
  "ci_checks": [
    {"name": "baseline", "required": true, "conclusion": "PASS", "details": "passed"}
  ],
  "prior_review_comments": [],
  "implementation_worker_identity": "worker-a",
  "reviewer_identity": "reviewer-b"
}
```

`worker_attempt` uses the full T-0003 `AttemptResult.to_dict()` shape. CI conclusions are `PASS`, `FAIL`, or `INFRA_FAILURE`. An empty prior-comment list is valid; omitting it is not, because absence must not be confused with a confirmed lack of earlier feedback.

## GitHub boundary

`reviewer.github.GitHubCli` can read PR metadata, diff, checks, and prior comments and can post a validated approval or change request. Before posting, it revalidates the ticket, PR, head SHA, identities, decision, criteria, CI, and actionable reasons, then confirms that the authenticated GitHub identity is the declared reviewer. The adapter deliberately has no checkout, edit, commit, push, or merge method. Unit tests replace command execution and never contact GitHub.
