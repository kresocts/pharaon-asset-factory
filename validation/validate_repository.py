#!/usr/bin/env python3
"""Validate Pharaon Asset Factory repository and ticket metadata."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


TICKET_ID = re.compile(r"T-\d{4}")
ALLOWED_STATUSES = {"READY", "IN_PROGRESS", "REVIEW", "BLOCKED", "DONE"}
METADATA_FIELDS = {"id", "title", "status", "dependencies", "priority"}
REQUIRED_SECTIONS = {
    "Goal",
    "Context",
    "Dependencies",
    "Allowed scope",
    "Acceptance criteria",
    "Required tests",
    "Out of scope",
    "Implementation notes",
}
REQUIRED_PATHS = {
    "AGENTS.md",
    "PLAN.md",
    "README.md",
    "architecture/overview.md",
    "architecture/security.md",
    "architecture/decisions/README.md",
    "tickets/README.md",
    ".github/ISSUE_TEMPLATE/implementation-ticket.yml",
    ".github/pull_request_template.md",
    ".github/workflows/metadata.yml",
    ".gitignore",
}


class ValidationError(ValueError):
    """Raised for invalid repository metadata."""


@dataclass(frozen=True)
class Ticket:
    path: Path
    ticket_id: str
    title: str
    status: str
    dependencies: tuple[str, ...]
    priority: int


def _parse_dependencies(raw: str, path: Path) -> tuple[str, ...]:
    if not (raw.startswith("[") and raw.endswith("]")):
        raise ValidationError(f"{path}: dependencies must be an inline list")
    inner = raw[1:-1].strip()
    if not inner:
        return ()
    values = tuple(value.strip().strip("'\"") for value in inner.split(","))
    if any(not TICKET_ID.fullmatch(value) for value in values):
        raise ValidationError(f"{path}: dependencies contain an invalid ticket ID")
    if len(values) != len(set(values)):
        raise ValidationError(f"{path}: dependencies contain duplicates")
    return values


def parse_ticket(path: Path) -> Ticket:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValidationError(f"{path}: missing opening front-matter delimiter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as error:
        raise ValidationError(f"{path}: missing closing front-matter delimiter") from error

    metadata: dict[str, str] = {}
    for line_number, line in enumerate(lines[1:closing], 2):
        if not line.strip() or ":" not in line:
            raise ValidationError(f"{path}:{line_number}: invalid metadata line")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in metadata:
            raise ValidationError(f"{path}: duplicate metadata field {key}")
        metadata[key] = value.strip("'\"")

    missing = METADATA_FIELDS - metadata.keys()
    unknown = metadata.keys() - METADATA_FIELDS
    if missing:
        raise ValidationError(f"{path}: missing metadata fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValidationError(f"{path}: unknown metadata fields: {', '.join(sorted(unknown))}")

    ticket_id = metadata["id"]
    if not TICKET_ID.fullmatch(ticket_id):
        raise ValidationError(f"{path}: invalid ticket ID {ticket_id!r}")
    if path.stem != ticket_id:
        raise ValidationError(f"{path}: ticket ID must match filename")
    if not metadata["title"]:
        raise ValidationError(f"{path}: title must not be empty")
    if metadata["status"] not in ALLOWED_STATUSES:
        raise ValidationError(f"{path}: invalid status {metadata['status']!r}")
    try:
        priority = int(metadata["priority"])
    except ValueError as error:
        raise ValidationError(f"{path}: priority must be a positive integer") from error
    if priority < 1:
        raise ValidationError(f"{path}: priority must be a positive integer")

    headings = {line[3:].strip() for line in lines[closing + 1 :] if line.startswith("## ")}
    missing_sections = REQUIRED_SECTIONS - headings
    if missing_sections:
        raise ValidationError(f"{path}: missing sections: {', '.join(sorted(missing_sections))}")

    dependencies = _parse_dependencies(metadata["dependencies"], path)
    if ticket_id in dependencies:
        raise ValidationError(f"{path}: ticket cannot depend on itself")

    return Ticket(path, ticket_id, metadata["title"], metadata["status"], dependencies, priority)


def validate_repository(root: Path) -> list[Ticket]:
    errors: list[str] = []
    for relative_path in sorted(REQUIRED_PATHS):
        if not (root / relative_path).is_file():
            errors.append(f"missing required file: {relative_path}")

    ticket_directory = root / "tickets"
    ticket_paths = sorted(ticket_directory.glob("T-*.md")) if ticket_directory.is_dir() else []
    if not ticket_paths:
        errors.append("no ticket files found")

    tickets: list[Ticket] = []
    for path in ticket_paths:
        try:
            tickets.append(parse_ticket(path))
        except (OSError, UnicodeError, ValidationError) as error:
            errors.append(str(error))

    by_id = {ticket.ticket_id: ticket for ticket in tickets}
    if len(by_id) != len(tickets):
        errors.append("duplicate ticket IDs found")
    for expected in (f"T-{number:04d}" for number in range(1, 6)):
        if expected not in by_id:
            errors.append(f"missing required bootstrap ticket: {expected}")
    for ticket in tickets:
        for dependency in ticket.dependencies:
            if dependency not in by_id:
                errors.append(f"{ticket.path}: dependency does not exist: {dependency}")

    if errors:
        raise ValidationError("Repository validation failed:\n- " + "\n- ".join(errors))
    return tickets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        tickets = validate_repository(args.root.resolve())
    except ValidationError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Repository metadata is valid ({len(tickets)} tickets checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
