# 0010 - Immutable shape-model binding and offline cache preflight

- **Status:** Accepted
- **Ticket:** T-0023
- **Pull request:** (filled by the T-0023 implementation PR)

## Context

T-0021 defined a deterministic offline shape-job contract and T-0022 added an
explicit backend registry and immutable execution-request envelope. T-0014 already
provided the canonical external-model manifest schema, cache destination policy, and
offline plan/status/acquire/verify commands. The next contract boundary must connect
those pieces so a future Hunyuan shape execution ticket receives a bound, verified
model identity without enabling inference, downloads, or network access in the
preflight path.

## Decision

Reuse the T-0014 manifest parser and cache-verification implementation instead of
creating a second parser, cache-state module, or file-hashing policy. Harden that
existing reader with a finite byte limit, UTF-8 enforcement, duplicate-key rejection,
non-JSON-constant rejection, and stable recursion handling, then expose a public
read-only verification function that `models verify` delegates to.

The Hunyuan shape binding is operator-supplied and immutable. The manifest must
identify exactly `hunyuan3d-2.1-shape`, use a lowercase 40-hex immutable revision,
and use the exact `hunyuan3d-2.1-shape/<revision>` namespace. Every file URL must be
an HTTPS `huggingface.co` URL with no credentials, fragment, or query, and must
follow the exact `/tencent/Hunyuan3D-2.1/resolve/<revision>/hunyuan3d-dit-v2-1/`
boundary where the suffix exactly equals the manifest file path. Accepted roles are
`shape-config`, `shape-weights`, and `shape-auxiliary`, with at least one file for
each of the first two roles. No mutable reference, model-hub lookup, or revision
discovery is performed.

Cache verification is read-only. It inspects the existing destinations and hashes
regular files with the established SHA-256 implementation; it never creates
directories, locks, stale-part cleanup, temporary files, or final files, and never
downloads, repairs, or opens a socket.

Source URLs are excluded from the emitted execution envelope and model binding. This
prevents accidental disclosure if later generic manifests support signed URLs. The
binding is immutable and defensively copies sequence fields and `to_dict()` output.

Successful preflight still reports `execution_supported: false` and retains only
`GPU_EXECUTION_NOT_IMPLEMENTED`, because the Hunyuan runtime import, GPU executor, and
actual inference are future work.

## Consequences

Benefits:

- One authoritative manifest and cache policy remains in place, reducing drift and
  review risk.
- The preflight boundary is deterministic, offline, non-mutating, and safe to run
  repeatedly.
- Sensitive source URLs and signed credentials are not propagated into future
  execution state.
- Failure classes are stable and machine-readable.

Costs and risks:

- The canonical public model identity is encoded, but no production weight inventory
  or real model hashes are committed. Completeness of the real official manifest is
  intentionally deferred.
- A direct `docker.model_cache` import is accepted at this repository-stage boundary;
  a later ticket may introduce a formal package interface if needed.

Follow-up work:

- Independently review and publish the exact official Hunyuan shape artifact manifest
  and acquisition/license/provenance procedure.
- Implement a separate Hunyuan runtime/GPU execution ticket that imports and runs the
  model only after the bound manifest and cache are verified.
- Do not claim inference, GPU readiness, or model acquisition from this ticket.

## Alternatives considered

- Duplicating the T-0014 manifest and cache logic inside `asset_pipeline` was rejected
  because it would create two parsers, two hashing policies, and two failure
  contracts.
- Discovering or resolving model files at runtime was rejected because it would make
  the preflight nondeterministic and network-dependent and would violate the offline
  boundary.
- Emitting source URLs in the execution envelope was rejected because signed URLs can
  contain credentials and future manifests may be generic.
- Making cache verification repair or download missing files was rejected because
  preflight must not mutate state; acquisition remains an explicit, separately
  authorized command.
