# Tools

`provenance_capture.py` is a standard-library-only, append-only, tamper-evident
offline provenance logger. It is designed to capture bounded HTTP evidence only
after a future ticket obtains fresh operator authorization. T-0027 performs no live
network research and authorizes no public endpoint use.

The logger enforces an explicit HTTPS official-host allowlist, request and aggregate
byte budgets, no ambient credentials, no automatic retries, and no checkpoint/weight
body URLs. It writes one hash-chained JSONL record per request/hop and verifies the
complete chain before every append. Plans are immutable, canonical SHA-256-bound,
and must explicitly link a redirect source to its one exact target.

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