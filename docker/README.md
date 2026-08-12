# GPU base image

T-0010 provides the provider-neutral CUDA/Python foundation for later asset-worker images. It does not contain Hunyuan3D, PyTorch, model weights, model downloads, or a service.

## Version choices

The image uses the official `nvidia/cuda:12.4.1-devel-ubuntu22.04` base pinned to manifest-list digest `sha256:da6791294b0b04d7e65d87b7451d6f2390b4d36225ab0701ee7dfec5769829f5` and explicitly installs Python 3.10 into `/opt/venv`. CUDA 12.4 is suitable for modern NVIDIA drivers and Ampere/Ada GPUs such as RTX 3090 and RTX 4090. The development variant includes the compiler toolchain that a later ticket will need for native CUDA extensions. PyTorch is intentionally deferred to T-0011 so its version can be pinned together with the actual model dependency layer.

The host supplies the NVIDIA kernel driver; NVIDIA Container Toolkit exposes the GPU and driver libraries to the pinned CUDA userspace in the container. The host does not need an exactly matching CUDA toolkit, but its driver must support the container's CUDA version.

## Build and diagnostics

From the repository root:

```bash
docker build --tag pharaon-asset-factory-gpu-base --file docker/Dockerfile .
docker run --rm pharaon-asset-factory-gpu-base health
docker run --rm pharaon-asset-factory-gpu-base health --json
```

The CPU smoke test succeeds with `GPU_NOT_AVAILABLE`. On a configured NVIDIA host, require a visible GPU with:

```bash
docker run --rm --gpus all pharaon-asset-factory-gpu-base health --require-gpu
```

Startup makes no network calls or downloads. The default command exits after diagnostics; it does not run a server. The health script reports Python and OS details, configured paths, CUDA environment and compiler state, `nvidia-smi`/GPU visibility, and PyTorch state. `GPU_RUNTIME_ERROR` means `nvidia-smi` was found but failed, while `GPU_NOT_AVAILABLE` is expected on CPU-only hosts.

## Filesystem and mounts

| Path | Environment variable | Purpose |
| --- | --- | --- |
| `/app` | n/a | Immutable application/runtime code |
| `/models` | `MODEL_CACHE_DIR` | External future model cache |
| `/data/input` | `INPUT_DIR` | Input assets |
| `/data/output` | `OUTPUT_DIR` | Generated artifacts |
| `/workspace` | `WORKSPACE_DIR` | Optional temporary working area |

Keep weights outside `/app`. Named volumes or bind mounts work without application changes:

```bash
docker run --rm --gpus all \
  --mount type=volume,source=pharaon-models,target=/models \
  --mount type=bind,source="$PWD/input",target=/data/input,readonly \
  --mount type=bind,source="$PWD/output",target=/data/output \
  pharaon-asset-factory-gpu-base health --require-gpu
```

The process runs as fixed unprivileged user/group `10001:10001`; bind-mounted writable directories must grant that identity access. No privileged mode or Docker socket mount is required.

## Current limitations

This image proves only the CUDA/Python container boundary. T-0011 is expected to add a pinned PyTorch and Hunyuan dependency layer. It must not bake model weights into the image; `/models` remains the portable cache boundary for local Docker, Vast.ai, or RunPod volumes. This ticket does not publish an image or integrate any provider.
