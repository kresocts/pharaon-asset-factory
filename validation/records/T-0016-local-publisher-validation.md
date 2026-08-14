# T-0016 local publisher integration validation

Status: PASS
Commit tested: 1d1dd9f847562e6ae2efeb077401fd72af890f43
Branch: ticket/T-0016-local-publisher-integration-validation

## Environment

- Docker client: 29.5.3
- Docker server OSType: linux
- Buildx: github.com/docker/buildx v0.34.1-desktop.1 c79576280a671664e17eb68da98ec3136b614aed
- Builder: desktop-linux
- D drive free before build: 646625292288 bytes
- D drive free after build: 646625292288 bytes
- Registry image: registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
- Loopback binding: 127.0.0.1:49672->5000

## Build

- Exact command: shared Invoke-PublisherBuildxBuild with --file docker/Dockerfile --platform linux/amd64 --provenance=false --sbom=false --push, SHA/release tags, OCI labels, temporary Docker config, and no cache-pruning flags.
- Start: 2026-08-14T12:55:01.8695445+02:00
- End: 2026-08-14T12:55:15.2907401+02:00
- Duration: 13.42 seconds
- Cache observation: Buildx used the existing default builder without --no-cache, prune, or external cache export. Exact cache reuse was not independently measured from raw logs.

## Tags and digest

- SHA-like tag: sha-1d1dd9f847562e6ae2efeb077401fd72af890f43
- Release-like tag: v0.0.0-rc.1
- Metadata digest: sha256:c4ddc3b66a5575d264f2cfc24c61aa88a28a3bcb70470ba1e90c0646752b13d2
- SHA tag digest match: PASS
- Release tag digest match: PASS
- Platform verification: Linux AMD64 PASS
- Digest-qualified reference: 127.0.0.1:49672/pharaon-asset-factory@sha256:c4ddc3b66a5575d264f2cfc24c61aa88a28a3bcb70470ba1e90c0646752b13d2

## Existing-tag preflight

- Before build: both tags classified ABSENT.
- After push: both tags classified EXISTING.
- Second preflight refusal: PASS (Requested tag already exists and publication is refused: sha-1d1dd9f847562e6ae2efeb077401fd72af890f43)
- Observed absence output shape:
  - SHA tag exit code: 1
  - SHA tag first line: no such manifest: 127.0.0.1:49672/pharaon-asset-factory:sha-1d1dd9f847562e6ae2efeb077401fd72af890f43
  - Release tag exit code: 1
  - Release tag first line: no such manifest: 127.0.0.1:49672/pharaon-asset-factory:v0.0.0-rc.1

## Runtime checks

- Pull by digest: PASS
- health --json --network none: exit 0
-
- ready --profile cpu --json --network none: READY
- Weight state: ABSENT, no detected files
- models plan --json --network none: offline PASS
- models status --json --network none: offline PASS

## Cleanup and integrity

- Registry container removed: PASS
- Localhost port closed after cleanup: PASS
- Ticket-owned temporary directory removed: PASS
- Normal Docker config unchanged: PASS
- Shared Docker Buildx cache remains available: PASS
- No GHCR authentication, request, or publication occurred.
- Self-hosted runner remained offline and unused.
- No model weights were downloaded.
- No paid or cloud resources were used.
