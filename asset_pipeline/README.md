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

## Shape execution-request and backend registry

T-0022 adds an explicit, immutable preparation handoff. Run:

```bash
python -m asset_pipeline.cli shape prepare \
  --job path/to/job.json \
  --backend hunyuan3d-2.1-shape \
  --json
```

The command reuses `shape plan` validation and path policy, resolves the backend from
a fixed local registry, and emits a schema-versioned execution request. `--backend` is
required; no hidden default or dynamic plugin loading exists. The canonical backend is
`hunyuan3d-2.1-shape`, implementation `hunyuan3d-2.1`, and it references the same
Hunyuan3D 2.1 repository and immutable commit pinned in `docker/Dockerfile`.

Valid preparation output has `classification: SHAPE_EXECUTION_REQUEST_READY`,
`preparation_supported: true`, and `execution_supported: false`. It includes
deterministic blockers for missing production model-manifest binding, model-cache
verification, and GPU execution. Repeated runs with the same job and roots are
byte-identical. The command performs no writes, downloads, model-cache access, heavy
runtime imports, GPU initialization, or inference.

## Shape model preflight and immutable binding

T-0023 adds an explicit offline binding and cache-verification boundary. Run:

```bash
python -m asset_pipeline.cli shape preflight   --job path/to/job.json   --backend hunyuan3d-2.1-shape   --model-manifest path/to/model-manifest.json   --json
```

The command reuses `shape plan` validation and the T-0022 backend registry, parses the
operator-supplied manifest with the existing T-0014 implementation, applies the strict
Hunyuan shape-model binding policy, and verifies every required artifact already
present under `MODEL_CACHE_DIR` by exact size and SHA-256. It emits a deterministic,
sanitized, versioned envelope with `classification: SHAPE_MODEL_PREFLIGHT_READY`,
`model_binding_supported: true`, `model_cache_verified: true`, and
`execution_supported: false`. The only remaining blocker on success is
`GPU_EXECUTION_NOT_IMPLEMENTED`.

The model binding accepts only the canonical artifact set `hunyuan3d-2.1-shape`, a
lowercase 40-hex immutable revision, the exact corresponding namespace, and immutable
`https://huggingface.co` URLs with no credentials, query, or fragment. Accepted file
roles are `shape-config`, `shape-weights`, and `shape-auxiliary`; at least one
`shape-config` and one `shape-weights` file are required. Source URLs are not emitted
in the binding or preflight envelope.

Preflight is read-only and performs no writes, downloads, model-hub calls, heavy ML
imports, GPU initialization, or inference. Cache verification never repairs or
acquires missing files; acquisition remains the separately authorized `models acquire`
command.

## Future work

This ticket does not implement production weight manifests, real model hashes,
licenses or acquisition, Hunyuan imports, GPU execution, raw mesh generation, texture
generation, post-processing, packaging, or API/server work. Real shape inference
remains unimplemented.
