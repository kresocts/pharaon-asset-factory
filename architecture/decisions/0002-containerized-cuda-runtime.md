# ADR 0002: Containerized CUDA runtime and external model cache

**Status:** Accepted

**Context:** T-0010 begins Phase 1 and must provide a portable GPU foundation before Hunyuan3D dependencies or provider integrations exist. Later native extensions need CUDA development tooling, while model weights are large, licensed separately, and must survive replacement of a disposable container.

**Decision:** Base GPU workers on the pinned official `nvidia/cuda:12.4.1-devel-ubuntu22.04` image with an explicit Python 3.10 virtual environment. Use the standard host NVIDIA driver and NVIDIA Container Toolkit boundary, without requiring the host CUDA toolkit to match container userspace. Run as an unprivileged fixed user. Keep application code under `/app`, model cache under `/models`, and job data under `/data`; model weights are mounted or cached externally and never baked into the image. Startup performs diagnostics only and makes no downloads. PyTorch and Hunyuan dependencies are deferred to later tickets.

**Consequences:** The same image boundary can run on local RTX 3090/4090 hardware or a provider exposing the NVIDIA container runtime. The development base is larger than a runtime-only image but avoids changing foundations when native CUDA extensions are compiled. Operators must supply a sufficiently recent host driver and writable mount permissions for user `10001:10001`. Base-image and Ubuntu package repositories remain upstream reproducibility dependencies; later release work may add digest/package lock automation.

**Alternatives considered:** A runtime-only image is smaller but cannot compile anticipated native extensions. Installing CUDA on each host couples builds to provider images. Baking weights into `/app` creates large, non-portable images and mixes licensed mutable data with source. Provider-specific images would prematurely couple the worker to rental infrastructure.

Related ticket: T-0010.
