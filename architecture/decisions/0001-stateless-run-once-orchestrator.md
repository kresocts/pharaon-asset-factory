# ADR 0001: Stateless run-once orchestrator

**Status:** Accepted

**Context:** T-0005 must connect disposable worker and independent reviewer attempts
without hidden memory, uncontrolled retries, paid execution, or autonomous merging.

**Decision:** The orchestrator performs one bounded step per invocation. Repository and
GitHub evidence is authoritative; state, claims, counters, identities, and audit events
are persisted through a compare-and-set backend. Worker and reviewer providers remain
separate interfaces over the T-0003 and T-0004 contracts. Retries are finite and keep
infrastructure, implementation, validation, and reviewer execution failures distinct.
Approval means awaiting human merge; it never invokes merge or marks a ticket complete.
No paid or cloud dispatcher exists by default.

**Consequences:** Restart recovery is deterministic and unit tests require no network.
The local file adapter coordinates processes sharing one checkout; a later GitHub
adapter must provide equivalent compare-and-set semantics across hosts. Provider and
merge integrations require separate approved tickets.

**Alternatives considered:** A long-running agent would make memory authoritative.
Redis or a database would add infrastructure before it is needed. A provider-specific
LLM loop or autonomous merge would expand scope and security/cost authority.

Related ticket: T-0005.
