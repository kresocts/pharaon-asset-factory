# ADR 0008: Deterministic shape-job contract and offline preflight

**Status:** Accepted

**Context:** Phase 2 — Asset worker begins after the Phase 1 container/runtime
boundary is complete. Real Hunyuan shape generation needs model manifests, weights,
GPU execution, and packaging, but introducing those pieces without a stable job
boundary would couple validation, path policy, and error semantics to inference code.
T-0021 must establish the contract and an offline, deterministic planning command
without downloading weights or running inference.

**Decision:** Add a new root Python package, `asset_pipeline`, distinct from the
implementation-agent `worker` package. Expose a canonical standard-library-only
command:

```bash
python -m asset_pipeline.cli shape plan --job path/to/job.json --json
```

Define one strict schema-v1 document with exactly `schema_version`, `job_id`,
`reference_image`, `seed`, and `remove_background`. Reject missing, unknown,
duplicate, malformed, incorrectly typed, out-of-range, non-finite, oversized, and
trailing-data documents. Cap job files at 64 KiB.

Resolve `INPUT_DIR`, `OUTPUT_DIR`, and `WORKSPACE_DIR` (defaults `/data/input`,
`/data/output`, and `/workspace`) as absolute existing directories. Derive input as
`INPUT_DIR/<reference_image>` and output/workspace as `<ROOT>/<job_id>/`. Enforce root
containment, reject input symlinks and symlinked ancestor escapes, refuse existing
output/workspace targets, and perform no writes or network access. Accept only
non-empty PNG, JPEG, or WebP regular files at or below 32 MiB with extension and
file-signature agreement.

Emit one deterministic schema-versioned JSON plan with `status: VALID`,
`classification: SHAPE_JOB_CONTRACT_READY`, `exit_code: 0`, `stage: shape`, and
`execution_supported: false`. Report the future Hunyuan3D 2.1 shape backend and
required-but-unconfigured model weights and GPU. Use stable exit codes `0`, `2`, `3`,
`64`, and `70` and concise JSON errors for expected validation/policy failures.

**Consequences:** The repository gains an independently testable contract and
preflight boundary before real model acquisition or inference. Later shape-generation
tickets can consume the validated plan without changing path-security or error
semantics. The command is safe to run in baseline CI without CUDA, PyTorch, Hunyuan,
Docker, or network access. It does not claim that shape generation is complete.

**Alternatives considered:** Reusing or renaming `worker` would mix asset-pipeline
logic with the implementation-agent workflow concern. Adding a third-party schema or
Pillow dependency would increase the offline/security surface unnecessarily.
Performing planning inside the container would make baseline host-side validation
harder and would mix runtime concerns into the contract.

Related ticket: T-0021.
