# Pharaon Asset Factory

Pharaon Asset Factory is the foundation for a future reproducible AI game-asset generation pipeline. The intended system will turn a prompt or reference image into a processed, game-ready GLB with metadata and package the results as a ZIP archive.

## Current status

Phase 0 is being established. This repository currently contains project governance, architecture guidance, ticket specifications, metadata validation, tests, and minimal CI. It does **not** yet contain image generation, Hunyuan3D integration, GPU provisioning, a production container, or a web interface.

## Intended architecture

The long-term pipeline is:

```text
prompt/reference → image stage → GPU 3D worker → shape → texture
                 → post-processing → GLB → metadata/report → ZIP
```

Control-plane coordination will use repository and GitHub state so that workers and reviewers can be short-lived and reproducible. GPU execution will eventually run in a container on ephemeral local or rented NVIDIA hardware. See [the architecture overview](architecture/overview.md) and [roadmap](PLAN.md).

## Development model

Work is divided into small tickets. A disposable worker implements one ticket on a ticket-specific branch and opens a pull request. A separate reviewer checks the ticket, diff, tests, and CI before approving or requesting changes. Agents do not rely on persistent memory; durable context belongs in tickets, Git history, architecture records, and pull requests.

Future agents must begin by reading:

1. `AGENTS.md`
2. `PLAN.md`
3. The assigned file in `tickets/`
4. Relevant files in `architecture/`

They should then verify dependencies, create `ticket/T-XXXX-short-description` from current `main`, remain within allowed scope, run required tests, and use the pull request template.

## Repository structure

- `AGENTS.md` — mandatory operating contract for workers and reviewers
- `PLAN.md` — phased roadmap
- `architecture/` — system, security, and decision documentation
- `tickets/` — canonical specifications and machine-readable ticket state
- `validation/` — dependency-free repository metadata validator
- `tests/` — validator tests
- `.github/` — issue/PR templates and minimal CI

Application, container, and provider-specific directories will be introduced only by tickets that need them.

## Ticket workflow

Ticket metadata uses YAML front matter with an ID, workflow status, dependencies, and priority. The human-readable body defines immutable acceptance criteria and required tests. Status changes are committed so future orchestration can derive state from the repository. See `tickets/README.md`.

Run the bootstrap checks with:

```bash
python -m unittest discover -s tests -v
python validation/validate_repository.py
```

## Security and cost control

Secrets never belong in Git. Future cloud operations must use least-privilege credentials, explicit user approval, bounded retries, price/runtime/job limits, secure artifact retrieval, and automatic teardown. No paid resource is created by the current repository. See `architecture/security.md`.
