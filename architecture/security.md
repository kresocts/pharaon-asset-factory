# Security and Cost Controls

This policy applies to current development and future GPU/cloud execution.

## Secrets and permissions

- Never store secrets in Git, committed configuration, images, fixtures, tickets, or logs.
- Supply credentials at runtime through an approved secret store or environment mechanism.
- Use separate, least-privilege credentials for GitHub, registries, cloud providers, and artifact storage.
- Workers must not receive permissions they do not need. Generation workers should not have repository administration or billing permissions.
- Sanitize logs, error messages, command output, metadata, and packaged artifacts before persistence.

## Paid resource guardrails

- Require explicit user confirmation through an approved workflow before creating any paid resource.
- Enforce a maximum hourly GPU price, maximum runtime, idle timeout, and per-session/per-job budget before provisioning.
- Estimate cost before confirmation and account for actual cost afterward.
- Do not use uncontrolled retries. Every retry policy needs a finite attempt count, backoff, and cost-aware termination condition.
- Automatically tear down instances after success, terminal failure, timeout, cancellation, or control-plane recovery.
- Teardown should be idempotent and independently verifiable.

## Artifacts and lifecycle

- Transfer outputs to secured durable storage and verify integrity before destroying an instance.
- Treat prompts, reference images, generated models, metadata, and logs as potentially sensitive.
- Minimize retention and document ownership, access, encryption, and deletion policies before production use.
- Distinguish infrastructure failures (capacity, network, provider, provisioning) from implementation or model failures so retries and billing decisions are safe.
