# Hunyuan dependency image

T-0011 extends the T-0010 CUDA/Python foundation with pinned PyTorch and Hunyuan3D 2.1 software dependencies. It contains no model weights, automatic downloads, compiled Hunyuan rendering extensions, inference, service, or cloud/provider logic.

## Immutable version contract

| Component | Pinned value |
| --- | --- |
| CUDA base | `nvidia/cuda:12.4.1-devel-ubuntu22.04` at digest `sha256:da6791294b0b04d7e65d87b7451d6f2390b4d36225ab0701ee7dfec5769829f5` |
| Python | 3.10 in `/opt/venv` |
| PyTorch stack | `torch==2.5.1`, `torchvision==0.20.1`, `torchaudio==2.5.1` from `https://download.pytorch.org/whl/cu124` |
| Hunyuan source | Official Tencent repository at `82920d643c0dc2f7bfd7255f45f62d386edfe60c`, installed in `/opt/hunyuan3d` |

The build fetches only the full Hunyuan commit, verifies `HEAD` and the pinned upstream `requirements.txt` SHA256 (`ce8954023db68966a6fb2b80253b24c9da81ae48ee9d96f8f4a8d98b4cead289`), records `/opt/hunyuan3d.commit`, and removes `.git`. OCI labels repeat immutable version and purpose data.

To upgrade intentionally, inspect official guidance and the candidate commit, then update `HUNYUAN_COMMIT`, the upstream-requirements checksum, health default, tests, this guide, and ADR 0003 together. Repeat CPU/GPU validation. Never substitute mutable `main`.

## Dependency strategy

The verified upstream file remains at `/opt/hunyuan3d.requirements.upstream.txt`. The local lock removes Tencent/Aliyun mirror directives, retains upstream pins, normalizes `bpy==4.0` to `bpy==4.0.0`, and freezes upstream's four unversioned entries as `timm==1.0.15`, `pythreejs==2.4.2`, `torchdiffeq==0.2.5`, and `deepspeed==0.17.1`. Standard PyPI is used except for Blender's official package index, required for `bpy`. The build runs `pip check` and records `/opt/hunyuan3d.dependencies.freeze.txt`. The top-level lock does not constitute a hash-locked transitive wheelhouse, so package-index availability remains an external dependency.

Real build validation required three explicit compatibility accommodations. The official cu124 index remains the primary PyTorch source, while standard PyPI is an extra source because the cu124 index's current NVIDIA dependency link does not expose the pinned `nvidia-cudnn-cu12==9.1.0.70`; build assertions still require `torch==2.5.1+cu124` and CUDA 12.4. The Linux Blender wheel needs small X11 runtime libraries for deterministic `bpy` import. Finally, `pip check` currently emits only its known platform-tag false positives for `bpy==4.0.0` and `ninja==1.11.1.1`; the build rejects any other issue and separately imports both packages.

## Build and diagnostics

```bash
docker build --pull --tag pharaon-asset-factory-gpu:t0011 --file docker/Dockerfile .
docker run --rm pharaon-asset-factory-gpu:t0011 health --json
docker run --rm pharaon-asset-factory-gpu:t0011 dependency-smoke
```

CPU health must exit zero with `HUNYUAN_DEPENDENCIES_READY`; `GPU_NOT_AVAILABLE` and `torch.cuda.is_available() == false` are expected without GPU passthrough. Dependency smoke imports torch, torchvision, torchaudio, transformers, diffusers, accelerate, trimesh, and numpy with process-local offline guards. It loads no model pipeline and downloads nothing.

Health reports Python, CUDA runtime/compiler data, GPU visibility, exact PyTorch versions, `torch.version.cuda`, `torch.cuda.is_available()`, GPU name, Hunyuan source/revision, dependency imports, native extensions, external model cache, and model-weight state. This layer always reports `full_hunyuan_ready: false`. Expected states are `CUSTOM_RASTERIZER_NOT_BUILT_EXPECTED`, `DIFFERENTIABLE_RENDERER_NOT_BUILT_EXPECTED`, and `MODEL_WEIGHTS_NOT_PRESENT_EXPECTED`.

For real PyTorch CUDA validation:

```bash
docker run --rm --gpus all pharaon-asset-factory-gpu:t0011 gpu-smoke
docker run --rm --gpus all pharaon-asset-factory-gpu:t0011 health --require-gpu --json
```

The GPU smoke test allocates a small CUDA tensor, performs an operation, synchronizes, and reports the NVIDIA device. It does not run Hunyuan inference.

## Runtime layout and boundary

| Path | Purpose |
| --- | --- |
| `/app` | Small Pharaon diagnostics |
| `/opt/hunyuan3d` | Immutable third-party source |
| `/models` | Unchanged external future model cache |
| `/data/input`, `/data/output`, `/workspace` | Job data and temporary workspace |

Startup never downloads into `/models`. The process remains fixed unprivileged user/group `10001:10001`. T-0012 will compile the custom rasterizer and DifferentiableRenderer; later work will define licensed weight acquisition. Until then, do not claim that Hunyuan generation works.

## T-0012 native extension layer

The image now bakes both native components from Hunyuan commit `82920d643c0dc2f7bfd7255f45f62d386edfe60c` into a Docker build layer:

- `custom_rasterizer` is built as a normal wheel with the pinned torch environment visible through `--no-build-isolation`; upstream `setup.py` imports torch as an undeclared build dependency, so isolated metadata/build cannot work.
- The exact upstream `hy3dpaint/DifferentiableRenderer/compile_mesh_painter.sh` emits `/opt/hunyuan3d/hy3dpaint/DifferentiableRenderer/mesh_inpaint_processor.cpython-310-x86_64-linux-gnu.so`.
- `/opt/hunyuan3d.native-artifacts.txt` records the rasterizer kernel and mesh-painter shared libraries.

The single-stage CUDA development image is retained because the base was intentionally selected for native compilation and the toolchain is part of its diagnostic contract. CUDA compilation targets `8.6;8.9`: RTX 3090 and RTX 4060/4090-class architectures. Only the local RTX 4060 is empirically tested here.

Build and run diagnostics with:

```bash
docker build --no-cache --tag pharaon-asset-factory-gpu:t0012 --file docker/Dockerfile .
docker run --rm pharaon-asset-factory-gpu:t0012 health --json
docker run --rm pharaon-asset-factory-gpu:t0012 native-smoke
docker run --rm --gpus all pharaon-asset-factory-gpu:t0012 native-smoke --require-gpu-operation
docker run --rm --gpus all pharaon-asset-factory-gpu:t0012 health --require-gpu --require-native-gpu --json
```

Without GPU passthrough, ordinary health and `native-smoke` load both extensions and run the renderer's safe CPU pybind11 operation; they do not invoke CUDA automatically. Health reports `HUNYUAN_NATIVE_EXTENSIONS_READY`, installed/importable artifacts, `GPU_NOT_AVAILABLE`, absent weights, and `full_hunyuan_ready: false`. The GPU smoke rasterizes one synthetic triangle through the actual custom CUDA kernel. No command downloads weights or accesses a model hub.

Model snapshots, Hunyuan checkpoints, Real-ESRGAN, and other inference assets remain absent. Asset generation and full Hunyuan inference are still unavailable.


## T-0013 runtime readiness gate

The `ready` command is the canonical machine-readable pass/fail gate for the next deployment stage. It answers whether the container runtime is technically ready before model weights are acquired. It never downloads weights, accesses a model hub, or executes inference.

### Canonical commands

```bash
docker run --rm IMAGE ready --profile cpu --json
docker run --rm --gpus all IMAGE ready --profile native-gpu --json
```

Run with `--network none` for validation; both profiles are network-independent.

### Profiles

- `cpu`: validates the container structure without GPU access. It checks the Python version, runtime configuration, pinned PyTorch import/version, Hunyuan source and revision, representative dependency imports, native extension artifacts and imports, the renderer CPU native operation, required path existence/writability, external model cache, and weight state. GPU absence is expected and does not fail this profile.
- `native-gpu`: includes the CPU checks plus NVIDIA GPU visibility, `torch.cuda.is_available()`, a PyTorch CUDA tensor operation, custom rasterizer imports, DifferentiableRenderer imports, and the real custom rasterizer CUDA smoke operation. A missing GPU is a clean `NOT_READY`, not an internal crash.

### JSON schema

`schema_version` is `1`. The JSON report contains the requested profile, `status` (`READY`/`NOT_READY`), boolean `ready`, `classification`, `exit_code`, `checks`, `facts`, and `failure_summary`. Each check has a stable `id`, `status` (`PASS`/`FAIL`), and an actionable `message`. Check identifiers include `python.version`, `torch.import`, `torch.version`, `hunyuan.source`, `hunyuan.revision`, `dependencies.imports`, `native.artifacts`, `native.custom_rasterizer.import`, `native.renderer.import`, `native.renderer.operation`, `paths.models.exists`, `paths.models.writable`, `paths.input.writable`, `paths.output.writable`, `paths.workspace.writable`, `paths.hunyuan_source.exists`, `model.cache.external`, `weights.present`, `inference.full_ready`, `gpu.visible`, `torch.cuda.available`, `torch.cuda.operation`, and `native.custom_rasterizer.operation`. Timestamps are omitted for determinism.

### Exit-code contract

- `0`: requested profile is ready.
- `2`: expected readiness requirements were not met.
- `3`: diagnostic/internal execution error.
- `64`: invalid command-line usage.

### Expected pre-weights state

For T-0013, model weights are normally `ABSENT`; this does not fail either profile. The report sets `facts.weights.state` to `ABSENT` (or `PRESENT_UNVERIFIED`) and `facts.inference.full_ready` to `false`. Full inference readiness remains false because weights and inference are intentionally absent.

### Path checks

The gate probes `/models`, `/data/input`, `/data/output`, `/workspace`, and the Hunyuan source path. Writable paths are probed with a temporary file that is removed afterward. The model cache must remain outside `/app` and the Hunyuan source.

### Difference from health and inference

- `health` is a diagnostic report, not a pass/fail gate.
- `ready` is the authoritative runtime pass/fail gate for the pre-weights stage.
- Full Hunyuan inference readiness is a later concern and is always reported false by this ticket.

## T-0014 external model cache and controlled acquisition

The `models` command is the canonical container interface for planning, inspecting,
acquiring, and verifying future model artifacts in the external `/models` cache. It
exists so that a later ticket can introduce the first verified production Hunyuan
manifest and perform an explicitly approved, budgeted download. T-0014 itself contains
no production model weights and all download validation uses deterministic tiny local
fixtures.

### Canonical commands

```bash
docker run --rm IMAGE models plan --manifest MANIFEST.json --json
docker run --rm IMAGE models status --manifest MANIFEST.json --json
docker run --rm IMAGE models acquire --manifest MANIFEST.json --confirm-download --max-bytes N --json
docker run --rm IMAGE models verify --manifest MANIFEST.json --json
```

`plan`, `status`, and `verify` are fully offline and deterministic; they never open a
network connection. Only `acquire` may access the network, and only when the operator
passes both `--confirm-download` and `--max-bytes` and every preflight check passes.

### Manifest format

Manifests use schema version `1`:

```json
{
  "schema_version": 1,
  "artifact_set": "hunyuan3d-2-1",
  "revision": "e6e8b8c8d8e8f8a8b8c8d8e8f8a8b8c8d8e8f8a8",
  "namespace": "hunyuan3d-2-1/e6e8b8c8d8e8f8a8b8c8d8e8f8a8b8c8d8e8f8a8",
  "description": "Example immutable placeholder manifest",
  "files": [
    {
      "path": "data/placeholder-artifact.dat",
      "url": "https://example.invalid/placeholder-artifact.dat",
      "size": 123456789,
      "sha256": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2",
      "role": "placeholder-artifact"
    }
  ]
}
```

Every file requires an immutable `url`, an exact expected `size` in bytes, and an exact
lowercase `sha256`. Mutable references such as `main`, `latest`, `master`, `HEAD`,
`/resolve/main/`, or `/blob/main/` URLs are rejected, as are missing or invalid
checksums, missing or non-positive sizes, absolute or `..` traversal paths, duplicate
destinations, unsupported URL schemes, and URLs with embedded credentials. Production
sources must use HTTPS; HTTP is accepted only for loopback and `host.docker.internal`
test fixtures. Mutable-reference checks run against the recursively percent-decoded
URL path, so single- and double-encoded forms such as `/resolve/%6dain/` or
`/resolve/%256dain/` are rejected too; malformed percent escapes are rejected,
non-empty URL fragments are refused, and targeted revision-like query parameters
(`revision`, `rev`, `ref`, `branch`, `tag`) are rejected when they carry mutable
values (`main`, `master`, `latest`, `head`), while other query strings (including
signed HTTPS parameters) are preserved. Destinations are compared case-insensitively
for cross-platform determinism (so `A.bin` and `a.bin` are rejected as ambiguous), a
file destination cannot be an ancestor of another destination, acquisition-owned
temporary names use an underscore prefix that is not representable as a manifest
destination, and every existing symlink in a destination ancestor below the cache
root is rejected â€” including symlinks that resolve to another location inside the
same cache, because such internal aliases would let different namespace locks
manipulate one physical destination. Redirects are followed only when the target still obeys the same
policy: unsupported schemes, embedded credentials, and mutable source references are
refused; HTTPS sources never downgrade to HTTP and never redirect into loopback or
test hosts; loopback/test HTTP redirects are allowed only for already-allowed
loopback/test fixture URLs. The destination namespace and every file path are
validated so nothing can be written outside the configured model-cache root.

### Cache layout

The cache root comes from `MODEL_CACHE_DIR` (default `/models`). Artifacts are stored
under an immutable destination namespace:

```text
/models/<namespace>/<relative-file-path>
```

Namespaces should include the artifact set and immutable revision (for example
`hunyuan3d-2-1/<revision>`). Model data never lands in `/app`, `/opt/hunyuan3d`, or the
image layers. The bind-mounted `/models` volume remains writable by the fixed runtime
user/group `10001:10001`.

### Authorization and byte limits

`models acquire` is refused with exit code `2` and zero network requests unless the
operator provides both `--confirm-download` and a mandatory `--max-bytes` allowance.
Before the first network request the command validates the complete manifest, inspects
the current cache, computes the bytes still required, verifies that the required amount
and file count fit the configured policy limits, and acquires the artifact-set lock. A
missing confirmation or insufficient allowance is a policy refusal, never an internal
exception. There is no environment-variable bypass and no unlimited-byte mode.
`--max-bytes` is a hard cap on the total number of response-body bytes received across
all artifacts, all attempts, and all retries; bytes received during failed or
interrupted attempts are never refunded, and the command never reports success after
exceeding the cap. The cumulative received-body count is reported in
`network.bytes_received`. Bounded acquisition requires an exact `Content-Length`
matching the manifest size: responses without a `Content-Length` or with chunked
`Transfer-Encoding` are refused before any body byte is consumed, every accepted read
is bounded by the remaining expected size, and every consumed byte is accounted.
Redirect response bodies are consumed in bounded chunks against the same shared
budget, so a redirect cannot bypass `--max-bytes` or under-report `bytes_received`.
Before any final artifact body byte is consumed, the expected artifact size must fit
within the remaining allowance; every final-body read is bounded by the remaining
expected size and the remaining allowance, and `network.bytes_received` can never
exceed `--max-bytes`.

### Retries and timeouts

Transport behavior is finite and documented: connection establishment is bounded by a
10-second connect timeout, each socket read by a 30-second read timeout, and at most 2
retries (3 attempts total) are made for transient failures (connection errors,
timeouts, HTTP 408/429, and HTTP 5xx). A connection that closes before the declared
body is fully received is treated as a transient transport interruption and follows
the same bounded retry policy. Permanent HTTP 4xx errors and integrity failures are
never retried. Local filesystem failures (temporary-file creation, write, flush,
fsync, promotion, and permission errors) are never retried as transport failures;
they are reported with the stable `LOCAL_IO_FAILURE` classification and exit code
`70` after at most one network attempt. Retry attempts are visible in the
machine-readable `network.retries`, `network.requests_attempted`, and
`network.bytes_received` fields. `network.requests_attempted` counts every HTTP
exchange actually attempted, including the initial artifact request, every
followed redirect request, and every retry; refused redirects do not add a
target request.

### Streaming, atomicity, and states

Downloads stream in bounded chunks into a genuinely unique acquisition-owned
temporary file (`_acq-<token>.part`) in the final destination directory, never into
memory. The underscore prefix is not representable as a manifest destination, so a
final artifact can never collide with or be mistaken for another artifact's temporary
file. The temporary file is created with exclusive, no-follow flags
(`O_CREAT|O_EXCL|O_NOFOLLOW`), flushed and synced, then promoted to the final path
with an atomic rename only after exact size and SHA-256 verification succeed.
Acquisition-owned temporary files are removed after success, and the complete
manifest is re-verified under the lock before success is reported. A stale reserved
temporary path (`_acq-*.part`) that is a symlink (broken or not), a directory, a
device, or any other non-regular entry is reported `CORRUPTED` by status/verify, and
acquisition refuses it with `LOCAL_IO_FAILURE` before any network request. Safe stale
regular temporary files are removed under the namespace lock as part of the
restart-from-zero policy; a cleanup failure or any remaining reserved temporary path
prevents success, and existing verified finals are never touched. Corrupted,
incomplete, oversized, or checksum-mismatched content is never reported as valid and
never leaves a verified final file behind.

Per-file states are `ABSENT`, `PARTIAL`, `CORRUPTED`, and `VERIFIED`. `PARTIAL` means an
incomplete `.part` download file exists; `CORRUPTED` means the final file exists but
fails size or checksum verification. `models verify` performs no download and exits
non-zero until every file is `VERIFIED`. T-0014 uses a documented restart-from-zero
policy: a stale `.part` is removed and re-downloaded on the next authorized
acquisition, and a corrupted final file is replaced only after a fully verified fresh
download. Existing verified files are always reused without network access or rewrites.
A destination that already exists as a directory or another non-regular file is refused
as a path-policy failure before any network request.

### Locking

Acquisition is serialized per destination namespace with an atomic lock directory
under `<cache root>/.locks/<first namespace component>`. Any manifests that can write
overlapping destination paths share the same lock even when their URLs, hashes, roles,
or plan digests differ; the complete-manifest `plan_id` remains reported separately.
Every successful acquisition writes a unique unpredictable `owner_token` into
`owner.json`; `touch()` refreshes the heartbeat and `release()` removes the lock only
while `owner.json` still contains the object's own token, so a replaced lock
generation is never touched or deleted. Automatic stale-lock removal is disabled:
stale locks are removed manually by an operator after confirming no active
acquisition. Lock acquisition waits at most 10 seconds and then fails cleanly with
exit code `6` and `LOCK_CONFLICT` classification (including a hint when the existing
lock appears stale), preserving the full manifest and cache context (artifact
identity, plan id, per-file states, byte totals, and cache root) in the JSON
response. The lock holder refreshes the owner heartbeat during long downloads and
removes its own lock on completion.

### JSON and exit-code contract

Every subcommand emits versioned JSON (`schema_version: 1`) with the command, artifact
identity, plan digest, cache root, file counts, byte totals, per-file states, network
request/retry/received-byte counts, success flag, classification, exit code, and an
actionable message. No credentials, tokens, or environment dumps are emitted.

- `0` operation succeeded
- `2` policy refusal (missing confirmation or insufficient byte allowance)
- `3` manifest validation or destination path-security failure
- `4` integrity verification failure
- `5` transport failure
- `6` lock/concurrency conflict
- `64` invalid CLI usage
- `70` internal error or local filesystem/IO failure (`LOCAL_IO_FAILURE`)

### Example with a local test manifest

```bash
# Generate a tiny fixture manifest that points at a local HTTP fixture server, then:
docker run --rm -v "$PWD/manifest.json:/manifests/fixture.json:ro" \
  -v "$PWD/models:/models" pharaon-asset-factory-gpu:t0014 \
  models acquire --manifest /manifests/fixture.json --confirm-download --max-bytes 1048576 --json
```

All T-0014 validation uses tiny deterministic fixtures served by a local HTTP server
that counts requests; total fixture transfers stay below 5 MiB. No production Hunyuan
manifests, model URLs, or checksums are included in T-0014, and no real weights or
checkpoints are downloaded.

## T-0015 GHCR publishing workflow foundation

T-0015 adds `.github/workflows/publish-container.yml`, a secure, manually triggered
foundation for publishing the reproducible GPU worker image to
`ghcr.io/kresocts/pharaon-asset-factory`. The workflow is not executed by T-0015.

### Purpose and boundary

The workflow defines the publishing path for the image built by `docker/Dockerfile`.
It did not start the self-hosted runner, perform Docker login, build or push an image,
run a local registry, or contact GHCR during T-0015. T-0016 completed local integration
testing of the publishing logic; T-0017 attempted the first controlled GHCR publication
and failed, and T-0019 completed the successful second controlled publication after
T-0018 fixed the exit-state blocker.

### Runner and environment

The job runs only on the dedicated self-hosted runner with labels:

- `self-hosted`
- `Windows`
- `X64`
- `pharaon-publisher`

The runner is installed at `D:\actions-runner`, is not a service, and is offline by
default. Do not start `run.cmd` as part of T-0015. The workflow uses the protected
`ghcr-publish` environment, which is restricted to `main` with a required reviewer and
no administrator bypass.

The Windows host must expose Docker Desktop with the Linux engine; the publishing job
rejects non-Linux Docker servers before login or build.

### Manual inputs

- `confirm_publish` â€” required string. No authorizing default. Must be exactly `PUBLISH`.
- `expected_sha` â€” required string. Must be exactly the current 40-character lowercase
  `github.sha`.
- `release_tag` â€” optional string. Empty means SHA-tag-only publication. Accepted
  examples include `v1.0.0`, `v1.2.3-rc.1`, and `v2.0.0-beta.2`. The value must match a
  strict Docker-compatible immutable version grammar. Uppercase, whitespace, slashes,
  shell metacharacters, build-metadata `+` suffixes, empty/trailing prerelease
  separators, excessively long values, and mutable names such as `latest`, `stable`,
  `current`, `main`, `master`, `dev`, `edge`, `nightly`, `rolling`, and `snapshot` are
  rejected before authentication or build.

### Trusted-context policy

The first PowerShell step fails closed unless the event is `workflow_dispatch`, the
repository is exactly `kresocts/pharaon-asset-factory`, the ref is exactly
`refs/heads/main`, `github.sha` is a 40-character lowercase hex value,
`expected_sha` equals `github.sha`, `confirm_publish` equals `PUBLISH`, the optional
release tag is valid, and the runner OS is Windows. Inputs are passed through
environment variables rather than embedded directly into PowerShell source.

### Image and tag policy

The image is fixed to `ghcr.io/kresocts/pharaon-asset-factory`. Every publication
generates the immutable tag `sha-<full-github-sha>`. An optional validated release tag
points to the same future digest. `latest`, `main`, `stable`, `dev`, and other rolling
tags are never published. The workflow inputs cannot change the registry, owner, image
name, Dockerfile, build context, platform, or runner.

### Existing-tag refusal

Before Buildx starts, the workflow inspects the required SHA tag and any optional
release tag through the temporary Docker config. If either tag already exists, the job
fails and reports the conflicting tag. Only a narrow allowlist of actual registry
absence signals is accepted as proof that a tag is absent: `MANIFEST_UNKNOWN`,
`NAME_UNKNOWN`, `manifest unknown`, and `no such manifest`. Authentication,
authorization, TLS, DNS, timeout, Docker component, credential-helper, and unknown
errors all fail closed. There is no force/overwrite input. Registry preflight plus the
non-cancelling concurrency group reduce but cannot make registry tag creation fully
atomic. T-0016 will empirically validate the exact local-registry and Docker outputs
before the first GHCR publication.

### Permissions and credentials

The workflow permissions are limited to `contents: read` and `packages: write`. Docker
authentication uses only `secrets.GITHUB_TOKEN` and `--password-stdin`. A unique
temporary Docker config directory is created under `RUNNER_TEMP`; the default user
Docker config is never used. The token is never echoed. An `if: always()` cleanup step
logs out through the temporary config and removes only that config and job metadata; it
does not delete images, caches, or unrelated files.

### Docker and disk preflight

Before login or build, the workflow checks that Docker is reachable, `docker info`
reports `OSType=linux`, Buildx is available, `D:\actions-runner` exists, and the D
drive has at least a trusted 150 GiB free. The threshold is declared as `[int64]150`,
converted to bytes with numeric multiplication, and compared against
`[int64]$drive.Free`; no environment-variable string is multiplied directly by `1GB`.
On insufficient capacity it fails clearly without logging in, building, pruning, or
selecting another runner. No Docker prune command is used.

### Timeout, concurrency, and action pinning

The job has a finite 180-minute timeout and a deterministic publication concurrency
group with `cancel-in-progress: false`. Every `uses:` reference in every repository
workflow is pinned to a verified full 40-character commit SHA; only allowlisted
official `actions/*` actions are used. The recorded release comments are exact:
`actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1` and
`actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0`.

### Build and digest verification

The future Buildx command uses the repository root as context, `docker/Dockerfile`,
`linux/amd64`, direct registry push, `provenance=false`, `sbom=false`, no multi-platform
build, no GHA persistent cache export, no build secrets, no model credentials, and no
GH token as a build argument. OCI labels record source, revision, version, and
description. Post-push steps capture the pushed `sha256` digest, inspect the SHA tag
remotely with `buildx imagetools inspect --format "{{json .}}"`, and parse that
documented JSON structurally with `ConvertFrom-Json`. The verifier compares
`.manifest.digest` with the digest captured from `build-metadata.json`, requires the
manifest platform and image config to be exactly Linux AMD64 when those documented
fields are present, verifies any optional release tag resolves to the same digest, and
writes the digest-qualified reference to the job summary. Because Buildx output can be
version-sensitive, T-0016 must validate this command against the disposable local
registry before the first GHCR publication.

### Package visibility and follow-up

Package visibility remains a manual GitHub Packages setting and is not changed by this
workflow. T-0016 completed local integration validation of the publishing logic. T-0017
attempted the first controlled GHCR publication and failed; T-0018 fixed the blocker
and T-0019 completed the successful second controlled publication. The implementation
worker must not approve or merge its own pull request.

## T-0016 local publisher integration validation

T-0016 validates the T-0015 publisher design against a disposable official Docker
Distribution registry bound only to `127.0.0.1`. It does not contact GHCR, start the
self-hosted runner, or trigger `publish-container.yml`.

### Exact local integration command

From the repository root on a clean `ticket/T-0016-*` branch with Docker Desktop in
Linux mode and at least 150 GiB free on drive D:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File validation/run_local_publisher_integration.ps1 -Confirmation "RUN LOCAL PUBLISHER TEST"
```

Use `-PreflightOnly` for a non-destructive environment and plan check without starting
the registry or building:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File validation/run_local_publisher_integration.ps1 -PreflightOnly
```

### Confirmation and policy

The real integration run requires the exact confirmation phrase
`RUN LOCAL PUBLISHER TEST`; the production GHCR workflow confirmation remains
`PUBLISH`. The disposable registry is official Docker Distribution image
`registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373`
and is published only to a loopback address. No insecure-registry daemon modification,
public/LAN binding, authentication service, or persistent registry volume is used.

### What it validates

- shared production publisher PowerShell logic and safe native argument splatting
- temporary Docker config isolation and unchanged normal Docker config
- SHA-like and `v0.0.0-rc.1` tag absence before build
- one real Buildx `linux/amd64` direct push to `127.0.0.1:<port>/pharaon-asset-factory`
- metadata digest extraction and both-tags-to-one-digest verification
- Linux AMD64 manifest verification
- pull by digest and offline health/readiness/model-plan/status checks
- existing-tag refusal without a second build
- deterministic cleanup of only ticket-owned resources

The build uses the existing local Buildx builder/cache without pruning, uses
`--provenance=false --sbom=false`, performs no hidden retry, and does not download
model weights. D-drive space should remain comfortably above the existing 150 GiB
threshold; exact build duration varies with cache state. Cleanup removes the registry
container and anonymous volume, pulled-by-digest local image, temporary Docker config,
metadata, transcript, and the ticket-owned temporary directory. It does not prune or
remove shared Buildx cache, unrelated images/volumes, or the owner's normal Docker
configuration.

T-0017 attempted the first controlled GHCR publication and failed; it is now
`SUPERSEDED`. T-0018 fixed the exit-state blocker and T-0019 completed the successful
second controlled SHA-only publication.

## T-0018 handled PowerShell native exit-state normalization

T-0017 run 31800647785 failed at the existing-tag preflight because
`docker manifest inspect` returned non-zero for an absent tag. The shared classifier
correctly returned `Absent`, but `$LASTEXITCODE` remained non-zero and GitHub Actions
used that stale native exit state as the step's final exit code.

`Test-PublisherRegistryTagState` now captures the native Docker exit code immediately.
Only when that non-zero result matches the existing narrow absence allowlist does the
explicit `Reset-PublisherLastExitCodeAfterAbsence` helper assign
`$global:LASTEXITCODE = 0` before returning `Absent`. Existing tags and all real
registry errors return without this normalization and remain fail-closed.

T-0018 adds deterministic separate-process regression tests for expected absence,
existing-tag refusal, real-error fail-closed behavior, and state isolation, and it
validated actual Docker CLI behavior against a disposable `127.0.0.1` registry. The
failed T-0017 run remains recorded as failure. T-0019 completed a separately
approved second controlled publication after T-0018 was merged and independently
approved.

## T-0025 production shape provenance

The repository now includes the reviewed immutable Hunyuan3D 2.1 shape manifest and
provenance under `model-manifests/production/`. The manifest contains no model payload
and does not authorize acquisition. `models plan`, `models status`, and `models verify`
can consume it offline; `models acquire` remains a separate explicitly authorized
operation. Loader source compatibility and the exact bounded research session are
recorded in the provenance and offline validator.
