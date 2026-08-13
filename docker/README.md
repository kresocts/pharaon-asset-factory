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
test fixtures. Redirects are followed only when the target still obeys the same
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

### Retries and timeouts

Transport behavior is finite and documented: connection establishment is bounded by a
10-second connect timeout, each socket read by a 30-second read timeout, and at most 2
retries (3 attempts total) are made for transient failures (connection errors,
timeouts, HTTP 408/429, and HTTP 5xx). Permanent HTTP 4xx errors and integrity
failures are never retried. Retry attempts are visible in the machine-readable
`network.retries` and `network.requests_attempted` fields.

### Streaming, atomicity, and states

Downloads stream in 64 KiB chunks into a temporary `<final>.part` file on the
destination filesystem, never into memory. The temporary file is flushed and synced,
then promoted to the final path with an atomic rename only after exact size and
SHA-256 verification succeed. Corrupted, incomplete, oversized, or checksum-mismatched
content is never reported as valid and never leaves a verified final file behind.

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

Acquisition is serialized per artifact-set plan with an atomic lock directory under
`<cache root>/.locks/<plan-id>`. Lock acquisition waits at most 10 seconds and then
fails cleanly with exit code `6` and `LOCK_CONFLICT` classification. Stale locks are
handled conservatively: a lock is broken only when its owner metadata is older than 24
hours AND the recorded owner process is no longer alive; an active lock is never broken.
The lock holder refreshes the owner heartbeat during long downloads and removes the
lock on completion.

### JSON and exit-code contract

Every subcommand emits versioned JSON (`schema_version: 1`) with the command, artifact
identity, plan digest, cache root, file counts, byte totals, per-file states, network
request/retry counts, success flag, classification, exit code, and an actionable
message. No credentials, tokens, or environment dumps are emitted.

- `0` operation succeeded
- `2` policy refusal (missing confirmation or insufficient byte allowance)
- `3` manifest validation or destination path-security failure
- `4` integrity verification failure
- `5` transport failure
- `6` lock/concurrency conflict
- `64` invalid CLI usage
- `70` internal error

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
