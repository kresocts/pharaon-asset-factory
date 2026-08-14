from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_SCRIPT = ROOT / "scripts" / "publisher" / "publisher.ps1"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-container.yml"
INTEGRATION_SCRIPT = ROOT / "validation" / "run_local_publisher_integration.ps1"

STRICT_RELEASE_TAG = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:[0-9a-z]+(?:-[0-9a-z]+)*)(?:\.(?:[0-9a-z]+(?:-[0-9a-z]+)*))*)?$"
)
ABSENCE_SIGNALS = (
    "MANIFEST_UNKNOWN",
    "NAME_UNKNOWN",
    "manifest unknown",
    "no such manifest",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _release_tag_allowed(tag: str) -> bool:
    if tag == "":
        return True
    if len(tag) > 128 or re.search(r"\s", tag) or "/" in tag or tag.lower() != tag:
        return False
    if re.search(r"[&|;<>$]", tag):
        return False
    return bool(STRICT_RELEASE_TAG.fullmatch(tag))


class PublisherLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.publisher = _text(PUBLISHER_SCRIPT)
        self.workflow = _text(PUBLISH_WORKFLOW)
        self.integration = _text(INTEGRATION_SCRIPT)

    def test_release_tag_grammar(self) -> None:
        valid = ["", "v1.0.0", "v1.2.3-rc.1", "v2.0.0-beta.2"]
        invalid = [
            "v1.2.3+build.1",
            "v1.2.3-rc.",
            "V1.2.3",
            "latest",
            "main",
            "stable",
            "dev",
            "v1.2.3/rc",
            "v1.2.3 rc",
            "v1.0.0-" + "a" * 125,
        ]
        for tag in valid:
            self.assertTrue(_release_tag_allowed(tag), tag)
        for tag in invalid:
            self.assertFalse(_release_tag_allowed(tag), tag)

    def test_exact_sha_tag_generation(self) -> None:
        self.assertIn("Assert-PublisherShaTag", self.publisher)
        self.assertIn('$Sha -notmatch "^[0-9a-f]{40}$"', self.publisher)
        self.assertIn('"${Image}:sha-${Sha}"', self.publisher)

    def test_remote_tag_state_classifier(self) -> None:
        self.assertIn("function Test-PublisherRegistryTagState", self.publisher)
        self.assertIn("return \"Existing\"", self.publisher)
        self.assertIn("return \"Absent\"", self.publisher)
        self.assertIn("return \"Error\"", self.publisher)
        for signal in ABSENCE_SIGNALS:
            self.assertIn(signal, self.publisher)
        self.assertNotIn('"not found"', self.publisher.lower())
        self.assertIn("foreach ($signal in $absenceSignals)", self.publisher)

    def test_unrelated_errors_fail_closed(self) -> None:
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
        lowered = self.publisher.lower()
        for error in rejected:
            self.assertNotIn(error.lower(), lowered)

    def test_digest_parser(self) -> None:
        self.assertIn("function Read-PublisherBuildMetadataDigest", self.publisher)
        self.assertIn('"containerimage.digest"', self.publisher)
        self.assertIn('$digest -notmatch "^sha256:[0-9a-f]{64}$"', self.publisher)
        self.assertIn("Buildx metadata file is malformed.", self.publisher)

    def test_digest_mismatch_and_platform_mismatch(self) -> None:
        self.assertIn("$shaManifestDigest -ne $Digest", self.publisher)
        self.assertIn("$releaseManifestDigest -ne $Digest", self.publisher)
        self.assertIn('$platform.os -ne "linux"', self.publisher)
        self.assertIn('$platform.architecture -ne "amd64"', self.publisher)
        self.assertIn('$imageOs -ne "linux"', self.publisher)
        self.assertIn('$imageArch -ne "amd64"', self.publisher)

    def test_malformed_inspection_json(self) -> None:
        self.assertIn("ConvertFrom-Json", self.publisher)
        self.assertIn("returned malformed JSON", self.publisher)

    def test_multiple_platforms_rejected(self) -> None:
        self.assertIn("$platformManifests = @($Inspection.manifest.manifests)", self.publisher)
        self.assertIn("$platformManifests.Count -ne 1", self.publisher)

    def test_temporary_docker_config_isolation(self) -> None:
        self.assertIn("function New-PublisherTemporaryDockerConfig", self.publisher)
        self.assertIn("$configPath = Join-Path $BasePath $Name", self.publisher)
        self.assertIn("New-Item -ItemType Directory -Path $configPath", self.publisher)
        self.assertNotIn("$env:USERPROFILE\\.docker", self.publisher)
        self.assertNotIn("$env:USERPROFILE\\.docker", self.workflow)

    def test_safe_native_argument_construction(self) -> None:
        for forbidden in ("Invoke-Expression", "iex ", "Start-Process", "cmd /c"):
            self.assertNotIn(forbidden, self.publisher)
        self.assertIn("$dockerArgs = @(", self.publisher)
        self.assertIn("& docker @dockerArgs", self.publisher)
        self.assertNotRegex(self.publisher, r'docker\s+\$\w+\s+manifest')
        self.assertNotIn("$command = \"docker", self.publisher)

    def test_production_constants_are_fixed(self) -> None:
        self.assertIn("ghcr.io/kresocts/pharaon-asset-factory", self.workflow)
        self.assertIn("PLATFORM: linux/amd64", self.workflow)
        self.assertIn("DOCKERFILE: docker/Dockerfile", self.workflow)
        self.assertIn("BUILD_CONTEXT: .", self.workflow)
        self.assertIn("scripts/publisher/publisher.ps1", self.workflow)
        self.assertNotIn("inputs.image", self.workflow)
        self.assertNotIn("inputs.registry", self.workflow)
        self.assertNotIn("inputs.dockerfile", self.workflow)
        self.assertNotIn("inputs.context", self.workflow)
        self.assertNotIn("inputs.runner", self.workflow)
        self.assertNotIn("inputs.platform", self.workflow)

    def test_local_script_has_no_ghcr_or_runner_control(self) -> None:
        for forbidden in (
            "ghcr.io",
            "GITHUB_TOKEN",
            "actions/checkout",
            "workflow_dispatch",
            "run.cmd",
            "runner.configure",
            "svc.sh",
            "svc.bat",
        ):
            self.assertNotIn(forbidden, self.integration)

    def test_local_script_has_no_prune(self) -> None:
        self.assertNotIn("docker system prune", self.integration)
        self.assertNotIn("docker builder prune", self.integration)
        self.assertNotIn("docker buildx prune", self.integration)
        for command in ("docker system prune", "docker builder prune", "docker buildx prune"):
            self.assertNotIn(command, self.integration.lower())

    def test_local_script_requires_loopback(self) -> None:
        self.assertIn("127.0.0.1", self.integration)
        self.assertNotRegex(self.integration, r"0\.0\.0\.0|::\s")
        self.assertIn("RUN LOCAL PUBLISHER TEST", self.integration)
        self.assertIn("[switch]$PreflightOnly", self.integration)

    def _run_pwsh_exit(self, body: str) -> subprocess.CompletedProcess:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        env = os.environ.copy()
        env["REPO_ROOT"] = str(ROOT)
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            ". (Join-Path $env:REPO_ROOT 'scripts/publisher/publisher.ps1')\n"
            + body
        )
        return subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_integration_script_uses_explicit_exit_decision(self) -> None:
        self.assertIn("function Get-PublisherProcessExitCode", self.publisher)
        self.assertIn("$primaryFailure = $null", self.integration)
        self.assertIn("$primarySucceeded = $false", self.integration)
        self.assertIn("$cleanupFailures = New-Object System.Collections.Generic.List[string]", self.integration)
        self.assertIn("Get-PublisherProcessExitCode", self.integration)
        self.assertIn("exit $finalExitCode", self.integration)
        self.assertNotIn("Set-Content -LiteralPath $RecordPath", self.integration)

    def test_integration_cleanup_runs_in_finally_and_feeds_exit_code(self) -> None:
        self.assertIn("finally {", self.integration)
        self.assertIn("docker rm --force --volumes $registryContainer", self.integration)
        self.assertIn("Remove-Item -LiteralPath $tempBase -Recurse -Force", self.integration)
        self.assertIn("Get-PublisherProcessExitCode", self.integration)
        self.assertIn("CLEANUP_FAILURE=", self.integration)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is not available")
    def test_process_exit_failure_is_not_converted_by_later_write_host(self) -> None:
        result = self._run_pwsh_exit(
            "Write-Host 'later-success'\n"
            "exit (Get-PublisherProcessExitCode -PrimarySuccess $false -CleanupSuccess $true -EvidenceWritten $false)\n"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("later-success", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is not available")
    def test_process_exit_success_requires_cleanup_and_evidence(self) -> None:
        result = self._run_pwsh_exit(
            "exit (Get-PublisherProcessExitCode -PrimarySuccess $true -CleanupSuccess $true -EvidenceWritten $true)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is not available")
    def test_process_exit_cleanup_failure_is_nonzero(self) -> None:
        result = self._run_pwsh_exit(
            "exit (Get-PublisherProcessExitCode -PrimarySuccess $true -CleanupSuccess $false -EvidenceWritten $false)\n"
        )
        self.assertEqual(result.returncode, 1, result.stderr)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is not available")
    def test_pure_powershell_functions_when_pwsh_available(self) -> None:
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            ". (Join-Path $env:REPO_ROOT 'scripts/publisher/publisher.ps1')\n"
            "$valid = Test-PublisherReleaseTag -ReleaseTag 'v0.0.0-rc.1'\n"
            "$invalid = Test-PublisherReleaseTag -ReleaseTag 'latest'\n"
            "$tags = @(Get-PublisherTags -Image '127.0.0.1:5000/pharaon-asset-factory' -Sha "
            + "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' -ReleaseTag 'v0.0.0-rc.1')\n"
            "Write-Output ('VALID=' + $valid)\n"
            "Write-Output ('INVALID=' + $invalid)\n"
            "Write-Output ('COUNT=' + $tags.Count)\n"
            "Write-Output ('SHA=' + $tags[0])\n"
            "Write-Output ('RELEASE=' + $tags[1])\n"
        )
        env = os.environ.copy()
        env["REPO_ROOT"] = str(ROOT)
        shell = shutil.which("pwsh") or shutil.which("powershell")
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("VALID=True", lines)
        self.assertIn("INVALID=False", lines)
        self.assertIn("COUNT=2", lines)
        self.assertIn(
            "SHA=127.0.0.1:5000/pharaon-asset-factory:sha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            lines,
        )
        self.assertIn("RELEASE=127.0.0.1:5000/pharaon-asset-factory:v0.0.0-rc.1", lines)


if __name__ == "__main__":
    unittest.main()
