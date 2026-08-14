# Architectural Decision Records

Use lightweight Architectural Decision Records (ADRs) for choices that materially constrain future implementation, operations, security, cost, or interfaces.

Create `NNNN-short-title.md` with:

- **Status:** Proposed, Accepted, Superseded, or Deprecated
- **Context:** the problem and constraints
- **Decision:** the chosen approach
- **Consequences:** benefits, costs, risks, and follow-up work
- **Alternatives considered:** credible options and why they were not chosen

Keep ADRs concise and link the relevant ticket and pull request. Do not rewrite accepted history. If a decision changes, add a new ADR and mark the old one superseded.

## Accepted decisions

- [0001: Stateless run-once orchestrator](0001-stateless-run-once-orchestrator.md)
- [0002: Containerized CUDA runtime and external model cache](0002-containerized-cuda-runtime.md)
- [0003: Pinned Hunyuan dependency layer and native-extension boundary](0003-pinned-hunyuan-dependency-layer.md)
- [0004: Bake pinned Hunyuan native extensions into the image](0004-baked-hunyuan-native-extensions.md)
- [0005: Runtime readiness gate](0005-runtime-readiness-gate.md)
- [0006: External model cache and controlled acquisition](0006-external-model-cache-and-controlled-acquisition.md)
- [0007: Secure Windows GHCR publishing workflow](0007-secure-ghcr-publishing-workflow.md)
- [0008: Deterministic shape-job contract and offline preflight](0008-deterministic-shape-job-contract.md)
- [0009: Shape execution-request and backend registry](0009-shape-execution-request-and-backend-registry.md)
- [0010: Immutable shape-model binding and offline cache preflight](0010-immutable-shape-model-binding-and-offline-cache-preflight.md)
- [0012: Verified offline provenance capture logger](0012-verified-provenance-capture-logger.md)
