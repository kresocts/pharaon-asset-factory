# T-0016 local publisher integration validation

Status: PASS
Commit tested: 13efe053e38762aa0504e34a1e0c9a6f3bdc8137
Branch: ticket/T-0016-local-publisher-integration-validation

## Environment

- Docker client: 29.5.3
- Docker server OSType: linux
- Buildx: github.com/docker/buildx v0.34.1-desktop.1 c79576280a671664e17eb68da98ec3136b614aed
- Builder: desktop-linux
- D drive free before build: 646625292288 bytes
- D drive free after build: 646625292288 bytes
- Registry image: registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
- Loopback binding: 127.0.0.1:50501->5000

## Build

- Exact command: Invoke-PublisherBuildxBuild --file docker/Dockerfile --platform linux/amd64 --provenance=false --sbom=false --push
- Start: 2026-08-14T13:17:50.4282034+02:00
- End: 2026-08-14T13:18:04.2470019+02:00
- Duration: 13.82 seconds
- Cache observation: Buildx used the existing default builder without --no-cache, prune, or external cache export. Exact cache reuse was not independently measured from raw logs.

## Tags and digest

- SHA-like tag: sha-13efe053e38762aa0504e34a1e0c9a6f3bdc8137
- Release-like tag: v0.0.0-rc.1
- Metadata digest: sha256:ac337e9cfef74160a9bd5afd49a831cfc8cbb187ef992d3fab6bd65b4e48adda
- SHA tag digest match: PASS
- Release tag digest match: PASS
- Platform verification: Linux AMD64 PASS
- Digest-qualified reference: 127.0.0.1:50501/pharaon-asset-factory@sha256:ac337e9cfef74160a9bd5afd49a831cfc8cbb187ef992d3fab6bd65b4e48adda

## Existing-tag preflight

- Before build: both tags classified ABSENT.
- After push: both tags classified EXISTING.
- Second preflight refusal: PASS (Requested tag already exists and publication is refused: sha-13efe053e38762aa0504e34a1e0c9a6f3bdc8137)
- Observed absence output:
  - SHA tag exit code: 1
  - SHA tag first line: no such manifest: 127.0.0.1:50501/pharaon-asset-factory:sha-13efe053e38762aa0504e34a1e0c9a6f3bdc8137
  - Release tag exit code: 1
  - Release tag first line: no such manifest: 127.0.0.1:50501/pharaon-asset-factory:v0.0.0-rc.1

## Runtime checks

- Pull by digest: PASS
- health --json --network none: exit 0
- ready --profile cpu --json --network none: READY
- Weight state: ABSENT, no detected files
- models plan --json --network none: offline PASS
- models status --json --network none: offline PASS

## Cleanup and integrity

- Registry container removed: True
- Loopback port closed: True
- Ticket-owned temporary directory removed: True
- Normal Docker config unchanged: True
- Shared Docker Buildx cache available: True
- Pulled digest image removed: True
- No GHCR authentication, request, or publication occurred.
- Self-hosted runner remained offline and unused.
- No model weights were downloaded.
- No paid or cloud resources were used.
