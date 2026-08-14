# ADR 0006: External model cache and controlled acquisition

**Status:** Accepted

**Context:** T-0013 proved the pre-weights runtime is ready but left `/models` empty
and unmanaged. Future Hunyuan weights are large, licensed separately, and must be
acquired deliberately; unplanned or unlimited downloads would be a security and cost
risk. T-0014 must provide a deterministic acquisition framework without downloading
any real production model data.

**Decision:** Introduce one canonical `models` command through the container
entrypoint with `plan`, `status`, `acquire`, and `verify` subcommands. Artifacts are
described by versioned manifests (schema version 1) that require immutable source
URLs, exact expected byte sizes, and lowercase SHA-256 checksums, with destinations
validated under the external cache root (`MODEL_CACHE_DIR`, default `/models`).
Planning, status, and verification are offline and deterministic. Acquisition
requires explicit `--confirm-download` and a mandatory `--max-bytes` allowance, and
performs no network access until the manifest, cache state, byte budget, file-count
policy, and artifact-set lock all pass. Downloads stream into a `.part` temporary
file, verify exact size and SHA-256, and promote atomically; verified files are reused
without network access. Retries (2) and timeouts (10 s connect, 30 s read) are finite,
and integrity and permanent 4xx failures are never retried. A per-destination atomic
lock under the cache serializes acquisition with a bounded wait; every lock carries a
unique owner token, `touch()`/`release()` act only while the token still matches,
automatic stale-lock removal is disabled, and stale locks are removed manually by an
operator after confirming no active acquisition. Every subcommand emits versioned
JSON with a fixed exit-code contract. Docker build, startup, health, and readiness
remain download-free. T-0014 validation uses only tiny local fixtures; production
Hunyuan manifests and the first real acquisition are deferred to a later explicitly
approved ticket.

**Consequences:** Operators and future controllers get a scriptable, budgeted,
integrity-checked model-cache boundary without startup downloads or accidental
spending. The manifest, lock, and exit-code contracts become stable interfaces that
must be versioned and documented before breaking changes. Automatic stale-lock
removal is disabled: a crashed or stale lock remains until an operator removes it
manually after confirming no active acquisition, and conflicts return `LOCK_CONFLICT`
after the bounded wait.

**Alternatives considered:** Downloading weights during the image build would bake
licensed mutable data into layers and violate the external-cache boundary. A startup
or background downloader would spend bytes without explicit authorization. A
manifest-less "download this URL" tool would have no checksums, sizes, byte budgets,
or destination containment. Relying on Hugging Face client behavior would couple the
framework to a specific hub and authentication model.

Related ticket: T-0014.