from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-container.yml"
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python"}
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")


class OnKeyLoader(yaml.SafeLoader):
    """Load YAML while preserving the top-level GitHub `on` key as a string."""


def _construct_yaml_bool(loader: OnKeyLoader, node: yaml.ScalarNode) -> object:
    value = node.value
    if value == "on":
        return "on"
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    return value


OnKeyLoader.add_constructor("tag:yaml.org,2002:bool", _construct_yaml_bool)


def load_workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=OnKeyLoader)


def step_names(workflow: dict, job_id: str = "publish") -> list[str]:
    return [step.get("name", "") for step in workflow["jobs"][job_id]["steps"]]


class GhcrWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(PUBLISH_WORKFLOW)
        self.raw = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    def test_single_publish_workflow_exists(self) -> None:
        publish_workflows = [path for path in WORKFLOWS if path.name == "publish-container.yml"]
        self.assertEqual(len(publish_workflows), 1)

    def test_only_trigger_is_workflow_dispatch(self) -> None:
        on = self.workflow["on"]
        self.assertEqual(set(on), {"workflow_dispatch"})
        forbidden = {
            "pull_request",
            "pull_request_target",
            "push",
            "schedule",
            "workflow_call",
            "workflow_run",
            "repository_dispatch",
        }
        self.assertFalse(forbidden & set(on))

    def test_runner_and_environment_contract(self) -> None:
        job = self.workflow["jobs"]["publish"]
        self.assertEqual(job["runs-on"], ["self-hosted", "Windows", "X64", "pharaon-publisher"])
        self.assertEqual(job["environment"], "ghcr-publish")
        self.assertNotIn("ubuntu-latest", self.raw)
        self.assertNotIn("windows-latest", self.raw)
        self.assertNotIn("macos-latest", self.raw)
        self.assertNotIn("matrix", self.raw)
        self.assertNotIn("inputs.runner", self.raw)

    def test_permissions_are_least_privilege(self) -> None:
        top = self.workflow["permissions"]
        job = self.workflow["jobs"]["publish"]["permissions"]
        self.assertEqual(top, {"contents": "read", "packages": "write"})
        self.assertEqual(job, {"contents": "read", "packages": "write"})
        self.assertNotIn("id-token", self.raw)
        self.assertNotIn("write-all", self.raw)
        self.assertNotIn("PAT", self.raw)

    def test_inputs_and_preflight_requirements(self) -> None:
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        confirm = inputs["confirm_publish"]
        self.assertTrue(confirm["required"])
        self.assertNotIn("default", confirm)
        self.assertEqual(confirm["type"], "string")
        self.assertTrue(inputs["expected_sha"]["required"])
        self.assertEqual(inputs["expected_sha"]["type"], "string")
        self.assertEqual(inputs["release_tag"]["default"], "")

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
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"uses:\s*([^\s#]+)(?:\s*#\s*v?\S+)?", text):
                ref = match.group(1)
                action, _, suffix = ref.rpartition("@")
                self.assertIn(action, ALLOWED_ACTIONS, f"{path}: disallowed action {action}")
                self.assertRegex(suffix, ACTION_SHA, f"{path}: {ref} is not a full SHA pin")
                self.assertIn("# v", text[match.start() : match.end()])

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
        names = step_names(self.workflow)
        self.assertLess(names.index("Refuse existing requested tags"), names.index("Build and push immutable container"))

    def test_docker_credential_isolation_and_cleanup(self) -> None:
        self.assertIn("RUNNER_TEMP", self.raw)
        self.assertIn("DOCKER_CONFIG_NAME", self.raw)
        self.assertIn("--password-stdin", self.raw)
        self.assertIn("if: always()", self.raw)
        self.assertIn("docker --config $config logout ghcr.io", self.raw)
        self.assertIn("Remove-Item -LiteralPath $config -Recurse -Force", self.raw)
        self.assertNotIn("$env:USERPROFILE\\.docker", self.raw)

    def test_timeout_concurrency_and_preflight_ordering(self) -> None:
        job = self.workflow["jobs"]["publish"]
        self.assertEqual(job["timeout-minutes"], 180)
        self.assertEqual(job["concurrency"]["cancel-in-progress"], False)
        self.assertIn("ghcr-publish-pharaon-asset-factory", self.raw)
        names = step_names(self.workflow)
        self.assertLess(names.index("Docker and D-drive preflight"), names.index("Authenticate to GHCR with temporary Docker config"))
        self.assertLess(names.index("Docker and D-drive preflight"), names.index("Build and push immutable container"))
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
