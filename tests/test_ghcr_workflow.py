from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-container.yml"
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python"}
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block_after(raw: str, marker: str) -> str:
    match = re.search(rf"^\s*{re.escape(marker)}\s*$", raw, flags=re.MULTILINE)
    if not match:
        return ""
    marker_indent = len(match.group(0)) - len(match.group(0).lstrip())
    lines = raw[match.end() :].splitlines()
    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "":
            continue
        line_indent = len(line) - len(line.lstrip(" \t"))
        if line_indent <= marker_indent:
            break
        block.append(line)
    return "\n".join(block)


def _list_after(raw: str, marker: str) -> list[str]:
    match = re.search(rf"^\s*{re.escape(marker)}\s*\n((?:^\s*-\s+.*\n?)+)", raw, flags=re.MULTILINE)
    if not match:
        return []
    return [line.strip() for line in re.findall(r"^\s*-\s+(.+)$", match.group(1), flags=re.MULTILINE)]


class GhcrWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _text(PUBLISH_WORKFLOW)

    def test_single_publish_workflow_exists(self) -> None:
        publish_workflows = [path for path in WORKFLOWS if path.name == "publish-container.yml"]
        self.assertEqual(len(publish_workflows), 1)

    def test_only_trigger_is_workflow_dispatch(self) -> None:
        on_block = _block_after(self.raw, "on:")
        trigger_names = []
        for line in on_block.splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and line.startswith("  ") and not line.startswith("    "):
                trigger_names.append(stripped[:-1])
        self.assertEqual(trigger_names, ["workflow_dispatch"])
        forbidden = [
            "pull_request",
            "pull_request_target",
            "push",
            "schedule",
            "workflow_call",
            "workflow_run",
            "repository_dispatch",
        ]
        for name in forbidden:
            self.assertNotIn(f"{name}:", on_block)

    def test_runner_and_environment_contract(self) -> None:
        labels = _list_after(self.raw, "runs-on:")
        self.assertEqual(labels, ["self-hosted", "Windows", "X64", "pharaon-publisher"])
        self.assertIn("environment: ghcr-publish", self.raw)
        for hosted in ["ubuntu-latest", "windows-latest", "macos-latest"]:
            self.assertNotIn(hosted, self.raw)
        self.assertNotIn("matrix", self.raw)
        self.assertNotIn("inputs.runner", self.raw)

    def test_permissions_are_least_privilege(self) -> None:
        pre_jobs = self.raw.split("jobs:")[0]
        permissions = _block_after(pre_jobs, "permissions:")
        self.assertIn("contents: read", permissions)
        self.assertIn("packages: write", permissions)
        self.assertNotIn("id-token", self.raw)
        self.assertNotIn("write-all", self.raw)
        self.assertNotIn("PAT", self.raw)

    def test_inputs_and_preflight_requirements(self) -> None:
        confirm = _block_after(self.raw, "confirm_publish:")
        self.assertIn("required: true", confirm)
        self.assertNotIn("default:", confirm)
        self.assertIn("type: string", confirm)

        expected = _block_after(self.raw, "expected_sha:")
        self.assertIn("required: true", expected)
        self.assertIn("type: string", expected)

        release = _block_after(self.raw, "release_tag:")
        self.assertIn('default: ""', release)
        self.assertIn("required: false", release)

        for required_text in [
            "confirm_publish must be exactly PUBLISH",
            "expected_sha does not exactly match github.sha",
            "Publication is refused for repository",
            "Publication is refused for ref",
            "github.sha is not a full 40-character lowercase hex SHA",
            'release_tag must be an immutable version such as v1.0.0 or v1.2.3-rc.1',
        ]:
            self.assertIn(required_text, self.raw)

    def test_release_tag_policy(self) -> None:
        for mutable in [
            "latest",
            "stable",
            "current",
            "main",
            "master",
            "dev",
            "edge",
            "nightly",
            "rolling",
            "snapshot",
        ]:
            self.assertIn(mutable, self.raw)
        self.assertIn('$releaseTag -cmatch "^v(0|[1-9][0-9]*)', self.raw)

    def test_every_uses_reference_is_pinned_full_sha_and_allowlisted(self) -> None:
        for path in WORKFLOWS:
            text = _text(path)
            for match in re.finditer(r"uses:\s*([^\s#]+)(?:\s*#\s*v?\S+)?", text):
                ref = match.group(1)
                action, _, suffix = ref.rpartition("@")
                self.assertIn(action, ALLOWED_ACTIONS, f"{path}: disallowed action {action}")
                self.assertRegex(suffix, ACTION_SHA, f"{path}: {ref} is not a full SHA pin")
                self.assertIn("# v", match.group(0))

    def test_image_tag_and_digest_contract(self) -> None:
        self.assertIn("ghcr.io/kresocts/pharaon-asset-factory", self.raw)
        self.assertIn('"sha-$env:GITHUB_SHA"', self.raw)
        self.assertIn('"$env:IMAGE`:sha-$env:GITHUB_SHA"', self.raw)
        self.assertNotIn(":latest", self.raw)
        self.assertIn("org.opencontainers.image.source", self.raw)
        self.assertIn("org.opencontainers.image.revision", self.raw)
        self.assertIn("org.opencontainers.image.version", self.raw)
        self.assertIn("org.opencontainers.image.description", self.raw)
        self.assertIn("containerimage.digest", self.raw)
        self.assertIn("GITHUB_STEP_SUMMARY", self.raw)

    def test_existing_tag_refusal_and_fail_closed_registry_checks(self) -> None:
        self.assertIn("Refuse existing requested tags", self.raw)
        self.assertIn("docker --config $config manifest inspect", self.raw)
        self.assertIn("Requested tag already exists", self.raw)
        self.assertIn("Registry or authentication error", self.raw)
        self.assertLess(
            self.raw.index("Refuse existing requested tags"),
            self.raw.index("Build and push immutable container"),
        )

    def test_docker_credential_isolation_and_cleanup(self) -> None:
        self.assertIn("RUNNER_TEMP", self.raw)
        self.assertIn("DOCKER_CONFIG_NAME", self.raw)
        self.assertIn("--password-stdin", self.raw)
        self.assertIn("if: always()", self.raw)
        self.assertIn("docker --config $config logout ghcr.io", self.raw)
        self.assertIn("Remove-Item -LiteralPath $config -Recurse -Force", self.raw)
        self.assertNotIn("$env:USERPROFILE\\.docker", self.raw)

    def test_timeout_concurrency_and_preflight_ordering(self) -> None:
        self.assertRegex(self.raw, r"timeout-minutes:\s*180")
        self.assertIn("cancel-in-progress: false", self.raw)
        self.assertIn("ghcr-publish-pharaon-asset-factory", self.raw)
        self.assertLess(
            self.raw.index("Docker and D-drive preflight"),
            self.raw.index("Authenticate to GHCR with temporary Docker config"),
        )
        self.assertLess(
            self.raw.index("Docker and D-drive preflight"),
            self.raw.index("Build and push immutable container"),
        )
        self.assertIn("D:\\actions-runner", self.raw)
        self.assertIn("MIN_DISK_FREE_GIB", self.raw)
        self.assertNotIn("prune", self.raw.lower())

    def test_no_forbidden_automation_or_cloud_runner_paths(self) -> None:
        forbidden_patterns = [
            r"run\.cmd",
            r"runner\.configure",
            r"svc\.(sh|bat)",
            r"start\.cmd",
            r"huggingface",
            r"models acquire",
            r"--confirm-download",
            r"\.safetensors",
            r"\.ckpt",
            r"\.pth",
            r"ubuntu-latest",
            r"windows-latest",
            r"macos-latest",
        ]
        for pattern in forbidden_patterns:
            self.assertNotRegex(self.raw, pattern)


if __name__ == "__main__":
    unittest.main()