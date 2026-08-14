# Model manifests

The `production/` directory holds the first immutable production Hunyuan shape-model
inventory and its machine-readable provenance record.

- `hunyuan3d-2.1-shape.json` is the canonical T-0014 schema-version-1 manifest for the
  shape-only runtime. It pins the full Hugging Face revision
  `0b94677654c57bb9a6b6845cd7b704ccf551d327` and lists exactly the two files required by
  the repository-pinned Hunyuan loader: `config.yaml` and `model.fp16.ckpt`.
- `hunyuan3d-2.1-shape.provenance.json` records the official source metadata, exact file
  identities, license/model-card facts, source-loader references, and the mandatory
  human-review/acquisition flags.
- `../validation/validate_production_shape_manifest.py` validates both files completely
  offline and must pass before this inventory is considered production evidence.

This ticket does not authorize acquisition. Cache population, Docker/GHCR/runner work,
GPU execution, and inference remain separate future tickets.
