# Pharaon Asset Factory

Pharaon Asset Factory is the foundation for a future reproducible AI game-asset generation pipeline. The intended system will turn a prompt or reference image into a processed, game-ready GLB with metadata and package the results as a ZIP archive.

## Current status

Phase 0 is complete. Phase 1 now includes the reproducible CUDA/Python base, pinned PyTorch and Hunyuan3D dependencies, and compiled Hunyuan native rasterizer/mesh-painter extensions for CUDA architectures 8.6 and 8.9. CPU-safe diagnostics distinguish native readiness from full inference readiness; model weights, inference, GPU provisioning, and a web interface remain intentionally absent. See [the GPU image guide](docker/README.md).

## Intended architecture

The long-term pipeline is:

```text
prompt/reference → image stage → GPU 3D worker → shape → texture
                 → post-processing → GLB → metadata/report → ZIP
```

Control-plane coordination will use repository and GitHub state so that workers and reviewers can be short-lived and reproducible. GPU execution will eventually run in a container on ephemeral local or rented NVIDIA hardware. See [the architecture overview](architecture/overview.md) and [roadmap](PLAN.md).

## Development model

Work is divided into small tickets. A disposable worker implements one ticket on a ticket-specific branch and opens a pull request. A separate reviewer checks the ticket, diff, tests, and CI before approving or requesting changes. Agents do not rely on persistent memory; durable context belongs in tickets, Git history, architecture records, and pull requests.

Future agents must begin by reading:

1. `AGENTS.md`
2. `PLAN.md`
3. The assigned file in `tickets/`
4. Relevant files in `architecture/`

They should then verify dependencies, create `ticket/T-XXXX-short-description` from current `main`, remain within allowed scope, run required tests, and use the pull request template.

## Repository structure

- `AGENTS.md` — mandatory operating contract for workers and reviewers
- `PLAN.md` — phased roadmap
- `architecture/` — system, security, and decision documentation
- `tickets/` — canonical specifications and machine-readable ticket state
- `validation/` — dependency-free repository metadata validator
- `tests/` — validator tests
- `worker/` — provider-neutral worker context, evidence, and Git/GitHub boundaries
- `reviewer/` - independent review context, decision, validation, and GitHub boundaries
- `orchestrator/` - run-once selection, claims, persisted state, dispatch boundaries, and reconciliation
- `.github/` — issue/PR templates and minimal CI

Application, container, and provider-specific directories will be introduced only by tickets that need them.

## Ticket workflow

Ticket metadata uses YAML front matter with an ID, workflow status, dependencies, and priority. The human-readable body defines immutable acceptance criteria and required tests. Status changes are committed so future orchestration can derive state from the repository. See `tickets/README.md`.

## Baseline validation and CI

Run the same baseline checks locally from the repository root with:

```bash
python validation/run_ci.py
```

The command runs the complete automated test suite followed by repository metadata validation and stops at the first failed stage. Its stage labels and exit code identify whether automated tests or metadata validation failed. Individual stages can be reproduced with `python validation/run_ci.py --stage tests` and `python validation/run_ci.py --stage metadata`.

GitHub Actions runs baseline CI for pull requests targeting `main` and pushes to `main`, with read-only repository permissions. Tests and metadata validation remain separate workflow steps for clear failure reporting. New commits supersede older in-progress runs for the same pull request or branch without cancelling unrelated work.

Future tickets should extend the canonical entry point with a distinct, documented stage and add a corresponding workflow step when separate CI reporting is useful. They should not add another workflow that repeats existing baseline checks.

## Phase 0 handoff validation

T-0006 validates the real ephemeral worker/reviewer handoff with a deliberately small repository health command; it does not begin Phase 1. Run:

```bash
python validation/phase0_status.py
```

The command reads T-0001 through T-0005, counts the canonical worker, reviewer, and orchestrator workflow tests, and verifies that the documented baseline CI entry point exists. It prints `PHASE_0_READY` and exits zero when all Phase 0 tickets are `DONE`; otherwise it identifies the incomplete state, prints `PHASE_0_INCOMPLETE`, and exits non-zero.

## Worker preparation

Prepare the deterministic context for exactly one runnable ticket without an AI provider or GitHub credentials:

```bash
python -m worker.cli T-0003
```

Use `--ensure-branch` only when the caller intends to create or reuse the canonical ticket branch from `origin/main`. See [the worker workflow contract](worker/README.md) for result states, evidence requirements, and the optional GitHub boundary.

## Reviewer preparation

Validate a deterministic local reviewer evidence package without an AI provider or GitHub credentials:

```bash
python -m reviewer.cli path/to/reviewer-package.json
```

The package deliberately combines the unchanged canonical ticket with T-0003 worker evidence, PR identity/diff, CI checks, prior comments, and distinct worker/reviewer identities. See [the reviewer workflow contract](reviewer/README.md) for the two decisions, explicit failure states, stateless repeated reviews, and guarded GitHub posting boundary.

## Orchestrator

Inspect deterministic eligibility or advance at most one bounded orchestration step:

```bash
python -m orchestrator.cli list-ready
python -m orchestrator.cli run-once --owner local-run
```

The state machine persists compare-and-set workflow state and atomic claims, reconstructs
progress from worker/PR/CI/reviewer evidence after restart, and exposes provider-neutral
dispatch interfaces only. It includes no LLM, paid/cloud provider, merge operation, or
endless loop. See [the orchestrator contract](orchestrator/README.md).


## Security and cost control

Secrets never belong in Git. Future cloud operations must use least-privilege credentials, explicit user approval, bounded retries, price/runtime/job limits, secure artifact retrieval, and automatic teardown. No paid resource is created by the current repository. See `architecture/security.md`.
