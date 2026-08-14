# Asset pipeline shape-job contract

This package is the Phase 2 asset-worker contract boundary. It is intentionally
distinct from the repository implementation-agent `worker` package, which manages
agent workflow context and GitHub state rather than asset generation.

The canonical host-side command is:

```bash
python -m asset_pipeline.cli shape plan --job path/to/job.json --json
```

The command is standard-library-only and performs no writes, directory creation,
downloads, or network requests.

## Why this contract exists before inference

Shape generation needs a stable, reviewable boundary before model weights, GPU
execution, or packaging are introduced. `shape plan` validates one reference-image
job, securely resolves its runtime paths, and emits a machine-readable plan that
later Hunyuan shape-generation work can consume. It does not load or run a model.

## Schema v1

The strict request contains exactly five fields:

```json
{
  "schema_version": 1,
  "job_id": "pharaoh-001",
  "reference_image": "references/pharaoh.png",
  "seed": 12345,
  "remove_background": true
}
```

- `schema_version` must be the integer `1`.
- `job_id` is 1-64 lowercase ASCII letters, digits, `.`, `_`, or `-`, and starts and
  ends with a letter or digit.
- `reference_image` is a relative path below `INPUT_DIR` using `/` separators only.
- `seed` is an integer from `0` through `4294967295`.
- `remove_background` is a boolean.

Missing, unknown, duplicate, malformed, incorrectly typed, out-of-range, non-finite,
oversized, and trailing-data documents are rejected. Job files are capped at 64 KiB.

## Path roots and containment

Roots follow the existing container conventions and may be overridden:

- `INPUT_DIR`, default `/data/input`
- `OUTPUT_DIR`, default `/data/output`
- `WORKSPACE_DIR`, default `/workspace`

Every root must be an absolute existing directory. The plan derives:

```text
input:     INPUT_DIR/<reference_image>
output:    OUTPUT_DIR/<job_id>/
workspace: WORKSPACE_DIR/<job_id>/
```

Resolved input/output/workspace paths must remain inside their roots. Input symlinks
and symlinked ancestors are rejected. Output and workspace targets must not already
exist, and planning never creates or modifies anything.

## Image type and size policy

Only non-empty PNG, JPEG, or WebP regular files at or below 32 MiB are accepted. The
command checks the file signature, not only the extension, and requires extension and
signature agreement.

## Deterministic JSON and exit codes

`--json` is mandatory for machine-readable use. A valid plan has
`execution_supported: false` and explicitly reports the future Hunyuan/GPU/model
requirements. No timestamps, random identifiers, hostnames, usernames, or process IDs
are emitted.

- `0` valid deterministic plan produced
- `2` invalid job document or input-policy refusal
- `3` invalid/missing runtime root or unsafe path
- `64` invalid CLI usage
- `70` unexpected internal error

Expected validation and policy failures emit concise versioned JSON, not Python
tracebacks.

## Future work

This ticket does not implement model manifests, weights, GPU execution, raw mesh
generation, texture generation, post-processing, packaging, or API/server work. Real
shape inference remains unimplemented.
