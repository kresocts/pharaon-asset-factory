# T-0017 operator runbook: first controlled GHCR publication

This runbook is for the single controlled SHA-only publication of
`ghcr.io/kresocts/pharaon-asset-factory`. The workflow and security contract are fixed;
this runbook does not weaken them.

## Prerequisites

1. `origin/main` is fetched and the working tree is clean.
2. PR #13 is merged.
3. T-0010 through T-0016 are all `DONE`.
4. `python validation/phase0_status.py` reports `PHASE_0_READY`.
5. Every `uses:` entry in every repository workflow is pinned to a full 40-character
   immutable commit SHA.
6. No T-0017 ticket, branch, PR, or partial implementation exists before Stage A.
7. The Stage A preparation PR is merged and the owner sends:
   `T-0017 preparation PR is merged. Continue with Stage B.`

## Stage A merge procedure warning

- PR #14 must be merged without changing T-0017 to DONE.
- Do not use an automation that merges the PR and automatically marks the ticket DONE.
- T-0017 must remain REVIEW throughout Stage B and evidence review.

## Exact no-retry rule

Only one production publication attempt is allowed. If any workflow step fails, stop the
runner immediately, preserve diagnostics, do not dispatch another run, do not overwrite
an existing SHA tag, and keep T-0017 in `REVIEW` for independent review.

## Exact `main` SHA capture procedure

After Stage A is merged and the owner authorizes Stage B:

1. Fetch `origin/main`.
2. Switch to `main`.
3. Pull with `--ff-only`.
4. Verify the working tree is clean.
5. Verify the Stage A PR is merged.
6. Verify T-0017 exists on `main` and remains `REVIEW`.
7. Record `git rev-parse HEAD`.
8. Verify the SHA is exactly 40 lowercase hexadecimal characters.
9. Verify `HEAD == origin/main`.

## Mandatory Stage B pre-dispatch preflight

Run every check in this order. Any failed preflight check stops Stage B before workflow
dispatch.

1. Fetch and update `main`:

   ```powershell
   git fetch origin
   git switch main
   git pull --ff-only origin main
   ```

2. Verify the working tree is clean:

   ```powershell
   git status --porcelain
   ```

   Any output is a blocker.

3. Capture and compare commits:

   ```powershell
   git rev-parse HEAD
   git rev-parse origin/main
   ```

   They must be identical full 40-character lowercase SHAs.

4. Verify:

   - PR #14 is merged
   - T-0017 exists on `main`
   - T-0017 status is still `REVIEW`
   - T-0010 through T-0016 are `DONE`

5. Re-run:

   ```powershell
   python -m unittest tests.test_publisher_logic -v
   python -m unittest tests.test_ghcr_workflow -v
   python -m unittest discover -s tests -v
   python validation/run_ci.py
   python validation/validate_repository.py
   python validation/phase0_status.py
   git diff --check
   ```

6. Verify PowerShell 7 because the production workflow uses `shell: pwsh`:

   ```powershell
   pwsh --version
   ```

   If `pwsh` is unavailable, stop. Do not dispatch the workflow and do not silently
   substitute Windows PowerShell 5.1.

7. Verify Docker Desktop:

   ```powershell
   docker version
   docker info --format '{{.OSType}}'
   docker buildx version
   ```

   `OSType` must be exactly `linux`.

8. Verify D-drive capacity using PowerShell and record the result:

   ```powershell
   Get-PSDrive D
   ```

   At least 150 GiB free is required.

9. Verify runner `pharaon` is Offline immediately before dispatch.
10. Inspect GitHub Actions for unexpected queued or running jobs.

    Stop if any PR, fork, or unrelated workflow job could target a self-hosted Windows
    runner or the `pharaon-publisher` labels.

11. Record the exact current `main` SHA that will be supplied as `expected_sha`.

## Exact workflow inputs

Use only these values:

- branch: `main`
- `confirm_publish`: `PUBLISH`
- `expected_sha`: exact current full 40-character lowercase `main` SHA
- `release_tag`: empty

Show these values to the owner before dispatch and require explicit confirmation:

`DISPATCH T-0017 SHA-ONLY PUBLICATION`

Dispatch the canonical workflow exactly once. Do not use a release tag. Do not dispatch
a second run.

## Empty release-tag requirement

The first publication is SHA-only. `release_tag` must remain empty, and no `latest`,
`main`, `stable`, `current`, `dev`, `edge`, `nightly`, `rolling`, or `snapshot` tag may
be requested or created.

## Runner gating order

Preserve this exact required order:

1. Runner confirmed Offline.
2. Check there are no unexpected self-hosted jobs.
3. Dispatch the SHA-only workflow once.
4. Verify the expected job is waiting for `ghcr-publish`.
5. Confirm the runner is still Offline.
6. Owner manually approves the environment.
7. Verify only the expected publication job is queued for:
   - self-hosted
   - Windows
   - X64
   - pharaon-publisher
8. Reconfirm no unexpected self-hosted job is queued.
9. Only then instruct the owner to start `D:\actions-runner\run.cmd`.

If the runner is unexpectedly Online at any point before Gate C, do not approve the
environment and do not continue.

## Environment approval checkpoint

After dispatch, record the workflow run ID and URL. Confirm the expected publication job
is waiting for `ghcr-publish`, the source branch is `main`, the source SHA equals
`expected_sha`, and no release tag was supplied. Then stop and tell the owner to approve
the `ghcr-publish` environment in GitHub. Do not bypass or programmatically approve the
environment. Continue only after the owner confirms:

`ghcr-publish environment approved.`

## Queued-job inspection

After environment approval, verify the expected publication job is queued for the labels
`self-hosted`, `Windows`, `X64`, and `pharaon-publisher`. Verify no unexpected PR or fork
job is queued for the runner. Stop if any unexpected job could target the self-hosted
runner.

## Manual runner startup

Do not start the runner automatically. Display this exact command to the owner:

```powershell
cd D:\actions-runner
.\run.cmd
```

Stop. The owner must manually start the runner. Continue only after the owner confirms:

`pharaon runner is online and processing the expected T-0017 job.`

## Workflow observation

Monitor only the expected workflow run. Do not trigger another run, do not modify the
workflow during execution, do not retry automatically, and do not use a release tag.
Capture:

- workflow run URL
- exact source commit
- runner name
- start and end times
- workflow conclusion
- SHA tag
- digest
- digest-qualified image reference
- Linux AMD64 verification
- cleanup/logout result

## Immediate runner shutdown

As soon as the workflow succeeds or fails, instruct the owner to:

1. Press `Ctrl+C` in the runner window.
2. Confirm with `Y` if prompted.
3. Verify runner status returns to `Offline`.

Do not mark the publication successful until the runner is confirmed `Offline`.

## Success verification

On successful workflow completion, verify:

- workflow conclusion is `success`
- source commit equals `expected_sha`
- published tag is `sha-<full-main-sha>`
- digest matches `sha256:<64 lowercase hex>`
- manifest is Linux AMD64
- digest-qualified reference is available
- release tag input was empty
- no release tag was published by this run
- no `latest` tag was published by this run
- temporary Docker credentials were cleaned
- runner is `Offline`
- no model weights were included or downloaded
- no paid or cloud resources were used

Observe GHCR package visibility. Do not change package visibility automatically. If
visibility must be changed later, report it as a separate manual decision.

## Failure procedure

If any workflow step fails:

1. Stop the runner immediately.
2. Verify the runner returns `Offline`.
3. Do not dispatch another run.
4. Do not overwrite an existing SHA tag.
5. Record the failed step.
6. Record whether the SHA tag was created.
7. Preserve the workflow URL and logs.
8. Keep T-0017 in `REVIEW`.
9. Stop for independent review.

No automatic retry is permitted.

## Evidence fields

After verified success and runner shutdown, update
`validation/records/T-0017-first-ghcr-publication.md` with observed facts only:

- exact tested `main` commit
- workflow run URL and ID
- immutable SHA tag
- digest
- digest-qualified reference
- Linux AMD64 result
- release tag absent
- latest tag not created by the run
- environment approval completed
- runner manually started
- runner manually stopped
- runner final state `Offline`
- package visibility observed
- no weights
- no paid/cloud resources
- final result `PASS`

Do not expose tokens, Docker auth JSON, runner credentials, or secret values.

## Status transition to DONE

Do not change the ticket status to `DONE` during publication. After the real evidence is
independently reviewed, the owner may make a final status-only commit changing
`status: REVIEW` to `status: DONE`.
