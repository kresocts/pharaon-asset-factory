# Tools

`provenance_capture.py` is a standard-library-only, append-only, tamper-evident
offline provenance logger. It is designed to capture bounded HTTP evidence only
after a future ticket obtains fresh operator authorization. T-0027 performs no live
network research and authorizes no public endpoint use.

The logger enforces an explicit HTTPS official-host allowlist, request and aggregate
byte budgets, no ambient credentials, no automatic retries, and no checkpoint/weight
body URLs in raw or decoded path/query content. It writes one hash-chained JSONL
record per request/hop and verifies the complete chain before every append. Plans
are immutable, canonical SHA-256-bound, use strict portable request IDs, reject
canonically equivalent duplicate URLs, and must explicitly link a redirect source
to its one exact target.

A redirect source records authorization only after exact target validation; it does
not claim the redirect was followed until the adjacent target record exists and binds
back to the source record hash. Finalization appends a terminal hash-chained record.
Retained bodies use sequence-only filenames and are verified for safe containment,
size, SHA-256, and single-link status during session verification.

Plans enforce strict hard limits and field sets. `max_bytes` may not exceed
`DEFAULT_MAX_BYTES`, request count may not exceed `max_requests`, unknown plan/request
fields are rejected, duplicate allowed hosts are rejected, and the stored authoritative
plan must contain its canonical lowercase 64-hex `plan_hash`. Conflicting
`Content-Length`/`Transfer-Encoding` framing blocks the session. All retained-body,
atomic JSON, and JSONL writes use full-write loops and fail closed on `OSError`,
short writes, or fsync failures.

Run with:

```bash
python tools/provenance_capture.py validate-plan --plan session-plan.json
python tools/provenance_capture.py init --session-dir DIR --plan session-plan.json
python tools/provenance_capture.py request --session-dir DIR --entry-id ENTRY
python tools/provenance_capture.py verify --session-dir DIR
python tools/provenance_capture.py finalize --session-dir DIR
```

All commands emit machine-readable JSON and stable exit codes. The logger is also
importable as `tools.provenance_capture`.