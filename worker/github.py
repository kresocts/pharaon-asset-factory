"""Optional GitHub CLI boundary for stable one-PR-per-ticket behavior."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str


class GitHubCli:
    """Open or reuse a PR; intentionally exposes no approve or merge operation."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            ("gh", *arguments), cwd=self.root, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def ensure_pull_request(self, ticket_id: str, branch: str, body_file: Path) -> PullRequest:
        raw = self._run("pr", "list", "--state", "all", "--head", branch, "--json", "number,url,state")
        existing = json.loads(raw)
        if len(existing) > 1:
            raise RuntimeError(f"multiple pull requests already exist for {branch}")
        if existing:
            item = existing[0]
            if item["state"] != "OPEN":
                raise RuntimeError(f"pull request #{item['number']} for {branch} is {item['state']}")
            self._run(
                "pr", "edit", str(item["number"]), "--title", f"{ticket_id} implementation",
                "--body-file", str(body_file),
            )
            return PullRequest(item["number"], item["url"], item["state"])
        url = self._run(
            "pr", "create", "--base", "main", "--head", branch,
            "--title", f"{ticket_id} implementation", "--body-file", str(body_file),
        )
        item = json.loads(self._run("pr", "view", url, "--json", "number,url,state"))
        return PullRequest(item["number"], item["url"], item["state"])
