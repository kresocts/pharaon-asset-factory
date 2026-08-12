# Worker workflow

The worker workflow prepares one disposable implementation attempt from durable repository state. It does not call an AI provider, schedule tickets, review changes, approve pull requests, or merge them.

Run from the repository root:

```bash
python -m worker.cli T-0003
```

This read-only command validates the ticket and dependencies and prints a deterministic JSON context. Add `--ensure-branch` to create or reuse the canonical ticket branch from `origin/main`. Git mutations are isolated in `worker.repository.GitRepository`; optional one-PR creation/reuse is isolated in `worker.github.GitHubCli` and requires an authenticated GitHub CLI.

The input contract contains the exact ticket scope, criteria, tests, repository rules, documentation references, and stable branch/PR policy. A later attempt reconstructs that context from the repository and supplies a new positive attempt number plus any previous observable result or review feedback. No process memory is required.

An attempt result records changed files, tests, criterion-by-criterion evidence, failures or blockers, scope deviations, and optional commit/PR identifiers. Its terminal state is one of `BLOCKED`, `IMPLEMENTATION_FAILED`, `VALIDATION_FAILED`, `INFRA_FAILURE`, or `REVIEW_READY`. Only `REVIEW_READY` represents successful worker completion; it still requires independent review.
