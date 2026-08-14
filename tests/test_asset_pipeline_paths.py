"""Focused tests for shape-job path policy and image validation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asset_pipeline import paths


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 9
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP"


def _document(reference_image: str = "references/pharaoh.png") -> dict:
    return {
        "schema_version": 1,
        "job_id": "pharaoh-001",
        "reference_image": reference_image,
        "seed": 12345,
        "remove_background": True,
    }


class PathsTests(unittest.TestCase):
    def _make_roots(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        input_dir = base / "input"
        output_dir = base / "output"
        workspace_dir = base / "workspace"
        for directory in (input_dir, output_dir, workspace_dir):
            directory.mkdir()
        return base, input_dir, output_dir, workspace_dir

    def _write_input(self, input_dir: Path, relative: str, content: bytes) -> Path:
        target = input_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def test_valid_plan_is_deterministic_and_non_mutating(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.png", PNG_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        plan = paths.build_plan(_document(), roots)
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["status"], "VALID")
        self.assertEqual(plan["classification"], "SHAPE_JOB_CONTRACT_READY")
        self.assertFalse(plan["execution_supported"])
        self.assertEqual(plan["stage"], "shape")
        self.assertEqual(plan["requirements"]["inference_backend"], "hunyuan3d-2.1-shape")
        self.assertEqual(
            plan["requirements"]["model_weights"], "REQUIRED_BUT_NOT_CONFIGURED"
        )
        self.assertEqual(
            plan["requirements"]["gpu"], "REQUIRED_FOR_FUTURE_EXECUTION"
        )
        self.assertFalse((output_dir / "pharaoh-001").exists())
        self.assertFalse((workspace_dir / "pharaoh-001").exists())

        second = paths.build_plan(_document(), roots)
        self.assertEqual(plan, second)

    def test_missing_input_image_is_refused(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError) as caught:
            paths.build_plan(_document(), roots)
        self.assertEqual(caught.exception.exit_code, 2)

    def test_non_regular_input_is_refused(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        (input_dir / "references").mkdir(parents=True)
        (input_dir / "references" / "pharaoh.png").mkdir()
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError):
            paths.build_plan(_document(), roots)

    def test_empty_input_image_is_refused(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.png", b"")
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError):
            paths.build_plan(_document(), roots)

    def test_image_size_limit_is_enforced(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.png", PNG_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with mock.patch("asset_pipeline.paths.MAX_INPUT_IMAGE_BYTES", 4):
            with self.assertRaises(paths.InputPolicyError) as caught:
                paths.build_plan(_document(), roots)
        self.assertIn("byte limit", str(caught.exception))

    def test_wrong_signature_is_refused(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.png", JPEG_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError) as caught:
            paths.build_plan(_document(), roots)
        self.assertIn("does not match", str(caught.exception))

    def test_unsupported_extension_is_refused(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.txt", PNG_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError):
            paths.build_plan(_document("references/pharaoh.txt"), roots)

    def test_jpeg_and_webp_signatures_are_accepted(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/a.jpg", JPEG_BYTES)
        self._write_input(input_dir, "references/b.webp", WEBP_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        for reference in ("references/a.jpg", "references/b.webp"):
            with self.subTest(reference=reference):
                plan = paths.build_plan(_document(reference), roots)
                self.assertEqual(plan["status"], "VALID")

    def test_existing_output_or_workspace_target_is_refused(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.png", PNG_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        (output_dir / "pharaoh-001").mkdir()
        with self.assertRaises(paths.SafePathError) as caught:
            paths.build_plan(_document(), roots)
        self.assertEqual(caught.exception.exit_code, 3)

        (output_dir / "pharaoh-001").rmdir()
        (workspace_dir / "pharaoh-001").mkdir()
        with self.assertRaises(paths.SafePathError):
            paths.build_plan(_document(), roots)

    def test_runtime_roots_must_be_absolute_existing_directories(self):
        with self.assertRaises(paths.RuntimeRootError):
            paths.load_runtime_roots(
                {
                    "INPUT_DIR": "relative",
                    "OUTPUT_DIR": "/data/output",
                    "WORKSPACE_DIR": "/workspace",
                }
            )
        with tempfile.TemporaryDirectory() as temp:
            missing = str(Path(temp) / "missing")
            with self.assertRaises(paths.RuntimeRootError):
                paths.load_runtime_roots(
                    {
                        "INPUT_DIR": missing,
                        "OUTPUT_DIR": missing,
                        "WORKSPACE_DIR": missing,
                    }
                )

    def test_planning_performs_no_writes(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        self._write_input(input_dir, "references/pharaoh.png", PNG_BYTES)
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with (
            mock.patch("os.mkdir") as mkdir,
            mock.patch("os.makedirs") as makedirs,
            mock.patch("os.remove") as remove,
            mock.patch("os.replace") as replace,
            mock.patch("tempfile.mkstemp") as mkstemp,
        ):
            paths.build_plan(_document(), roots)
        mkdir.assert_not_called()
        makedirs.assert_not_called()
        remove.assert_not_called()
        replace.assert_not_called()
        mkstemp.assert_not_called()

    def test_input_file_symlink_is_refused_when_supported(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        outside = base / "outside.png"
        outside.write_bytes(PNG_BYTES)
        link = input_dir / "references" / "pharaoh.png"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError):
            paths.build_plan(_document(), roots)

    def test_ancestor_symlink_escape_is_refused_when_supported(self):
        base, input_dir, output_dir, workspace_dir = self._make_roots()
        outside_dir = base / "outside"
        outside_dir.mkdir()
        (outside_dir / "pharaoh.png").write_bytes(PNG_BYTES)
        link = input_dir / "references"
        try:
            os.symlink(outside_dir, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(input_dir),
                "OUTPUT_DIR": str(output_dir),
                "WORKSPACE_DIR": str(workspace_dir),
            }
        )
        with self.assertRaises(paths.InputPolicyError):
            paths.build_plan(_document(), roots)


if __name__ == "__main__":
    unittest.main()
