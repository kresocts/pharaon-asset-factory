# T-0025 Hunyuan shape provenance evidence

- Base SHA: `c61e7c200dd3837b25a429f9fb5f8b0beb4c8873`
- Implementation content SHA: `397a1b8f20b79a2acfd30a1f49af14dd19c7ee70`
- Branch: `ticket/T-0025-production-shape-provenance-retry`
- PR: one non-draft PR targeting `main`
- PR #22: closed and unmerged; used only as historical audit context

## Operator authorization

One bounded public-research session was authorized with at most 10 HTTPS requests,
at most 2,097,152 total response-body bytes, public official endpoints only, no
authentication/token/cookie/signed URL, no automatic retry, timeout no greater than
30 seconds per request, and no checkpoint/weight body, range request, LFS/Xet/CAS
transfer, Git clone, snapshot download, Docker/GHCR/runner/GPU/cloud/paid/inference
operation, or model-cache population.

## Exact request plan

1. Official Hugging Face model metadata.
2. Official Hugging Face immutable revision metadata.
3. Official Hugging Face immutable recursive tree metadata (initial transport failure
   logged as sequence 3; one bounded continuation logged as sequence 4).
4. Pinned source `model_worker.py`.
5. Pinned source `hy3dshape/hy3dshape/pipelines.py`.
6. Pinned source `hy3dshape/hy3dshape/utils/utils.py`.
7. Pinned source config path probe (expected absent; logged as HTTP 404).
8. Official immutable Hugging Face raw `hunyuan3d-dit-v2-1/config.yaml`.

No weight body or checkpoint URL was requested. One request slot remained unused.

## Authoritative request log

| Seq | URL | Status | Bytes | SHA-256 | Redirect |
| --- | --- | --- | --- | --- | --- |
| 1 | `https://huggingface.co/api/models/tencent/Hunyuan3D-2.1` | 200 | 5203 | `8c0ab282f6bbfa6c3d8bc90f4dd10d7ab7e0ac68b3827f4e49e34c87d1db32f9` | none |
| 2 | `https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/revision/0b94677654c57bb9a6b6845cd7b704ccf551d327` | 200 | 5203 | `8c0ab282f6bbfa6c3d8bc90f4dd10d7ab7e0ac68b3827f4e49e34c87d1db32f9` | none |
| 3 | `https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/tree/0b94677654c57bb9a6b6845cd7b704ccf551d327?recursive=true&expand=true` | 0 | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | none |
| 4 | `https://huggingface.co/api/models/tencent/Hunyuan3D-2.1/tree/0b94677654c57bb9a6b6845cd7b704ccf551d327?recursive=true&expand=true` | 200 | 34726 | `bc1606af8c948145d44d22f4d263dd08c09d8b8199cb667012419f856cb3ef12` | none |
| 5 | `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/model_worker.py` | 200 | 8225 | `4d1bc1b8857365afd289f60319aa78601a2ed054c21ae4de64c2cfea03a7aadc` | none |
| 6 | `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/hy3dshape/hy3dshape/pipelines.py` | 200 | 32244 | `80d3d66be03e2ebe2f988cefcf488f2e5fa52656e8547c72dd44aaae4ad45714` | none |
| 7 | `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/hy3dshape/hy3dshape/utils/utils.py` | 200 | 4634 | `09fda07cb7e7aafbbc2d0a0a808d312d6687bcb1a939c33cdf9f88dc07d42ca0` | none |
| 8 | `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/hunyuan3d-dit-v2-1/config.yaml` | 404 | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | none |
| 9 | `https://huggingface.co/tencent/Hunyuan3D-2.1/raw/0b94677654c57bb9a6b6845cd7b704ccf551d327/hunyuan3d-dit-v2-1/config.yaml` | 200 | 2078 | `a9e9b66f0163a9b827d730633ac88f47fcc8e3071dcbb9ee12d88ef7537c5c6e` | none |

Totals: 9 requests, 92,313 response-body bytes. Every redirect hop was counted; none
occurred in this session.

## Immutable revisions

- Model revision: `0b94677654c57bb9a6b6845cd7b704ccf551d327`
- Source revision: `82920d643c0dc2f7bfd7255f45f62d386edfe60c`

## Exact inventory

| Path | Role | Size | SHA-256 |
| --- | --- | --- | --- |
| `config.yaml` | `shape-config` | 2078 | `a9e9b66f0163a9b827d730633ac88f47fcc8e3071dcbb9ee12d88ef7537c5c6e` |
| `model.fp16.ckpt` | `shape-weights` | 7366389768 | `6b519fc7242f78e9b5f47ea4d55668fe3d944a2d27332f4ca68d29a6ff603f5e` |

Canonical `plan_id`: `5b6005ace3fa63b9719da75d1fc10a0793c41718e4f15666c8e527e16ff41cd8`.
File count: 2. Total expected bytes: 7,366,391,846.

## Loader source references

### model_worker.py

- URL: `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/model_worker.py`
- SHA-256: `4d1bc1b8857365afd289f60319aa78601a2ed054c21ae4de64c2cfea03a7aadc`
- Function: `ModelWorker.__init__`
- Lines 61-62: default `model_path='tencent/Hunyuan3D-2.1'`,
  `subfolder='hunyuan3d-dit-v2-1'`
- Line 99: calls
  `Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(model_path)`

### hy3dshape/hy3dshape/pipelines.py

- URL: `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/hy3dshape/hy3dshape/pipelines.py`
- SHA-256: `80d3d66be03e2ebe2f988cefcf488f2e5fa52656e8547c72dd44aaae4ad45714`
- Function: `Hunyuan3DDiTPipeline.from_pretrained`
- Lines 136-187: `from_single_file` loads config and the non-safetensors checkpoint,
  then model, VAE, conditioner, image processor, and scheduler.
- Lines 196-227: `from_pretrained` defaults `use_safetensors=False`,
  `variant='fp16'`, `subfolder='hunyuan3d-dit-v2-1'`, delegates to
  `smart_load_model`, and calls `from_single_file`.

### hy3dshape/hy3dshape/utils/utils.py

- URL: `https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/82920d643c0dc2f7bfd7255f45f62d386edfe60c/hy3dshape/hy3dshape/utils/utils.py`
- SHA-256: `09fda07cb7e7aafbbc2d0a0a808d312d6687bcb1a939c33cdf9f88dc07d42ca0`
- Function: `smart_load_model`
- Lines 89-128: selects `extension='ckpt'` for non-safetensors, constructs
  `variant='.fp16'`, `ckpt_name='model.fp16.ckpt'`, and
  `config_path='config.yaml'`.

## License and model-card facts

- Declared license: `tencent-hunyuan-community`
- Declared license type: `other`
- License link: `https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE`
- Gated: false; private: false; disabled: false
- Extra gated EU disallowed: true
- `LICENSE` metadata size: 17,915 bytes; immutable URL:
  `https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/LICENSE`
- `Notice.txt` metadata size: 16,644 bytes; immutable URL:
  `https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/Notice.txt`
- `README.md` metadata size: 3,998 bytes; immutable URL:
  `https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/0b94677654c57bb9a6b6845cd7b704ccf551d327/README.md`
- These optional bodies are metadata-only in this session; no direct body hash is
  claimed.
- Operator review required: true. Acquisition authorized: false. Legal conclusion:
  false.

## Offline results

The validator produced byte-identical `PRODUCTION_SHAPE_MANIFEST_VALID` output on two
runs. Production `models plan` reports the exact two-file inventory and total bytes.
`models status` and `models verify` against an empty cache remain read-only and
not-verified. `shape preflight` against an empty cache returns
`MODEL_CACHE_NOT_VERIFIED` under unchanged T-0023 semantics. No cache, output,
workspace, or model payload writes occurred.

## Deviations and limitations

The pinned source repository does not contain `hunyuan3d-dit-v2-1/config.yaml`, so the
config body was retrieved from the official immutable Hugging Face raw endpoint. The
checkpoint was identified from official LFS metadata only; its body was not requested.
Optional license/model-card bodies were not retrieved, so they remain metadata-only.

PR #22 remained closed and unmerged throughout this work.
