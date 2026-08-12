# AGENTS.md

This file is the operating contract for every agent working in this repository. Repository state is authoritative; agent memory is disposable.

## Roles and responsibility

- An implementation worker receives exactly one ticket per task and works only on that ticket.
- A worker must not approve or merge its own pull request.
- A reviewer evaluates the ticket, its acceptance criteria, the diff, tests, CI, and earlier review comments. The result must be `APPROVE` or `REQUEST_CHANGES` with actionable reasons.
- Never edit, reinterpret, or weaken acceptance criteria to make an implementation pass.
- Keep agents stateless. Persist project knowledge in Git, tickets, pull requests, ADRs, CI results, or other repository files.
- Report blockers with relevant evidence instead of retrying indefinitely.

## Scope and implementation

- Modify only files within the ticket's allowed scope unless a technically required dependency forces an expansion. Explain any expansion in the pull request.
- Prefer the smallest maintainable implementation that satisfies the ticket. Avoid speculative abstractions.
- Do not silently substitute a different architecture for the requested implementation. Propose and document material changes first.
- Record important architectural decisions in `architecture/decisions/`.
- Distinguish implementation failures from infrastructure failures in tests, CI, and reports whenever possible.
- Expensive operations must have explicit budgets and limits before they are introduced.
- Never create cloud resources or use paid APIs autonomously. Such actions require an explicit, approved workflow and cost controls.

## Security

- Never expose or commit secrets.
- Never commit credentials, API keys, access tokens, private keys, cloud credentials, or `.env` contents.
- Use least-privilege credentials supplied through approved secret stores or environment variables.
- Sanitize logs and review staged changes before every commit.

## Branches and tickets

- Never push directly to `main`.
- Create a ticket branch from current `main` named `ticket/T-XXXX-short-description`.
- Reference the canonical ticket ID (`T-XXXX`) in branch names, commits, and pull requests.
- Confirm the ticket's dependencies are `DONE` before starting unless the ticket explicitly documents an exception.
- Keep one implementation ticket per pull request.

## Commits and pull requests

- Make focused commits with imperative subjects, for example: `T-0002 add metadata CI checks`.
- Do not mix unrelated formatting, refactoring, or cleanup into a ticket.
- Complete the pull request template, link the ticket, and map results to every acceptance criterion.
- Disclose risks, limitations, scope expansion, and follow-up work.
- A worker may open or update its pull request but must never approve or merge it.

## Tests and completion

- Run every test required by the ticket before declaring it complete, plus relevant regression checks.
- Deterministic test failures and CI failures are failures, not suggestions.
- Never skip, delete, weaken, or suppress tests merely to make CI pass.
- Include exact test commands and results in the pull request.
- Do not mark a ticket `DONE` until all acceptance criteria are met and the change has passed the required review and merge workflow.

## Repository guide

- `PLAN.md` defines the roadmap and phase dependencies.
- `architecture/` contains system guidance and architectural decisions.
- `tickets/` contains canonical ticket specifications and machine-readable workflow state.
- `validation/` contains repository metadata validation.
- `tests/` contains automated tests.
- `.github/` contains collaboration templates and CI workflows.

When instructions conflict, follow the ticket's explicit scope and acceptance criteria unless they conflict with security policy. Escalate unresolved conflicts in the pull request rather than guessing.
