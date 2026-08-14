#!/usr/bin/env python3
"""Offline validator for the T-0024 production Hunyuan shape inventory."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_pipeline import models  # noqa: E402
from docker import model_cache  # noqa: E402


MAX_PROVENANCE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_DIRECTORY_FILE_BYTES = 10 * 1024 * 1024
EXPECTED_MODEL_REPO = "tencent/Hunyuan3D-2.1"
EXPECTED_SOURCE_REVISION = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"
EXPECTED_FILES = (
    {
        "path": "config.yaml",
        "role": "shape-config",
        "size": 2078,
        "sha256": "a9e9b66f0163a9b827d730633ac88f47fcc8e3071dcbb9ee12d88ef7537c5c6e",
    },
    {
        "path": "model.fp16.ckpt",
        "role": "shape-weights",
        "size": 7366389768,
        "sha256": "6b519fc7242f78e9b5f47ea4d55668fe3d944a2d27332f4ca68d29a6ff603f5e",
    },
)
EXPECTED_TOTAL_BYTES = sum(entry["size"] for entry in EXPECTED_FILES)
MUTABLE_WORDS = ("main", "master", "latest", "head")
WEIGHT_SUFFIXES = {".ckpt", ".safetensors", ".bin", ".pt", ".pth", ".onnx", ".h5", ".pb"}


class ValidationFailure(Exception):
    """An expected, sanitized production-inventory validation failure."""


def _reject_constant(value: str) -> Any:
    raise ValidationFailure(f"non-finite JSON constant is not allowed: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationFailure(f"duplicate provenance object key: {key}")
        result[key] = value
    return result


def _read_json_defensive(path: Path, limit: int) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        raise ValidationFailure(f"cannot read {path.name}: {error}") from error
    if len(data) > limit:
        raise ValidationFailure(f"{path.name} exceeds the {limit} byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationFailure(f"{path.name} is not valid UTF-8") from error
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except ValidationFailure:
        raise
    except RecursionError as error:
        raise ValidationFailure(f"{path.name} exceeds the supported JSON nesting depth") from error
    except (ValueError, json.JSONDecodeError) as error:
        raise ValidationFailure(f"{path.name} is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValidationFailure(f"{path.name} must contain a JSON object")
    return parsed


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def _sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _revision(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", value))


def _validate_provenance(prov: dict[str, Any]) -> None:
    _require(prov.get("schema_version") == 1, "provenance schema_version must be 1")
    _require(prov.get("artifact_set") == models.CANONICAL_ARTIFACT_SET, "provenance artifact_set mismatch")
    _require(prov.get("model_repo_id") == EXPECTED_MODEL_REPO, "provenance model_repo_id mismatch")
    revision = prov.get("model_revision")
    _require(isinstance(revision, str) and _revision(revision), "provenance model_revision is not a full lowercase 40-hex SHA")
    _require(prov.get("metadata_endpoint") == f"https://huggingface.co/api/models/{EXPECTED_MODEL_REPO}", "provenance metadata_endpoint mismatch")
    _require(isinstance(prov.get("observed_at_utc"), str) and prov["observed_at_utc"], "provenance observed_at_utc must be non-empty")
    _require(prov.get("manifest_path") == "model-manifests/production/hunyuan3d-2.1-shape.json", "provenance manifest_path mismatch")
    _require(isinstance(prov.get("manifest_plan_id"), str) and _sha256(prov["manifest_plan_id"]), "provenance manifest_plan_id must be a 64-hex SHA-256")
    _require(prov.get("file_count") == 2, "provenance file_count must be 2")
    _require(prov.get("total_expected_bytes") == EXPECTED_TOTAL_BYTES, "provenance total_expected_bytes mismatch")
    files = prov.get("files")
    _require(isinstance(files, list) and len(files) == 2, "provenance files must contain exactly two sorted records")
    _require([entry.get("path") for entry in files] == ["config.yaml", "model.fp16.ckpt"], "provenance files must be sorted and named exactly config.yaml, model.fp16.ckpt")
    for entry, expected in zip(files, EXPECTED_FILES):
        for field in ("path", "role", "size", "sha256"):
            _require(entry.get(field) == expected[field], f"provenance file {expected['path']} {field} mismatch")
        _require(entry.get("identity_method") in {"small-file-direct-sha256", "official-lfs-metadata"}, f"provenance file {expected['path']} identity_method invalid")
        _require(isinstance(entry.get("metadata_field"), str) and entry["metadata_field"], f"provenance file {expected['path']} metadata_field must be non-empty")
    _require(prov.get("source_code_repository") == "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git", "provenance source_code_repository mismatch")
    _require(prov.get("source_code_revision") == EXPECTED_SOURCE_REVISION, "provenance source_code_revision mismatch")
    refs = prov.get("source_references")
    _require(isinstance(refs, list) and len(refs) == 2, "provenance source_references must contain two records")
    _require(any(ref.get("file") == "model_worker.py" for ref in refs), "provenance source_references missing model_worker.py")
    _require(any(ref.get("file") == "hunyuan3d-dit-v2-1/config.yaml" for ref in refs), "provenance source_references missing config.yaml")
    license_data = prov.get("license")
    _require(isinstance(license_data, dict), "provenance license must be an object")
    _require(license_data.get("license_name") == "tencent-hunyuan-community", "provenance license_name mismatch")
    _require(license_data.get("license_link") == "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE", "provenance license_link mismatch")
    _require(license_data.get("license_declared") == "other", "provenance license_declared mismatch")
    _require(license_data.get("gated") is False, "provenance gated must be false")
    _require(license_data.get("private") is False, "provenance private must be false")
    _require(license_data.get("disabled") is False, "provenance disabled must be false")
    _require(license_data.get("extra_gated_eu_disallowed") is True, "provenance extra_gated_eu_disallowed must be true")
    _require(license_data.get("operator_review_required") is True, "provenance license operator_review_required must be true")
    _require(license_data.get("acquisition_authorized") is False, "provenance license acquisition_authorized must be false")
    _require(license_data.get("legal_conclusion") is False, "provenance license legal_conclusion must be false")
    _require(prov.get("operator_review_required") is True, "provenance operator_review_required must be true")
    _require(prov.get("acquisition_authorized") is False, "provenance acquisition_authorized must be false")
    _require(prov.get("legal_conclusion") is False, "provenance legal_conclusion must be false")


def _validate_manifest(manifest: dict[str, Any]) -> None:
    try:
        binding = models.bind_parsed_model_manifest(
            manifest,
            backend_id="hunyuan3d-2.1-shape",
            cache_root=ROOT / "__validator_cache__",
        )
    except models.ModelPreflightError as error:
        raise ValidationFailure(f"manifest binding refused: {error}") from error
    _require(binding.file_count == 2, "bound manifest file_count must be 2")
    _require(binding.total_expected_bytes == EXPECTED_TOTAL_BYTES, "bound manifest total_expected_bytes mismatch")
    _require([file.path for file in binding.files] == ["config.yaml", "model.fp16.ckpt"], "bound manifest file set mismatch")
    _require([file.role for file in binding.files] == ["shape-config", "shape-weights"], "bound manifest role mapping mismatch")


def _validate_urls(manifest: dict[str, Any]) -> None:
    for file in manifest["files"]:
        url = file["url"]
        parsed = urllib.parse.urlsplit(url)
        _require(parsed.scheme == "https", f"URL scheme must be https for {file['path']}")
        _require(parsed.netloc == "huggingface.co", f"URL host must be huggingface.co for {file['path']}")
        _require(parsed.username is None and parsed.password is None, f"URL must not contain credentials for {file['path']}")
        _require(not parsed.query, f"URL must not contain a query for {file['path']}")
        _require(not parsed.fragment, f"URL must not contain a fragment for {file['path']}")
        lowered = url.lower()
        _require(not any(word in lowered for word in MUTABLE_WORDS), f"URL contains a mutable reference for {file['path']}")
        expected_path = (
            f"/tencent/Hunyuan3D-2.1/resolve/{manifest['revision']}/"
            f"hunyuan3d-dit-v2-1/{file['path']}"
        )
        _require(parsed.path == expected_path, f"URL path mismatch for {file['path']}")


def _validate_consistency(manifest: dict[str, Any], prov: dict[str, Any]) -> None:
    _require(manifest.get("schema_version") == 1, "manifest schema_version must be 1")
    _require(manifest.get("artifact_set") == prov.get("artifact_set"), "artifact_set mismatch")
    _require(manifest.get("revision") == prov.get("model_revision"), "revision mismatch")
    _require(
        manifest.get("namespace") == f"{manifest['artifact_set']}/{manifest['revision']}",
        "manifest namespace is not the canonical artifact_set/revision namespace",
    )
    _require(model_cache.manifest_plan_id(manifest) == prov.get("manifest_plan_id"), "manifest plan_id mismatch")
    _require(len(manifest["files"]) == prov.get("file_count"), "file count mismatch")
    _require(sum(file["size"] for file in manifest["files"]) == prov.get("total_expected_bytes"), "total expected bytes mismatch")
    by_path = {entry["path"]: entry for entry in prov["files"]}
    for file in manifest["files"]:
        record = by_path.get(file["path"])
        _require(record is not None, f"missing provenance record for {file['path']}")
        _require(file["role"] == record["role"], f"role mismatch for {file['path']}")
        _require(file["size"] == record["size"], f"size mismatch for {file['path']}")
        _require(file["sha256"] == record["sha256"], f"sha256 mismatch for {file['path']}")
        _require(file["url"] == record["url"], f"url mismatch for {file['path']}")


def _validate_source_revision(root: Path, prov: dict[str, Any]) -> None:
    dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG HUNYUAN_COMMIT=([0-9a-f]{40})$", dockerfile, flags=re.MULTILINE)
    _require(match is not None, "docker/Dockerfile does not pin HUNYUAN_COMMIT")
    _require(match.group(1) == prov.get("source_code_revision"), "source_code_revision does not match docker/Dockerfile")


def _validate_no_model_payload(root: Path) -> None:
    manifest_root = root / "model-manifests"
    _require(manifest_root.is_dir(), "model-manifests directory is missing")
    for path in manifest_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in WEIGHT_SUFFIXES:
            raise ValidationFailure(f"model payload is present under model-manifests: {path.relative_to(root)}")
        if path.stat().st_size > MAX_MANIFEST_DIRECTORY_FILE_BYTES:
            raise ValidationFailure(f"unexpected large committed file under model-manifests: {path.relative_to(root)}")


def validate(root: Path) -> None:
    root = root.resolve()
    manifest_path = root / "model-manifests" / "production" / "hunyuan3d-2.1-shape.json"
    provenance_path = root / "model-manifests" / "production" / "hunyuan3d-2.1-shape.provenance.json"
    prov = _read_json_defensive(provenance_path, MAX_PROVENANCE_BYTES)
    _validate_provenance(prov)
    _validate_no_model_payload(root)
    _validate_source_revision(root, prov)
    try:
        manifest = model_cache.parse_manifest(manifest_path)
    except model_cache.ManifestValidationError as error:
        raise ValidationFailure(f"manifest is invalid: {error}") from error
    _validate_manifest(manifest)
    _validate_urls(manifest)
    _validate_consistency(manifest, prov)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        validate(args.root)
    except ValidationFailure as error:
        print(f"PRODUCTION_SHAPE_MANIFEST_INVALID: {error}", file=sys.stderr)
        return 1
    print("PRODUCTION_SHAPE_MANIFEST_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
