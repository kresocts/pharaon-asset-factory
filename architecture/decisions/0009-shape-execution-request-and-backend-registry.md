# ADR 0009: Shape execution-request and backend registry

**Status:** Accepted

**Context:** T-0021 established a deterministic, offline shape-job plan but left the
backend identity implicit inside a human-readable requirements block. Future Hunyuan
model binding, model-cache verification, and GPU execution tickets need a stable
contract boundary from the validated job plan to the execution handoff. Introducing
model manifests, weights, or Hunyuan imports now would violate the offline
preparation boundary and create hidden environment dependencies.

**Decision:** Add an explicit, versioned, fixed local backend registry and an
immutable execution-request envelope. The canonical backend ID is
`hunyuan3d-2.1-shape`, implementation `hunyuan3d-2.1`, source repository
`https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git`, and immutable source revision
`82920d643c0dc2f7bfd7255f45f62d386edfe60c`, matching the container's Dockerfile
arguments and labels. Backend IDs are plain, exact local strings; there is no plugin
discovery, entry-point scanning, filesystem crawling, arbitrary-string import, or
dynamic code loading.

Expose a new standard-library-only command:

```bash
python -m asset_pipeline.cli shape prepare \
  --job path/to/job.json \
  --backend hunyuan3d-2.1-shape \
  --json
```

The command reuses T-0021 document validation and path planning, resolves the backend
through the registry, and emits a schema-versioned execution request. Valid output has
`classification: SHAPE_EXECUTION_REQUEST_READY`, `exit_code: 0`,
`preparation_supported: true`, and `execution_supported: false`, with deterministic
blockers for production model-manifest binding, model-cache verification, and GPU
execution. The execution request is an immutable dataclass derived only from the
validated plan and resolved backend; emitted dictionaries are new copies.

**Consequences:** Later model-binding and Hunyuan inference tickets have a stable
handoff without coupling to model files or runtime hardware. Preparation can succeed
even though inference remains unsupported because it validates the job and contract
inputs, not the unavailable runtime prerequisites. The backend registry is explicit
and testable, reducing accidental plugin or import behavior. The container Dockerfile
pin remains the source of truth for the Hunyuan revision; a repository consistency
test compares the descriptor to that pin without parsing Dockerfile at runtime.

**Alternatives considered:** Dynamically importing backend modules from IDs would
increase the security and reproducibility surface. Inferring a backend from the job
document would introduce an implicit default. Emitting a less-structured plan-only
result would not provide the immutable request object later execution tickets need.
Creating a production model manifest or fixture weights in application code would
blur preparation with acquisition and was rejected as out of scope.

Related ticket: T-0022.

