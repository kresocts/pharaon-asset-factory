# Pharaon Asset Factory

Pharaon Asset Factory is the foundation for a future reproducible AI game-asset generation pipeline. The intended system will turn a prompt or reference image into a processed, game-ready GLB with metadata and package the results as a ZIP archive.

## Current status

Phase 0 is complete. Phase 1 is complete as the reproducible CUDA/Python base, pinned PyTorch and Hunyuan3D dependencies, and compiled Hunyuan native rasterizer/mesh-painter extensions for CUDA architectures 8.6 and 8.9. CPU-safe diagnostics distinguish native readiness from full inference readiness; model weights, inference, GPU provisioning, and a web interface remain intentionally absent. The container exposes a canonical `ready` command (docker run --rm IMAGE ready --profile cpu --json, or --profile native-gpu with GPU passthrough) that returns a versioned, machine-readable pre-weights runtime readiness decision. It also exposes a canonical external model-cache command (`models plan|status|acquire|verify`) with a versioned artifact-manifest schema, offline planning/status/verification, explicit download authorization, hard byte limits, streamed and integrity-verified acquisition, and concurrency locking; all T-0014 validation uses tiny local fixtures and downloads no real model weights. See [the GPU image guide](docker/README.md).

## Phase 2 contract/preflight foundation

T-0021 introduces the first Phase 2 asset-worker foundation: a deterministic, offline
shape-job contract and preflight planner. Run:

```bash
python -m asset_pipeline.cli shape plan --job path/to/job.json --json
```

The command validates and normalizes one reference-image job and emits a
machine-readable plan. It does not download model weights or run Hunyuan inference.

T-0022 extends that foundation with an explicit backend registry and immutable
execution request:

```bash
python -m asset_pipeline.cli shape prepare --job path/to/job.json --backend hunyuan3d-2.1-shape --json
```

`shape prepare` reuses the `shape plan` validation and path policy, resolves the
canonical Hunyuan3D 2.1 shape backend from a fixed local registry, and emits a
schema-versioned execution request. It reports `preparation_supported: true` and
`execution_supported: false`, with deterministic blockers for future model binding,
model-cache verification, and GPU execution.

T-0023 adds the offline model-binding/cache-preflight boundary:

T-0025 adds the reviewed official production shape inventory and provenance:

- `model-manifests/production/hunyuan3d-2.1-shape.json`
- `model-manifests/production/hunyuan3d-2.1-shape.provenance.json`

It passes the existing T-0014 manifest parser and T-0023 immutable binding policy and
includes the bounded request log and exact loader source line ranges. It does not
authorize acquisition or inference. Validate it offline with
`python validation/validate_production_shape_manifest.py`.

```bash
python -m asset_pipeline.cli shape preflight   --job path/to/job.json   --backend hunyuan3d-2.1-shape   --model-manifest path/to/model-manifest.json   --json
```

`shape preflight` validates one operator-supplied immutable Hunyuan shape-model
manifest with the existing T-0014 policy and verifies every required artifact already
present in `MODEL_CACHE_DIR` by exact size and SHA-256. It performs no writes,
downloads, network access, heavy ML imports, GPU initialization, or inference. On
success it reports `SHAPE_MODEL_PREFLIGHT_READY`, `model_binding_supported: true`,
`model_cache_verified: true`, and `execution_supported: false`, with only the
`GPU_EXECUTION_NOT_IMPLEMENTED` blocker remaining. The real official production model
manifest/license/provenance and Hunyuan runtime/GPU execution remain future tickets;
no weights or inference are implemented.

Shape generation, texture generation, post-processing, packaging, and API/server work
remain future tickets, and Phase 2 is not complete.

## Intended architecture

The long-term pipeline is:

```text
prompt/reference â†’ image stage â†’ GPU 3D worker â†’ shape â†’ texture
                 â†’ post-processing â†’ GLB â†’ metadata/report â†’ ZIP
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

- `AGENTS.md` â€” mandatory operating contract for workers and reviewers
- `PLAN.md` â€” phased roadmap
- `architecture/` â€” system, security, and decision documentation
- `tickets/` â€” canonical specifications and machine-readable ticket state
- `validation/` â€” dependency-free repository metadata validator
- `tests/` â€” validator tests
- `worker/` â€” provider-neutral worker context, evidence, and Git/GitHub boundaries
- `reviewer/` - independent review context, decision, validation, and GitHub boundaries
- `orchestrator/` - run-once selection, claims, persisted state, dispatch boundaries, and reconciliation
- `.github/` â€” issue/PR templates and minimal CI

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

## GHCR publishing foundation

T-0015 defined the secure, manually triggered `publish-container.yml` workflow
foundation for `ghcr.io/kresocts/pharaon-asset-factory`. T-0016 completed local
publisher integration validation against a disposable `127.0.0.1` registry.

T-0017 attempted the first controlled GHCR publication. That attempt failed at the
existing-tag preflight in run 31800647785 and is recorded as `FAILED`; T-0017 is now
`SUPERSEDED`, not `DONE`. T-0018 normalized handled PowerShell native exit state so an
expected absent-tag result does not leave the process in failure. T-0019 then completed
a separately approved successful controlled SHA-only publication to GHCR; see
[docker/README.md](docker/README.md).

The first failed attempt did not build or push an image and did not create a SHA,
release, or `latest` tag. Its failed evidence remains authoritative. Model weights and
inference remain unimplemented, and Phase 2 — Asset worker is the next roadmap phase.
