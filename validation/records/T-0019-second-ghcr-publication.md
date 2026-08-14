Status: PASS

# T-0019 second controlled GHCR publication evidence

This record reflects observed facts only from the single controlled T-0019
publication.

## Publication identity

- Publication UTC time: 2026-08-14T13:54:26Z
- Exact main SHA: aa90c10c24921b2cdf065a601b31b1d19642179a
- HEAD equals origin/main: Yes
- Workflow run ID: 31806906600
- Workflow run URL: https://github.com/kresocts/pharaon-asset-factory/actions/runs/31806906600

## Preflight

- PowerShell version: 7.6.4
- Docker version: 29.5.3
- Docker OSType: linux
- Buildx version: v0.34.1-desktop.1 c79576280a671664e17eb68da98ec3136b614aed
- D-drive free space: 602.21 GiB
- Runner status before dispatch: Offline
- Unexpected self-hosted jobs absent: Yes

## Exact workflow inputs

- branch: main
- confirm_publish: PUBLISH
- expected_sha: aa90c10c24921b2cdf065a601b31b1d19642179a
- release_tag: (empty)

## Environment and runner

- Environment approval: Completed by owner
- Runner status before startup: Offline
- Runner manually started: Yes, by owner
- Runner manually stopped: Yes, by owner
- Runner final status Offline: Yes

## Workflow result

- Workflow conclusion: success
- Failed step, if any: None
- SHA tag: sha-aa90c10c24921b2cdf065a601b31b1d19642179a
- Digest: sha256:dca9710dc3b83e350b89223fd4c7d4a21feb624100b051acb896409c66caca54
- Digest-qualified reference: ghcr.io/kresocts/pharaon-asset-factory@sha256:dca9710dc3b83e350b89223fd4c7d4a21feb624100b051acb896409c66caca54
- Linux AMD64 result: Pass, verified by the successful Assert-PublisherPushedDigest and Assert-PublisherLinuxAmd64 workflow step
- Release tag absent: Yes
- Latest tag not created by this run: Yes

## GHCR and safety observations

- Package visibility observed: Not changed automatically by this run; direct GitHub API read was unavailable because the current token lacks the read:packages scope
- Credential cleanup result: Success; temporary Docker config and build metadata cleanup step completed successfully
- No weights: Yes
- No paid/cloud resources: Yes
- No automatic retry: Yes

## Final result

- Final result: PASS