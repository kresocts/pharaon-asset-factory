# T-0018 local publisher exit validation

Status: PASS
Commit tested: f1b2391b6bf26efe5117d2e80184ef4e3c6c3ae7
Branch: ticket/T-0018-normalize-powershell-native-exit-state

## Root cause

`Test-PublisherRegistryTagState` captured the native `docker manifest inspect` exit
code for diagnostics but did not normalize `$LASTEXITCODE` after classifying the result
as an expected absent tag. GitHub Actions' `pwsh` step then used that stale non-zero
exit state as the final process exit code even though the absence was correctly
handled.

## Implemented fix

For only the narrow, allowlisted expected-absence case, the new explicit
`Reset-PublisherLastExitCodeAfterAbsence` helper assigns `$global:LASTEXITCODE = 0`
before `Test-PublisherRegistryTagState` returns. Existing-tag and real-error paths
return without this normalization, so they remain fail-closed.

## Before-fix process-level reproduction

- Native mock exit code: 1
- Observed output signal: no such manifest: mock
- Classifier state: Absent
- `$LASTEXITCODE` after classifier: 1
- GitHub-like shell epilogue exit code: 1

## After-fix process-level regression

- Native mock exit code: 1
- Observed output signal: no such manifest: mock
- Classifier state: Absent
- `$LASTEXITCODE` after classifier: 0
- GitHub-like shell epilogue exit code: 0

## Disposable loopback registry validation

- Registry image: registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
- Registry container: pharaon-t0018-registry-2ba9c72ae8214ff2aa25c682b29d4672
- Loopback binding: 127.0.0.1:55965->5000
- Absent reference: 127.0.0.1:55965/pharaon-asset-factory:sha-f1b2391b6bf26efe5117d2e80184ef4e3c6c3ae7
- Raw Docker exit code: 1
- Raw Docker output first line: no such manifest: 127.0.0.1:55965/pharaon-asset-factory:sha-f1b2391b6bf26efe5117d2e80184ef4e3c6c3ae7
- Classifier state: Absent
- `$LASTEXITCODE` after classifier: 0
- Complete PowerShell process exit code: 0
- Registry container removed: True
- Loopback port closed: True
- Ticket-owned temporary directory removed: True
- Buildx cache unchanged: True

## Actual-error fail-closed validation

- Invalid/unreachable reference: 127.0.0.1:1/pharaon-asset-factory:sha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
- Classifier state: Error
- `$LASTEXITCODE` after classifier: 1
- Complete PowerShell process exit code: 1

## Safety confirmations

- No production image build occurred.
- No GHCR authentication, request, or publication occurred.
- No GHCR image name was used.
- No model weights were used.
- No Docker prune or BuildKit cache deletion occurred.
- Self-hosted runner remained offline and unused.
- No GitHub Actions workflow was triggered.
