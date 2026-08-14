# ADR 0007: Secure Windows GHCR publishing workflow

**Status:** Accepted

**Context:** T-0015 must create a publish path for
`ghcr.io/kresocts/pharaon-asset-factory` on a public repository with a dedicated
offline self-hosted Windows runner. Mutable tags, automatic triggers, broad token
permissions, reused Docker credentials, and uncontrolled runner execution are all
publishing-security risks. The workflow must be defined and statically validated now
but executed only by later tickets.

**Decision:** Add one canonical `publish-container.yml` workflow with a
`workflow_dispatch`-only trigger, a protected `ghcr-publish` environment, and the
dedicated runner labels `self-hosted`, `Windows`, `X64`, and `pharaon-publisher`.
Require exact `confirm_publish=PUBLISH` and an `expected_sha` equal to the full
lowercase `github.sha`. Fail closed unless the repository and ref are exactly
`kresocts/pharaon-asset-factory` and `refs/heads/main`. Pin every action to a verified
full commit SHA. Limit permissions to `contents: read` and `packages: write`, use only
`secrets.GITHUB_TOKEN` with `--password-stdin`, and keep credentials in a unique
temporary Docker config under `RUNNER_TEMP` with an `if: always()` cleanup. Check
Docker Linux-engine, Buildx, `D:\actions-runner`, and a conservative 150 GiB D-drive
threshold before login/build. Refuse existing requested tags before Buildx, always
publish an immutable `sha-<full-sha>` tag, optionally publish a strictly validated
immutable release tag, and never publish rolling tags. Use a 180-minute timeout and a
non-cancelling global publication concurrency group. Build with Buildx for
`linux/amd64` only, direct registry push, provenance/SBOM disabled, no cache export, no
build secrets, and no model credentials. Capture and verify the pushed digest, remote
SHA/release tags, and Linux AMD64 platform, and record the digest-qualified reference
in the job summary.

**Consequences:** The workflow has a narrow, reviewable manual publication boundary
without automatic publication, PATs, paid runners, or mutable tags. Because it is
manual-only and statically validated, T-0016 can integration-test the logic locally and
T-0017 can perform the first controlled publication with independent review. Registry
tag creation is not fully atomic; preflight plus non-cancelling concurrency reduces but
does not eliminate a race with another publisher. Package visibility remains a manual
GitHub Packages setting.

**Alternatives considered:** A push-triggered publish would reduce manual friction but
creates an automatic publication path on a public repository. Reusing the default
Docker config would risk credential leakage across jobs. Mutable or rolling tags would
undermine reproducibility. Broad workflow permissions or an ungated
build/login/push job would expose unnecessary authority to a self-hosted runner.

Related ticket: T-0015.
