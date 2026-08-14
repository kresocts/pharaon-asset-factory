# Model manifests

This directory contains reviewed, immutable external-model manifests and provenance
records. Files under this directory are small metadata only; no model payloads,
checkpoints, safetensors, or other weight bodies may be committed here.

## Production shape inventory

- [hunyuan3d-2.1-shape.json](production/hunyuan3d-2.1-shape.json) — the T-0014 schema
  version 1 manifest for the minimal Hunyuan3D 2.1 shape inventory.
- [hunyuan3d-2.1-shape.provenance.json](production/hunyuan3d-2.1-shape.provenance.json) —
  immutable model/source revision evidence, loader source line ranges, license
  metadata, and the authoritative bounded request log.

Validate completely offline with:

```bash
python validation/validate_production_shape_manifest.py
```

The manifest intentionally authorizes no acquisition. Cache verification remains
read-only through `models status` and `models verify`; acquisition is a separate,
explicitly authorized operation.
