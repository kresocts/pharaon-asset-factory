"""Repository parsing and Git operations behind a testable boundary."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from validation.validate_repository import Ticket, ValidationError, parse_ticket


SECTION = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
CRITERION = re.compile(r"^- (?:\[(?: |x|X)\]\s+)?(.+?)\s*$", re.MULTILINE)
BULLET = re.compile(r"^-\s+`?(.+?)`?\s*$", re.MULTILINE)


@dataclass(frozen=True)
class TicketDocument:
    metadata: Ticket
    sections: dict[str, str]


def load_ticket_document(path: Path) -> TicketDocument:
    metadata = parse_ticket(path)
    text = path.read_text(encoding="utf-8")
    matches = list(SECTION.finditer(text))
    sections = {
        match.group(1): text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else None].strip()
        for index, match in enumerate(matches)
    }
    return TicketDocument(metadata, sections)


def acceptance_criteria(document: TicketDocument) -> tuple[str, ...]:
    return tuple(CRITERION.findall(document.sections["Acceptance criteria"]))


def required_tests(document: TicketDocument) -> tuple[str, ...]:
    return tuple(BULLET.findall(document.sections["Required tests"]))


def canonical_branch(ticket_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"ticket/{ticket_id}-{slug}"


class GitRepository:
    """Small Git adapter; core preparation can use a fake in tests."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            ("git", *arguments), cwd=self.root, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def ensure_ticket_branch(self, branch: str, base: str = "origin/main") -> str:
        """Reuse the one canonical branch or create it from the specified base."""
        if self._run("branch", "--list", branch):
            self._run("switch", branch)
            return "reused-local"
        if self._run("branch", "--remotes", "--list", f"origin/{branch}"):
            self._run("switch", "--track", "-c", branch, f"origin/{branch}")
            return "reused-remote"
        self._run("switch", "-c", branch, base)
        return "created"

    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD")


def load_all_tickets(root: Path) -> dict[str, TicketDocument]:
    documents: dict[str, TicketDocument] = {}
    for path in sorted((root / "tickets").glob("T-*.md")):
        document = load_ticket_document(path)
        documents[document.metadata.ticket_id] = document
    return documents


def require_ticket_id(ticket_id: str) -> None:
    if not re.fullmatch(r"T-\d{4}", ticket_id):
        raise ValidationError(f"malformed ticket ID: {ticket_id!r}")
