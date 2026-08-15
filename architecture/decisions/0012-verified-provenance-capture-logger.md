# 0012 - Verified offline provenance capture logger

- **Status:** Accepted
- **Ticket:** T-0027
- **Pull request:** (filled by the T-0027 implementation PR)

## Context

T-0024, T-0025, and T-0026 were closed, unmerged attempts to capture authoritative
Hunyuan shape provenance. Independent review found incomplete request-byte
accounting, manually reconstructed evidence, and a redirect design that could not
safely represent a relative Hugging Face `Location`. T-0027 must land only the
reusable logger on `main`, fully tested offline, with the redirect and plan-linkage
defects corrected. No public provenance request is authorized by this ticket.

## Decision

Use a standard-library-only Python logger with these invariants:

- The transport never follows redirects automatically.
- Every received HTTP body, including non-2xx and redirect bodies, is measured by
  exact byte count and SHA-256 subject to the remaining budget.
- A declared `Content-Length` is checked before reading; streaming responses are
  capped and fail closed when the budget would be exceeded.
- Requests are budgeted, credentials/cookies/range requests are forbidden, and only
  the explicit official-host HTTPS allowlist is accepted.
- The session plan is parsed with a finite UTF-8 byte limit, duplicate-key and
  non-finite-number rejection, bounded nesting, strict portable request IDs,
  canonically unique URLs, and exact order semantics. Checkpoint/weight markers are
  screened in raw and safely decoded path/query content.
- A canonical plan SHA-256 is stored in session state and bound to every record and
  summary. Changing the plan after initialization is fatal.
- Redirect entries use explicit bidirectional linkage: the source names one
  `redirect_target_id`, and the target names the same source in `redirect_from_id`.
  The target must be the immediate next planned entry. A source record records
  `redirect_authorized` only after exact target validation; it does not claim
  `redirect_followed` until the adjacent target record exists and binds back to the
  source record hash.
- Relative and scheme-relative `Location` values are resolved with
  `urllib.parse.urljoin`, validated after resolution, and compared to the approved
  target under a narrow URL contract: scheme and host case-insensitive, absent port
  and 443 equivalent, path and query exact, no fragments, no percent-decoding, no
  query reordering, and no security-relevant normalization.
- Records form an append-only JSONL hash chain starting from the all-zero hash.
  `current_hash` is computed over canonical JSON excluding itself. Verification runs
  before every append and rejects inserted, removed, reordered, duplicated, modified,
  or truncated records. Finalization appends a terminal `SESSION_FINALIZED` record;
  blocked and finalized state are derived from the chain, not trusted mutable flags.
- Retained response bodies use sequence-only filenames, are written only after safe
  resolved-path containment checks, and are verified for regular-file status, size,
  SHA-256, and absence of symlink/escape/orphan anomalies.
- The CLI exposes `validate-plan`, `init`, `request`, `verify`, and `finalize`.
  `request` executes exactly one planned hop, so a valid redirect response is logged
  before the target request is separately issued.
- Plan limits and structure are strict: `1 <= max_bytes <= DEFAULT_MAX_BYTES`,
  `1 <= max_requests <= DEFAULT_MAX_REQUESTS`, `len(requests) <= max_requests`,
  unknown top-level/request fields are rejected, duplicate allowed hosts are
  rejected, and the authoritative stored plan must contain a valid lowercase 64-hex
  `plan_hash`.
- Response framing is fail-closed: `Content-Length` plus `Transfer-Encoding` is
  refused; only `Transfer-Encoding: chunked` without `Content-Length` is streamed and
  measured; malformed, duplicated, mixed-case-normalized, or unsupported codings are
  blocking.
- Retained-body persistence, atomic JSON temporary writes, and JSONL append use
  robust full-write loops. Real `OSError`, short writes, and fsync failures become
  authoritative `RESPONSE_STORAGE_ERROR` records rather than escaping or allowing a
  retry. Every authorized attempt first appends a durable `REQUEST_RESERVED` record;
  authoritative file opens compare `fstat`/`lstat` identity and reject path swaps.

## Consequences

Benefits:

- The logger is reusable for a later independently authorized provenance session.
- The central redirect failure from T-0026 is fixed without allowing generic
  `follow` behavior or unrelated later requests.
- Evidence records are tamper-evident and cannot be silently repaired or rewritten.
- All tests are offline, using local fakes and in-memory transports.

Costs and risks:

- The logger is intentionally stricter than a general HTTP client. The exact redirect
  target and immediate-next-entry constraints reject valid-but-unplanned redirects.
- It records bodies and hashes but does not produce a production model manifest,
  license conclusion, or model-inventory claim.
- A future live-session ticket must still obtain fresh operator authorization and
  supply an explicit plan for every hop.

Follow-up work:

- Obtain separate operator authorization for a future bounded provenance session.
- Use this logger to capture the exact official Hunyuan shape config, license, and
  source evidence without downloading weights.
- Extend plan tooling only if a later use case requires additional HTTP methods,
  signed URLs, or authenticated hosts; such changes need their own security review.

## Alternatives considered

- Reusing the T-0026 logger unchanged was rejected because its `follow` pointer
  permitted only "some later planned request" and did not safely resolve relative
  redirects.
- Letting the transport follow redirects automatically was rejected because the
  logger would lose hop-level byte accounting and exact evidence ordering.
- A generic URL normalization library or query canonicalization was rejected because
  it could collapse distinct paths or reorder security-relevant query content.
- A repair/rewrite command was rejected because authoritative evidence must remain
  append-only and tamper-evident.