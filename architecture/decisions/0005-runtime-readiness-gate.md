# ADR 0005: Runtime readiness gate

**Status:** Accepted

**Context:** Before model downloads or inference, a controller or operator needs one
deterministic answer to "is this container runtime technically ready for the next
deployment stage?" T-0012 already proves the native extensions compile and import, but
its health command is diagnostic rather than a machine-readable pass/fail gate.

**Decision:** Introduce one canonical `ready` command exposed through the existing
entrypoint. It uses a small profile model (`cpu`, `native-gpu`), a versioned JSON
schema, stable check identifiers, and a fixed exit-code contract:

- `0` READY
- `2` NOT_READY (expected requirements not met)
- `3` DIAGNOSTIC_ERROR (broken diagnostic logic)
- `64` INVALID_REQUEST

The gate reuses `health.py` and `native_smoke.py` for shared diagnostics and never
accesses the network or downloads model assets. Native/runtime readiness is separate
from model-weight and inference readiness; weights may be `ABSENT` while
`full_ready` remains `false`.

**Consequences:** A future local controller, Vast.ai deployment, RunPod deployment, or
human operator can gate the model-acquisition stage without parsing human prose. The
stable contract must be versioned before future breaking changes, and any new profile
or check must update the schema, tests, and Docker documentation together.

**Alternatives considered:** Reusing only the existing diagnostic `health` command
would leave automation parsing free-form output. Adding several overlapping readiness
commands would create multiple decision paths. A model-weight-aware inference profile
would pretend inference can work without weights.

Related ticket: T-0013.
