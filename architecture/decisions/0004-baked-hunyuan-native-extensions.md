# ADR 0004: Bake pinned Hunyuan native extensions into the image

**Status:** Accepted

**Context:** T-0012 must compile the pinned Hunyuan3D 2.1 custom rasterizer and mesh-painter extension reproducibly for local RTX 4060 validation and future RTX 3090/4090 hosts. Native installation must not be confused with model readiness.

**Decision:** Keep the established single CUDA development stage because T-0010 intentionally retained its compiler and `nvcc` for this layer. Install the narrow missing Ubuntu Python development toolchain, build the custom rasterizer as a normal wheel from pinned source with `--no-build-isolation`, and execute the exact pinned `compile_mesh_painter.sh`. Build isolation is disabled because upstream `setup.py` imports the already-pinned torch package before compilation. Compile CUDA code for compute capabilities 8.6 and 8.9 only. Record stable artifact paths and diagnose artifacts, imports, optional GPU execution, weights, and full inference readiness separately.

**Consequences:** RTX 3090- and RTX 4060/4090-class architectures are represented without an unnecessarily broad binary. Only RTX 4060 behavior is empirically validated by this ticket. The devel toolchain remains in the image, trading size for a simple reproducible extension layer and continuity with T-0010. `HUNYUAN_NATIVE_EXTENSIONS_READY` does not imply inference readiness: weights remain external and absent, so `full_hunyuan_ready` remains false. The renderer's native pybind11 operation is CPU-based; the custom rasterizer provides the ticket's CUDA-kernel evidence.

**Alternatives considered:** Editable installation would couple runtime imports to mutable source layout. PEP 517 isolation cannot see upstream's undeclared torch build dependency. A multi-stage copy would require reconstructing the large Python/native runtime boundary for modest benefit. Compiling every CUDA architecture would increase time and binary size without a stated target.

Related ticket: T-0012.
