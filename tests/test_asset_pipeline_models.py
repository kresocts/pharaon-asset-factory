"""Focused tests for immutable Hunyuan shape-model binding."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from asset_pipeline import models


REVISION = "a" * 40
PREFIX = (
    f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{REVISION}/hunyuan3d-dit-v2-1/"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_manifest(**overrides):
    config = b'{"shape_config": true}'
    weights = b"shape-weights"
    auxiliary = b"auxiliary-data"
    data = {
        "schema_version": 1,
        "artifact_set": models.CANONICAL_ARTIFACT_SET,
        "revision": REVISION,
        "namespace": f"{models.CANONICAL_ARTIFACT_SET}/{REVISION}",
        "description": "synthetic immutable model fixture",
        "files": [
            {
                "path": "config/model.json",
                "url": PREFIX + "config/model.json",
                "size": len(config),
                "sha256": _sha(config),
                "role": "shape-config",
            },
            {
                "path": "weights/model.safetensors",
                "url": PREFIX + "weights/model.safetensors",
                "size": len(weights),
                "sha256": _sha(weights),
                "role": "shape-weights",
            },
            {
                "path": "auxiliary/tokenizer.json",
                "url": PREFIX + "auxiliary/tokenizer.json",
                "size": len(auxiliary),
                "sha256": _sha(auxiliary),
                "role": "shape-auxiliary",
            },
        ],
    }
    data.update(overrides)
    return data


def write_manifest(directory, data):
    path = Path(directory) / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class BindingTests(unittest.TestCase):
    def _bind(self, data, cache_root):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(directory, data)
            return models.bind_model_manifest(
                path,
                backend_id="hunyuan3d-2.1-shape",
                cache_root=cache_root,
            )

    def _refuse(self, data, cache_root):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(directory, data)
            with self.assertRaises(models.ModelPreflightError) as raised:
                models.bind_model_manifest(
                    path,
                    backend_id="hunyuan3d-2.1-shape",
                    cache_root=cache_root,
                )
            return raised.exception

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache_root = Path(self.temp.name) / "models"

    def test_canonical_synthetic_binding(self):
        data = valid_manifest()
        binding = self._bind(data, self.cache_root)
        self.assertEqual(binding.schema_version, 1)
        self.assertEqual(binding.backend_id, "hunyuan3d-2.1-shape")
        self.assertEqual(binding.artifact_set, models.CANONICAL_ARTIFACT_SET)
        self.assertEqual(binding.revision, REVISION)
        self.assertEqual(binding.namespace, f"{models.CANONICAL_ARTIFACT_SET}/{REVISION}")
        self.assertEqual(binding.file_count, 3)
        self.assertEqual(
            binding.total_expected_bytes,
            sum(file["size"] for file in data["files"]),
        )
        self.assertEqual(binding.files[0].path, "auxiliary/tokenizer.json")
        self.assertEqual(json.dumps(binding.to_dict()).find("url"), -1)

    def test_wrong_artifact_set(self):
        self._refuse(valid_manifest(artifact_set="wrong-set"), self.cache_root)

    def test_revision_must_be_lowercase_40_hex(self):
        for revision in (REVISION.upper(), "g" + "a" * 39, "a" * 39, "a" * 41):
            data = valid_manifest(revision=revision)
            data["namespace"] = f"{models.CANONICAL_ARTIFACT_SET}/{revision}"
            self._refuse(data, self.cache_root)

    def test_namespace_mismatch(self):
        data = valid_manifest(namespace="wrong-namespace")
        self._refuse(data, self.cache_root)

    def test_wrong_scheme(self):
        data = valid_manifest()
        data["files"][0]["url"] = "ftp://huggingface.co/tencent/Hunyuan3D-2.1/resolve/"
        data["files"][0]["url"] += REVISION + "/hunyuan3d-dit-v2-1/config/model.json"
        self._refuse(data, self.cache_root)

    def test_wrong_host_and_suffix_trick(self):
        for host in ("huggingface.com", "huggingface.co.example.invalid", "sub.huggingface.co"):
            data = valid_manifest()
            data["files"][0]["url"] = (
                f"https://{host}/tencent/Hunyuan3D-2.1/resolve/{REVISION}/"
                "hunyuan3d-dit-v2-1/config/model.json"
            )
            self._refuse(data, self.cache_root)

    def test_embedded_credentials(self):
        data = valid_manifest()
        data["files"][0]["url"] = (
            f"https://user:pass@huggingface.co/tencent/Hunyuan3D-2.1/resolve/{REVISION}/"
            "hunyuan3d-dit-v2-1/config/model.json"
        )
        self._refuse(data, self.cache_root)

    def test_query_string_and_fragment(self):
        for suffix in ("?revision=x", "#fragment"):
            data = valid_manifest()
            data["files"][0]["url"] = (
                PREFIX + "config/model.json" + suffix
            )
            self._refuse(data, self.cache_root)

    def test_wrong_repository_owner_or_name(self):
        data = valid_manifest()
        data["files"][0]["url"] = (
            f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{REVISION}/"
            "hunyuan3d-dit-v2-1/config/model.json"
        ).replace("tencent", "other", 1)
        self._refuse(data, self.cache_root)
        data = valid_manifest()
        data["files"][0]["url"] = (
            f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{REVISION}/"
            "hunyuan3d-dit-v2-1/config/model.json"
        ).replace("Hunyuan3D-2.1", "Hunyuan3D-2.0", 1)
        self._refuse(data, self.cache_root)

    def test_mismatched_url_revision(self):
        other = "b" * 40
        data = valid_manifest()
        data["files"][0]["url"] = (
            f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{other}/"
            "hunyuan3d-dit-v2-1/config/model.json"
        )
        self._refuse(data, self.cache_root)

    def test_wrong_subdirectory(self):
        data = valid_manifest()
        data["files"][0]["url"] = (
            f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{REVISION}/"
            "other-dir/config/model.json"
        )
        self._refuse(data, self.cache_root)

    def test_url_and_file_path_mismatch(self):
        data = valid_manifest()
        data["files"][0]["path"] = "config/other.json"
        self._refuse(data, self.cache_root)

    def test_encoded_traversal_and_separators_are_refused(self):
        encoded = [
            "%2e%2e/weights/model.safetensors",
            "%2E%2E/weights/model.safetensors",
            "config%2Fmodel.json",
            "config%2fmodel.json",
            "%252e%252e/weights/model.safetensors",
        ]
        for suffix in encoded:
            data = valid_manifest()
            data["files"][0]["path"] = "config/model.json"
            data["files"][0]["url"] = PREFIX + suffix
            self._refuse(data, self.cache_root)

    def test_missing_role(self):
        data = valid_manifest()
        del data["files"][0]["role"]
        self._refuse(data, self.cache_root)

    def test_unsupported_role(self):
        data = valid_manifest()
        data["files"][0]["role"] = "texture-config"
        self._refuse(data, self.cache_root)

    def test_missing_required_role(self):
        for missing in ("shape-config", "shape-weights"):
            data = valid_manifest()
            data["files"] = [
                file for file in data["files"] if file["role"] != missing
            ]
            self._refuse(data, self.cache_root)

    def test_sorted_inventory_is_deterministic(self):
        data = valid_manifest()
        data["files"].reverse()
        binding = self._bind(data, self.cache_root)
        paths = [file.path for file in binding.files]
        self.assertEqual(
            paths,
            [
                "auxiliary/tokenizer.json",
                "config/model.json",
                "weights/model.safetensors",
            ],
        )

    def test_binding_is_immutable_and_to_dict_is_defensive(self):
        data = valid_manifest()
        binding = self._bind(data, self.cache_root)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            binding.revision = "other"  # type: ignore[misc]

        emitted = binding.to_dict()
        emitted["files"].append({"path": "mutated", "role": "shape-auxiliary", "size": 1, "sha256": "0" * 64})
        self.assertEqual(len(binding.files), 3)
        emitted["files"][0]["path"] = "mutated"
        self.assertEqual(binding.files[0].path, "auxiliary/tokenizer.json")


if __name__ == "__main__":
    unittest.main()
