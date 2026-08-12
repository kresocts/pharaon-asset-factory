# Ticket Specifications

Files named `T-XXXX.md` are the canonical implementation specifications and workflow records. A ticket should be small enough for one worker to implement and validate in one focused session.

## Machine-readable metadata

Every ticket begins with this restricted YAML front matter:

```yaml
---
id: T-0000
title: Short title
status: READY
dependencies: []
priority: 1
---
```

The repository validator supports scalar values and an inline dependency list; this deliberate subset avoids a third-party YAML dependency. Fields are:

- `id`: unique `T-` plus four digits; must match the filename.
- `title`: concise, non-empty title.
- `status`: one of `READY`, `IN_PROGRESS`, `REVIEW`, `BLOCKED`, or `DONE`.
- `dependencies`: existing ticket IDs; an empty list means no dependencies.
- `priority`: positive integer where `1` is highest priority.

Status meanings:

- `READY` — dependencies are satisfied and a worker may begin.
- `IN_PROGRESS` — a worker is actively implementing it.
- `REVIEW` — implementation is awaiting or undergoing independent review.
- `BLOCKED` — a recorded dependency or external condition prevents progress.
- `DONE` — every acceptance criterion is satisfied and the required workflow is complete.

## Required body

Each ticket must contain these level-two sections:

1. `Goal`
2. `Context`
3. `Dependencies`
4. `Allowed scope`
5. `Acceptance criteria`
6. `Required tests`
7. `Out of scope`
8. `Implementation notes`

The metadata dependency list is authoritative for automation; the prose section explains why. Acceptance criteria must be objective and must not be weakened during implementation or review. Update status in the same pull request when a transition is justified.
