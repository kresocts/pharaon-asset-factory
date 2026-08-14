# T-0024 evidence record - Hunyuan shape inventory

- Ticket: T-0024
- Branch: ticket/T-0024-production-shape-model-manifest
- Base SHA: c61e7c200dd3837b25a429f9fb5f8b0beb4c8873
- Final implementation SHA: (replaced with final HEAD after commit)
- Retrieval date/time UTC: 2026-08-14T18:40:39Z

## Exact official sources

- Model repository: `tencent/Hunyuan3D-2.1`
- Resolved immutable model revision: `0b94677654c57bb9a6b6845cd7b704ccf551d327`
- Source repository: `https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git`
- Pinned source revision from `docker/Dockerfile`: `82920d643c0dc2f7bfd7255f45f62d386edfe60c`

## Bounded retrieval log

| # | URL | Status | Bytes | Redirect host |
|---|-----|--------|-------|---------------|
| 1 | https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/revision/main | 200 | 5203 | none |
| 2 | https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/revision/0b94677654c57bb9a6b6845cd7b704ccf551d327 | 200 | 5203 | none |
| 3 | https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/revision/0b94677654c57bb9a6b6845cd7b704ccf551d327?blobs=true | 200 | 7875 | none |
| 4 | https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/revision/0b94677654c57bb9a6b6845cd7b704ccf551d327?blobs=true | 200 | 7875 | none |
| 5 | https://api.github.com/repos/Tencent-Hunyuan/Hunyuan3D-2.1/git/trees/82920d643c0dc2f7bfd7255f45f62d386edfe60c?recursive=1 | 200 | BLOCKED | none |
| 6 | https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/api_models.py | 200 | 2365 | none |
| 7 | https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/model_worker.py | 200 | 8225 | none |
| 8 | https://huggingface.co/api/models/tencent/Hunyuan3D-2.1 | 200 | 5203 | none |
| 9 | https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/hunyuan3d-dit-v2-1/config.yaml | 200 | 2078 | huggingface.co |
| 10 | https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/LICENSE | 200 | 17915 | huggingface.co |
| 11 | https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/Notice.txt | 200 | 16644 | huggingface.co |
| 12 | https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/README.md | 200 | 3998 | huggingface.co |


Total request count: 12.
Exact total response bytes: BLOCKED. Request #5 was not byte-captured, and no saved
response body, temp file, transcript, shell history, or local research log containing
that exact count has been found. The sum of the other 11 recorded bytes is 82,584; no
estimate or 13th request is authorized. No Authorization header, token, cookie, or
locally cached credential was used. No weight body was requested.

## Production inventory

| Path | Role | Size | SHA-256 | Identity method |
|------|------|------|---------|-----------------|
| config.yaml | shape-config | 2078 | `a9e9b66f0163a9b827d730633ac88f47fcc8e3071dcbb9ee12d88ef7537c5c6e` | small-file-direct-sha256 |
| model.fp16.ckpt | shape-weights | 7366389768 | `6b519fc7242f78e9b5f47ea4d55668fe3d944a2d27332f4ca68d29a6ff603f5e` | official-lfs-metadata |

Manifest plan_id: `5b6005ace3fa63b9719da75d1fc10a0793c41718e4f15666c8e527e16ff41cd8`.
File count: 2.
Total expected bytes: 7366391846.

## Loader compatibility evidence

Retained official immutable source `model_worker.py` at
`82920d643c0dc2f7bfd7255f45f62d386edfe60c`:

- line 55: `model_path='tencent/Hunyuan3D-2.1'`
- line 56: `subfolder='hunyuan3d-dit-v2-1'`
- line 91: `Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(model_path)`

The exact file-selection flow is recorded in provenance as:

- `hy3dshape/hy3dshape/pipelines.py` -> `Hunyuan3DDiTPipeline.from_pretrained`
- `hy3dshape/hy3dshape/utils/utils.py` -> `smart_load_model`

Those two source bodies were not retained during the bounded 12-request session, so
their exact line ranges are marked `BLOCKED` and are not independently verified in this
repository. No new retrieval was performed. The immutable `hunyuan3d-dit-v2-1/config.yaml`
still declares the `model`, `vae`, `conditioner`, `scheduler`, `image_processor`, and
`pipeline` targets, and the separate paint-model inventory is not required for shape-only
inference.

## License and model-card facts

- Declared license name: `tencent-hunyuan-community`
- Declared license link: `https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE`
- Declared `license`: `other`
- Official metadata `gated`: `false`
- Official metadata `private`: `false`
- Official metadata `disabled`: `false`
- Model card `extra_gated_eu_disallowed`: `true`
- LICENSE URL: `https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/LICENSE`
- Notice.txt URL: `https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/Notice.txt`
- Model card URL: `https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/README.md`
- LICENSE size/SHA-256: 17915 / `5bd08f93b2d280bb26ff3eed5d3996fe47a9698b5f7785163928668d7fd578c6`
- Notice.txt size/SHA-256: 16644 / `ffccf6b539a82e6084d14ff064dadd22d33384d6164b07c0c5a3141810df0350`
- Model card size/SHA-256: 3998 / `2905a706952c6a7716599cc76de44de82c650fd787d664b9cc013da3ef422679`

A human operator must review the license, notice, usage restrictions,
geographic/access restrictions, and intended use before any future acquisition.
T-0024 does not grant, accept, or confirm permission to download, use, redistribute,
or deploy the model.

## Confirmations

- No checkpoint or weight body was downloaded.
- No model cache was populated.
- No Docker build/push, GHCR operation, self-hosted runner startup, GPU operation,
  cloud resource, paid API, or inference operation occurred.
- Acquisition is not authorized by this ticket.
- `operator_review_required: true`, `acquisition_authorized: false`,
  `legal_conclusion: false`.

## Limitations

- `main` was used once to resolve the immutable revision; no mutable reference is
  committed or used by runtime/tests/validators.
- Request #5 exact byte count is `BLOCKED`; no local saved body or transcript contains it.
- Exact line ranges for `pipelines.py` and `utils/utils.py` are `BLOCKED`; their source bodies were not retained in the bounded session.
- Live upstream verification is intentionally not performed in CI; a future refresh is
  a new reviewed ticket with a new immutable revision.

## Validation commands and outcomes

The following commands were run from the repository root with temporary
input/output/workspace/cache directories and the committed production manifest.

```text
python validation/validate_production_shape_manifest.py
  -> exit 0, PRODUCTION_SHAPE_MANIFEST_VALID (twice, byte-identical)

python docker/model_cache.py plan --manifest model-manifests/production/hunyuan3d-2.1-shape.json --json
  -> exit 0, OK, file_count=2, total_expected=7366391846, plan_id=5b6005ace3fa63b9719da75d1fc10a0793c41718e4f15666c8e527e16ff41cd8

python docker/model_cache.py status --manifest model-manifests/production/hunyuan3d-2.1-shape.json --json
  -> exit 0, OK, fully_cached=false, ABSENT=2

python docker/model_cache.py verify --manifest model-manifests/production/hunyuan3d-2.1-shape.json --json
  -> exit 4, NOT_VERIFIED, fully_cached=false, ABSENT=2

python -m asset_pipeline.cli shape preflight --job JOB --backend hunyuan3d-2.1-shape --model-manifest model-manifests/production/hunyuan3d-2.1-shape.json --json
  -> exit 4, MODEL_CACHE_NOT_VERIFIED

Revision and plan_id are derived separately from the committed manifest and canonical
`docker.model_cache.manifest_plan_id` calculation, not from the preflight error JSON.

```

Filesystem snapshots before and after the plan/status/verify/preflight commands
confirmed that no output, workspace, or model-cache file was created.

Automated tests:

```text
python -m unittest tests.test_production_shape_manifest -v
  -> Ran 16 tests, OK

python -m unittest tests.test_model_cache_manifest tests.test_model_cache_plan tests.test_asset_pipeline_models tests.test_asset_pipeline_cli -v
  -> Ran 114 tests, OK (skipped=1)

python -m unittest discover -s tests -v
  -> Ran 406 tests, OK (skipped=9)
```

Repository and CI checks:

```text
python validation/run_ci.py -> passed (406 tests, metadata valid)
python validation/validate_repository.py -> Repository metadata is valid (21 tickets checked).
python validation/phase0_status.py -> PHASE_0_READY
git diff --check -> clean
```
