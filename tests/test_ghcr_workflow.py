from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-container.yml"
PUBLISHER_SCRIPT = ROOT / "scripts" / "publisher" / "publisher.ps1"
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python"}
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")
PINNED_ACTIONS = {
    ("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    ("actions/setup-python", "5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
}
STRICT_RELEASE_TAG = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:[0-9a-z]+(?:-[0-9a-z]+)*)(?:\.(?:[0-9a-z]+(?:-[0-9a-z]+)*))*)?$"
)
REGISTRY_ABSENCE_SIGNALS = (
    "MANIFEST_UNKNOWN",
    "NAME_UNKNOWN",
    "manifest unknown",
    "no such manifest",
)


def discover_workflows(root: Path = ROOT) -> list[Path]:
    workflow_dir = root / ".github" / "workflows"
    paths = set(workflow_dir.glob("*.yml")) | set(workflow_dir.glob("*.yaml"))
    return sorted(dict.fromkeys(paths))


WORKFLOWS = discover_workflows()


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _release_tag_allowed(tag: str) -> bool:
    if tag == "":
        return True
    if len(tag) > 128:
        return False
    if re.search(r"\s", tag):
        return False
    if "/" in tag:
        return False
    if tag.lower() != tag:
        return False
    if re.search(r"[&|;<>$]", tag):
        return False
    return bool(STRICT_RELEASE_TAG.fullmatch(tag))


def _registry_absence_confirmed(text: str) -> bool:
    lowered = text.lower()
    return any(signal.lower() in lowered for signal in REGISTRY_ABSENCE_SIGNALS)


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
        self.publisher = _text(PUBLISHER_SCRIPT)

    def _assert_action_policy(self, paths: list[Path]) -> None:
        uses_pattern = re.compile(r"uses:\s*([^\s#]+)(?:\s*#\s*(.*?))?\s*$")
        for path in paths:
            text = _text(path)
            for line in text.splitlines():
                match = uses_pattern.search(line)
                if not match:
                    continue
                ref = match.group(1)
                action, _, suffix = ref.rpartition("@")
                comment = (match.group(2) or "").strip()
                self.assertIn(action, ALLOWED_ACTIONS, f"{path}: disallowed action {action}")
                self.assertRegex(suffix, ACTION_SHA, f"{path}: {ref} is not a full SHA pin")
                self.assertIn(
                    (action, suffix, comment),
                    PINNED_ACTIONS,
                    f"{path}: unexpected action pin or release comment {ref} # {comment}",
                )

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
            "Assert-PublisherShaTag",
            "Assert-PublisherReleaseTag",
        ]:
            self.assertIn(required_text, self.raw)

    def test_release_tag_policy(self) -> None:
        self.assertIn("function Test-PublisherReleaseTag", self.publisher)
        self.assertIn("$ReleaseTag -cmatch \"^v(0|[1-9][0-9]*)", self.publisher)
        self.assertNotRegex(self.publisher, r"\+\[0-9a-z")
        self.assertNotIn("$segments", self.publisher)
        self.assertNotIn("$mutable", self.publisher)
        self.assertIn("Assert-PublisherReleaseTag -ReleaseTag $releaseTag", self.raw)

    def test_release_tag_grammar_adversarial(self) -> None:
        valid = [
            "v1.0.0",
            "v1.2.3-rc.1",
            "v2.0.0-beta.2",
            "v10.20.30-alpha.1-beta.2",
        ]
        invalid = [
            "v1.2.3+build.1",
            "v1.2.3-rc.",
            "v1.2.3-rc-",
            "v1.2.3-rc..1",
            "v1.2.3-.rc",
            "V1.2.3",
            "v01.2.3",
            "latest",
            "main",
            "stable",
            "current",
            "master",
            "dev",
            "edge",
            "nightly",
            "rolling",
            "snapshot",
            "v1.2.3/rc",
            "v1.2.3 rc",
            "v1.2.3$",
            "v1.0.0-" + "a" * 125,
        ]
        for tag in valid:
            self.assertTrue(_release_tag_allowed(tag), f"expected valid release tag: {tag}")
        for tag in invalid:
            self.assertFalse(_release_tag_allowed(tag), f"expected invalid release tag: {tag}")

    def test_every_uses_reference_is_pinned_full_sha_and_allowlisted(self) -> None:
        self._assert_action_policy(WORKFLOWS)

    def test_workflow_discovery_covers_yml_and_yaml(self) -> None:
        self.assertIn(PUBLISH_WORKFLOW, WORKFLOWS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_dir = root / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "one.yml").write_text(
                "name: one\non: workflow_dispatch\njobs: {}\n", encoding="utf-8"
            )
            (workflow_dir / "two.yaml").write_text(
                "name: two\non: workflow_dispatch\njobs: {}\n", encoding="utf-8"
            )
            discovered = discover_workflows(root)
            self.assertEqual([path.name for path in discovered], ["one.yml", "two.yaml"])

    def test_yaml_policy_discovery_detects_unpinned_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_dir = root / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            bad = (
                "name: bad\n"
                "on: workflow_dispatch\n"
                "jobs:\n"
                "  x:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@main # v7.0.1\n"
            )
            (workflow_dir / "bad.yaml").write_text(bad, encoding="utf-8")
            discovered = discover_workflows(root)
            self.assertEqual([path.name for path in discovered], ["bad.yaml"])
            with self.assertRaises(AssertionError):
                self._assert_action_policy(discovered)

    def test_image_tag_and_digest_contract(self) -> None:
        self.assertIn("ghcr.io/kresocts/pharaon-asset-factory", self.raw)
        self.assertIn("PUBLISHER_SCRIPT: scripts/publisher/publisher.ps1", self.raw)
        self.assertIn("Invoke-PublisherBuildxBuild", self.raw)
        self.assertIn("Read-PublisherBuildMetadataDigest", self.raw)
        self.assertIn("Assert-PublisherPushedDigest", self.raw)
        self.assertIn("Get-PublisherDigestQualifiedReference", self.raw)
        self.assertNotIn(":latest", self.raw)
        self.assertIn("org.opencontainers.image.source", self.raw)
        self.assertIn("org.opencontainers.image.revision", self.raw)
        self.assertIn("org.opencontainers.image.version", self.raw)
        self.assertIn("org.opencontainers.image.description", self.raw)
        self.assertIn("containerimage.digest", self.publisher)
        self.assertIn("GITHUB_STEP_SUMMARY", self.raw)
        self.assertNotIn("{{.Digest}}", self.raw)
        self.assertNotIn("{{json .Platforms}}", self.raw)
        self.assertIn('"--format"', self.publisher)
        self.assertIn('"{{json .}}"', self.publisher)
        self.assertIn("ConvertFrom-Json", self.publisher)
        self.assertIn(".manifest.digest", self.publisher)
        self.assertIn(".image.architecture", self.publisher)
        self.assertIn(".image.os", self.publisher)
        self.assertIn("platform.architecture", self.publisher)
        self.assertIn("platform.os", self.publisher)
        self.assertIn("$shaManifestDigest -ne $Digest", self.publisher)
        self.assertIn("$releaseManifestDigest -ne $Digest", self.publisher)
        self.assertIn('"${Image}:sha-${Sha}"', self.publisher)

    def test_existing_tag_refusal_and_fail_closed_registry_checks(self) -> None:
        self.assertIn("Refuse existing requested tags", self.raw)
        self.assertIn("Assert-PublisherTagsAbsent", self.raw)
        self.assertIn("function Test-PublisherRegistryTagState", self.publisher)
        self.assertIn("Requested tag already exists", self.publisher)
        self.assertIn("Registry or authentication error", self.publisher)
        self.assertIn("$absenceSignals = @(", self.publisher)
        self.assertIn("foreach ($signal in $absenceSignals)", self.publisher)
        self.assertNotIn('"not found"', self.publisher)
        self.assertLess(
            self.raw.index("Refuse existing requested tags"),
            self.raw.index("Build and push immutable container"),
        )

    def test_registry_absence_classifier_is_strict(self) -> None:
        for signal in REGISTRY_ABSENCE_SIGNALS:
            self.assertIn(signal, self.publisher)

        accepted = [
            "MANIFEST_UNKNOWN",
            "NAME_UNKNOWN",
            "manifest unknown",
            "no such manifest",
        ]
        rejected = [
            "credential helper not found",
            "404 Not Found",
            "unauthorized",
            "denied",
            "TLS failure",
            "timeout",
            "connection failure",
            "arbitrary unknown error",
        ]

        for text in accepted:
            self.assertTrue(_registry_absence_confirmed(text), f"expected absence: {text}")
        for text in rejected:
            self.assertFalse(_registry_absence_confirmed(text), f"expected hard failure: {text}")

    def test_docker_credential_isolation_and_cleanup(self) -> None:
        self.assertIn("RUNNER_TEMP", self.raw)
        self.assertIn("DOCKER_CONFIG_NAME", self.raw)
        self.assertIn("New-PublisherTemporaryDockerConfig", self.raw)
        self.assertIn("--password-stdin", self.raw)
        self.assertIn("if: always()", self.raw)
        self.assertIn("docker --config $config logout ghcr.io", self.raw)
        self.assertIn("Remove-Item -LiteralPath $config -Recurse -Force", self.raw)
        self.assertNotIn("$env:USERPROFILE\\.docker", self.raw)
        self.assertIn("New-Item -ItemType Directory -Path $configPath", self.publisher)

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
        self.assertNotIn("prune", self.raw.lower())

    def test_disk_threshold_is_numeric_and_trusted(self) -> None:
        self.assertNotIn("$env:MIN_DISK_FREE_GIB * 1GB", self.raw)
        self.assertNotIn("MIN_DISK_FREE_GIB", self.raw)
        self.assertIn("$requiredFreeGiB = [int64]150", self.raw)
        self.assertIn("$requiredFreeBytes = $requiredFreeGiB * 1GB", self.raw)
        self.assertIn("[int64]$drive.Free -lt $requiredFreeBytes", self.raw)
        self.assertLess(
            self.raw.index("$requiredFreeGiB = [int64]150"),
            self.raw.index("Authenticate to GHCR with temporary Docker config"),
        )

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
