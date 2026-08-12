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
