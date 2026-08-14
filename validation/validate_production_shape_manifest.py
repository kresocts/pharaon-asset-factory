#!/usr/bin/env python3
"""Offline validator for the T-0025 production Hunyuan shape provenance."""

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
EXPECTED_MODEL_REPO_ID = "tencent/Hunyuan3D-2.1"
EXPECTED_MODEL_REVISION = "0b94677654c57bb9a6b6845cd7b704ccf551d327"
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
WEIGHT_SUFFIXES = {".ckpt", ".safetensors", ".bin", ".pt", ".pth", ".onnx", ".h5", ".pb"}
EXPECTED_LOADER_PATHS = {
    "model_worker.py",
    "hy3dshape/hy3dshape/pipelines.py",
    "hy3dshape/hy3dshape/utils/utils.py",
}
PERMITTED_HOSTS = {
    "huggingface.co",
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
}


class ValidationFailure(Exception):
    """An expected, sanitized production-provenance validation failure."""


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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _is_revision(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def _validated_reference_url(url: str, revision: str, expected_prefix: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    _require(parsed.scheme == "https", f"source reference URL must be HTTPS: {url}")
    _require(parsed.hostname in PERMITTED_HOSTS, f"source reference host is not permitted: {url}")
    _require(parsed.username is None and parsed.password is None, "source reference URL contains credentials")
    _require(not parsed.query, "source reference URL must not contain a query")
    _require(not parsed.fragment, "source reference URL must not contain a fragment")
    _require(revision in url, f"source reference URL does not contain the pinned revision {revision}: {url}")
    _require(url.startswith(expected_prefix), f"source reference URL does not use the expected immutable prefix: {url}")
    lowered = url.lower()
    _require(not any(word in lowered for word in ("/main/", "/master/", "/head/", "/latest/")), "source reference URL contains a mutable reference")


def _validate_line_ranges(ref: dict[str, Any], path: str) -> None:
    ranges = ref.get("line_ranges")
    _require(isinstance(ranges, list) and ranges, f"{path} source reference must have non-empty line_ranges")
    for item in ranges:
        _require(isinstance(item, dict), f"{path} line_ranges item must be an object")
        start = item.get("start")
        end = item.get("end")
        _require(isinstance(start, int) and not isinstance(start, bool), f"{path} line start must be an integer")
        _require(isinstance(end, int) and not isinstance(end, bool), f"{path} line end must be an integer")
        _require(start > 0 and end > 0, f"{path} line ranges must be positive")
        _require(start <= end, f"{path} line start must not exceed end")
        _require(isinstance(item.get("claim"), str) and item["claim"], f"{path} line range claim must be non-empty")


def _validate_source_references(prov: dict[str, Any]) -> None:
    refs = prov.get("source_references")
    _require(isinstance(refs, list), "source_references must be a list")
    by_path = {ref.get("path"): ref for ref in refs if isinstance(ref, dict)}
    _require(EXPECTED_LOADER_PATHS <= set(by_path), "missing required loader source references")
    for path in sorted(EXPECTED_LOADER_PATHS):
        ref = by_path[path]
        _require(isinstance(ref.get("url"), str) and ref["url"], f"{path} source reference URL must be non-empty")
        _validated_reference_url(
            ref["url"],
            EXPECTED_SOURCE_REVISION,
            f"https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/{EXPECTED_SOURCE_REVISION}/",
        )
        _require(_is_sha256(ref.get("sha256")), f"{path} source reference SHA-256 must be lowercase 64-hex")
        _require(isinstance(ref.get("function"), str) and ref["function"], f"{path} source reference function must be non-empty")
        _validate_line_ranges(ref, path)
        for field in ("url", "sha256", "function"):
            value = str(ref.get(field, "")).lower()
            _require("blocked" not in value, f"{path} source reference contains BLOCKED")
            _require("placeholder" not in value, f"{path} source reference contains placeholder")
            _require("unresolved" not in value, f"{path} source reference contains unresolved")
            _require("missing" not in value, f"{path} source reference contains missing")
        _require("lines" not in ref, f"{path} source reference uses the rejected flat lines list")


def _validate_network(prov: dict[str, Any]) -> None:
    network = prov.get("network_session_summary")
    _require(isinstance(network, dict), "network_session_summary must be an object")
    count = network.get("request_count")
    _require(isinstance(count, int) and not isinstance(count, bool), "request_count must be an integer")
    _require(1 <= count <= 10, "request_count must be between 1 and 10")
    total = network.get("total_response_body_bytes")
    _require(isinstance(total, int) and not isinstance(total, bool), "total_response_body_bytes must be an integer")
    _require(1 <= total <= 2 * 1024 * 1024, "total_response_body_bytes must be between 1 and 2,097,152")
    _require(network.get("weight_body_requested") is False, "weight_body_requested must be false")
    requests = network.get("requests")
    _require(isinstance(requests, list) and len(requests) == count, "request log length must equal request_count")
    actual_total = 0
    request_urls = {request.get("url") for request in requests if isinstance(request, dict)}
    for request in requests:
        _require(isinstance(request, dict), "each request log entry must be an object")
        body_bytes = request.get("body_bytes")
        _require(isinstance(body_bytes, int) and not isinstance(body_bytes, bool) and body_bytes >= 0, "request body_bytes must be a non-negative integer")
        actual_total += body_bytes
        _require(_is_sha256(request.get("body_sha256")), "request body_sha256 must be lowercase 64-hex")
        status = request.get("status")
        _require(isinstance(status, int) and not isinstance(status, bool), "request status must be an integer")
        redirect = request.get("redirect_location")
        _require(redirect is None or isinstance(redirect, str), "redirect_location must be null or a string")
        if redirect is not None:
            _require(redirect.startswith("https://"), "redirect_location must be HTTPS")
            _require(redirect in request_urls, "redirect hop is not represented by a logged request")
        url = request.get("url")
        _require(isinstance(url, str) and url.startswith("https://"), "request URL must be HTTPS")
        parsed = urllib.parse.urlsplit(url)
        _require(parsed.hostname in PERMITTED_HOSTS, f"request URL host is not permitted: {url}")
        _require(parsed.username is None and parsed.password is None, "request URL contains credentials")
        lowered = url.lower()
        _require(not any(suffix in lowered for suffix in WEIGHT_SUFFIXES), f"request URL references a weight body: {url}")
        _require("model.fp16.ckpt" not in lowered, f"request URL references a weight body: {url}")
    _require(actual_total == total, "sum of per-request body_bytes must equal total_response_body_bytes")


def _validate_license(prov: dict[str, Any]) -> None:
    license_data = prov.get("license")
    _require(isinstance(license_data, dict), "license must be an object")
    _require(license_data.get("license_name") == "tencent-hunyuan-community", "license_name mismatch")
    _require(license_data.get("license_declared") == "other", "license_declared mismatch")
    _require(license_data.get("gated") is False, "license gated must be false")
    _require(license_data.get("private") is False, "license private must be false")
    _require(license_data.get("disabled") is False, "license disabled must be false")
    _require(license_data.get("extra_gated_eu_disallowed") is True, "extra_gated_eu_disallowed must be true")
    _require(license_data.get("operator_review_required") is True, "license operator_review_required must be true")
    _require(license_data.get("acquisition_authorized") is False, "license acquisition_authorized must be false")
    _require(license_data.get("legal_conclusion") is False, "license legal_conclusion must be false")
    files = license_data.get("files")
    _require(isinstance(files, list) and len(files) >= 3, "license files must contain LICENSE, Notice.txt, and README.md")
    by_path = {entry.get("path"): entry for entry in files if isinstance(entry, dict)}
    for path in ("LICENSE", "Notice.txt", "README.md"):
        entry = by_path.get(path)
        _require(entry is not None, f"license files missing {path}")
        _require(isinstance(entry.get("url"), str) and entry["url"].startswith("https://huggingface.co/"), f"{path} license URL must be an immutable Hugging Face URL")
        _require(isinstance(entry.get("size"), int) and entry["size"] > 0, f"{path} license size must be positive")
        _require(entry.get("direct_body_hash") is False, f"{path} is metadata-only and must not claim a direct body hash")


def _validate_provenance(prov: dict[str, Any]) -> None:
    _require(prov.get("schema_version") == 1, "provenance schema_version must be 1")
    _require(prov.get("artifact_set") == models.CANONICAL_ARTIFACT_SET, "provenance artifact_set mismatch")
    _require(prov.get("model_repo_id") == EXPECTED_MODEL_REPO_ID, "provenance model_repo_id mismatch")
    _require(_is_revision(prov.get("model_revision")), "provenance model_revision must be a lowercase 40-hex SHA")
    _require(prov["model_revision"] == EXPECTED_MODEL_REVISION, "provenance model_revision mismatch")
    _require(prov.get("source_code_revision") == EXPECTED_SOURCE_REVISION, "provenance source_code_revision mismatch")
    _require(prov.get("source_revision") == EXPECTED_SOURCE_REVISION, "provenance source_revision mismatch")
    _require(prov.get("manifest_path") == "model-manifests/production/hunyuan3d-2.1-shape.json", "provenance manifest_path mismatch")
    _require(_is_sha256(prov.get("plan_id")), "provenance plan_id must be a lowercase 64-hex SHA-256")
    _require(prov.get("file_count") == 2, "provenance file_count must be 2")
    _require(prov.get("total_expected_bytes") == EXPECTED_TOTAL_BYTES, "provenance total_expected_bytes mismatch")
    _require(prov.get("operator_review_required") is True, "operator_review_required must be true")
    _require(prov.get("acquisition_authorized") is False, "acquisition_authorized must be false")
    _require(prov.get("legal_conclusion") is False, "legal_conclusion must be false")
    files = prov.get("files")
    _require(isinstance(files, list) and len(files) == 2, "provenance files must contain exactly two records")
    _require([entry.get("path") for entry in files] == ["config.yaml", "model.fp16.ckpt"], "provenance files must be sorted exactly config.yaml, model.fp16.ckpt")
    for entry, expected in zip(files, EXPECTED_FILES):
        for field in ("path", "role", "size", "sha256"):
            _require(entry.get(field) == expected[field], f"provenance file {expected['path']} {field} mismatch")
        _require(entry.get("identity_method") in {"small-file-direct-sha256", "official-lfs-metadata"}, f"provenance file {expected['path']} identity_method invalid")
        _require(isinstance(entry.get("metadata_field"), str) and entry["metadata_field"], f"provenance file {expected['path']} metadata_field must be non-empty")
    _validate_source_references(prov)
    _validate_license(prov)
    _validate_network(prov)


def _validate_manifest(manifest: dict[str, Any]) -> None:
    try:
        binding = models.bind_parsed_model_manifest(
            manifest,
            backend_id=models.CANONICAL_ARTIFACT_SET,
            cache_root=ROOT / "__validator_cache__",
        )
    except models.ModelPreflightError as error:
        raise ValidationFailure(f"manifest binding refused: {error}") from error
    _require(binding.file_count == 2, "bound manifest file_count must be 2")
    _require(binding.total_expected_bytes == EXPECTED_TOTAL_BYTES, "bound manifest total_expected_bytes mismatch")
    _require([file.path for file in binding.files] == ["config.yaml", "model.fp16.ckpt"], "bound manifest file set mismatch")
    _require([file.role for file in binding.files] == ["shape-config", "shape-weights"], "bound manifest role mapping mismatch")


def _validate_manifest_urls(manifest: dict[str, Any]) -> None:
    for file in manifest["files"]:
        url = file["url"]
        parsed = urllib.parse.urlsplit(url)
        _require(parsed.scheme == "https", f"URL scheme must be https for {file['path']}")
        _require(parsed.netloc == "huggingface.co", f"URL host must be huggingface.co for {file['path']}")
        _require(parsed.username is None and parsed.password is None, f"URL must not contain credentials for {file['path']}")
        _require(not parsed.query, f"URL must not contain a query for {file['path']}")
        _require(not parsed.fragment, f"URL must not contain a fragment for {file['path']}")
        expected_path = (
            f"/tencent/Hunyuan3D-2.1/resolve/{manifest['revision']}/"
            f"hunyuan3d-dit-v2-1/{file['path']}"
        )
        _require(parsed.path == expected_path, f"URL path mismatch for {file['path']}")


def _validate_consistency(manifest: dict[str, Any], prov: dict[str, Any]) -> None:
    _require(manifest.get("schema_version") == 1, "manifest schema_version must be 1")
    _require(manifest.get("artifact_set") == prov.get("artifact_set"), "artifact_set mismatch")
    _require(manifest.get("revision") == prov.get("model_revision"), "revision mismatch")
    _require(manifest.get("namespace") == f"{manifest['artifact_set']}/{manifest['revision']}", "manifest namespace mismatch")
    _require(model_cache.manifest_plan_id(manifest) == prov.get("plan_id"), "manifest plan_id mismatch")
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
    _validate_manifest_urls(manifest)
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
