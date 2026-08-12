# ADR 0003: Pinned Hunyuan dependency layer and native-extension boundary

**Status:** Accepted

**Context:** T-0011 must add the software environment officially tested for Hunyuan3D 2.1. Mutable `main`, broad mirrors, four unversioned requirements, native CUDA builds, and weight downloads are not reproducible as one layer.

**Decision:** Install PyTorch 2.5.1, torchvision 0.20.1, and torchaudio 2.5.1 from the official CUDA 12.4 index. Fetch Tencent source at immutable commit `82920d643c0dc2f7bfd7255f45f62d386edfe60c`, verify source and requirements, install it under `/opt/hunyuan3d`, and record the revision without Git history. Preserve upstream requirements and install a sanitized top-level contract from standard PyPI, using Blender's index only for `bpy` and pinning the four unversioned entries. Keep weights under the external `/models` boundary. Defer both native renderers to T-0012 and diagnose their absence as intentional.

**Consequences:** Builds do not follow Hunyuan `main` or unversioned top-level packages. Diagnostics distinguish CUDA wheels, source, imports, extensions, and weights. The image is large; package-index availability remains external, and the recorded `pip freeze` is not a hash-locked wheelhouse. Inference remains unavailable until later extension and weight tickets. Upgrades must update all pins, checksums, diagnostics, docs, and CPU/GPU evidence together.

**Alternatives considered:** Tracking `main` is mutable. Vendoring duplicates third-party history. Broad Tencent/Aliyun mirrors widen package-source trust. Compiling extensions or downloading weights now would collapse separate cache, test, and license boundaries.

Related ticket: T-0011.
