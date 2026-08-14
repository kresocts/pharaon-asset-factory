# 0011 - Official Hunyuan shape inventory and provenance

- **Status:** Accepted
- **Ticket:** T-0024
- **Pull request:** (filled by the T-0024 implementation PR)

## Context

T-0023 introduced the immutable shape-model binding and offline cache-preflight
boundary but intentionally did not commit a production inventory. T-0024 supplies the
first official, immutable Hunyuan3D 2.1 shape-only production manifest and provenance,
without downloading model weights or enabling acquisition, Docker/GHCR/runner work,
GPU execution, or inference.

## Decision

Pin the official Hugging Face repository `tencent/Hunyuan3D-2.1` to the immutable
revision `0b94677654c57bb9a6b6845cd7b704ccf551d327`. The production manifest contains
exactly:

```text
hunyuan3d-dit-v2-1/config.yaml
hunyuan3d-dit-v2-1/model.fp16.ckpt
```

The small `config.yaml` is fetched from the immutable Hugging Face resolve URL and
hashed locally. The large `model.fp16.ckpt` identity comes only from official immutable
Hugging Face LFS metadata (`siblings[].lfs.sha256` and `size`); the weight body is never
requested or downloaded. The manifest remains explicit and operator-supplied; no hidden
default is added to the runtime.

The pinned source loader is proven by the official immutable source at
`82920d643c0dc2f7bfd7255f45f62d386edfe60c`: `model_worker.py` defaults to
`model_path='tencent/Hunyuan3D-2.1'` and `subfolder='hunyuan3d-dit-v2-1'`, then calls
`Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(model_path)`. The same subfolder's
official `config.yaml` declares the model, VAE, conditioner, scheduler, image
processor, and pipeline targets, so the normal non-safetensors shape path is covered by
one config file and one fp16 checkpoint. The separate paint-model inventory is not
required for shape-only inference.

## Consequences

Benefits:

- The shape model identity is immutable, exact, and independently verifiable offline.
- Large-file cryptographic identity is obtained without a prohibited weight download.
- License and access facts are recorded without making a legal conclusion or
  authorizing acquisition.
- Offline plan/status/verify/preflight commands can use the committed manifest against
  an empty cache and report `MODEL_CACHE_NOT_VERIFIED`, not execution readiness.

Costs and risks:

- `main` was used once to resolve the immutable revision; only the immutable SHA is
  committed and used by runtime, tests, and validators.
- The GitHub source tree enumeration response byte count was not captured by the
  exploratory path-listing command; all other bounded metadata/text responses are
  recorded exactly and total well under the 10 MiB limit.
- Live upstream verification is not performed in CI because it would make normal
  repository validation network-dependent and mutable.

Follow-up work:

- A future reviewed ticket must authorize and budget actual model acquisition into
  `MODEL_CACHE_DIR`.
- A future reviewed ticket must implement Hunyuan runtime/GPU execution and inference
  after cache verification.
- A future refresh must be a new reviewed ticket with a new immutable revision and
  updated manifest/provenance; it must not silently change this pinned inventory.
