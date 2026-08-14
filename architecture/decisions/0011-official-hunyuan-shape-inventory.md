# 0011 - Official Hunyuan shape inventory

- **Status:** Accepted
- **Ticket:** T-0025
- **Pull request:** (filled by the T-0025 implementation PR)

## Context

T-0023 introduced a strict, immutable Hunyuan shape-model binding and offline cache
preflight, but it intentionally did not commit the real official Hunyuan3D 2.1 shape
artifact inventory. PR #22 (`T-0024`) attempted that inventory and was closed unmerged
because its network accounting was incomplete, its loader source line ranges were not
retained, and its validator temporarily accepted missing evidence.

T-0025 is a clean retry from current `main` with an explicitly bounded public-research
session. It must establish exactly the minimal shape inventory and complete
independently reviewable provenance without authorizing acquisition or changing the
T-0023 binding/runtime behavior.

## Decision

The official production shape inventory is:

```text
hunyuan3d-dit-v2-1/config.yaml
hunyuan3d-dit-v2-1/model.fp16.ckpt
```

The immutable model revision is `0b94677654c57bb9a6b6845cd7b704ccf551d327`. The
checkpoint identity is taken from the official Hugging Face LFS metadata field
`lfs.oid`, never from a downloaded checkpoint body. The small config body is directly
retrieved and hashed from the official immutable raw endpoint. Loader compatibility is
proven by retaining the exact pinned source bodies for `model_worker.py`,
`hy3dshape/hy3dshape/pipelines.py`, and `hy3dshape/hy3dshape/utils/utils.py` and
recording exact positive line ranges.

The provenance record includes the authoritative request log, with every request and
redirect hop counted, exact response-body byte counts, SHA-256 values, and aggregate
totals. The offline validator rejects missing, blocked, placeholder, mutable,
oversized, or weight-body evidence. No checkpoint/weight body, model-cache population,
Docker/GHCR/runner/GPU/cloud/paid/inference operation is authorized by this decision.

## Consequences

- The production manifest passes the existing T-0014 parser and T-0023 binding policy
  and enables offline planning/status/verify and shape preflight evidence.
- Future acquisition remains separate and requires explicit operator authorization.
- The old unmerged PR #22 attempt remains historical audit material only and is not
  reused as T-0025 evidence.
- The loader source pin must remain synchronized with `docker/Dockerfile`.
