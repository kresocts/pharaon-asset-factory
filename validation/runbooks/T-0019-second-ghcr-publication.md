# T-0019 operator runbook: second controlled GHCR publication

This runbook is for the single controlled SHA-only publication of
`ghcr.io/kresocts/pharaon-asset-factory`. The workflow and security contract are fixed;
this runbook does not weaken them.

## Prerequisites

1. `origin/main` is fetched and the working tree is clean.
2. PR #15 is merged.
3. T-0018 is `DONE`.
4. T-0017 remains `REVIEW`.
5. T-0010 through T-0016 and T-0018 are `DONE`.
6. `python validation/phase0_status.py` reports `PHASE_0_READY`.
7. Every `uses:` entry in every repository workflow is pinned to a full 40-character
   immutable commit SHA.
8. No T-0019 ticket, branch, or PR exists before Stage A.
9. The Stage A preparation PR is merged and the owner sends:
   `T-0019 preparation PR is merged. Continue with Stage B.`

## Stage A merge procedure warning

- The Stage A preparation PR must be merged without changing T-0019 to DONE.
- Do not use an automation that merges the PR and automatically marks the ticket DONE.
- T-0019 must remain REVIEW throughout Stage B and evidence review.

## Exact no-retry rule

Only one production publication attempt is allowed. If any workflow step fails, stop the
runner immediately, preserve diagnostics, do not dispatch another run, do not overwrite
an existing SHA tag, and keep T-0019 in `REVIEW` for independent review.

## Exact `main` SHA capture procedure

After Stage A is merged and the owner authorizes Stage B:

1. Fetch `origin`.
2. Switch to `main`.
3. Pull with `--ff-only`.
4. Verify the working tree is clean.
5. Verify the Stage A PR is merged.
6. Verify T-0019 exists on `main` and remains `REVIEW`.
7. Verify T-0018 is `DONE`.
8. Verify T-0017 failure evidence remains unchanged.
9. Record `git rev-parse HEAD`.
10. Verify the SHA is exactly 40 lowercase hexadecimal characters.
11. Verify `HEAD == origin/main`.

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

   - the Stage A PR is merged
   - T-0019 exists on `main`
   - T-0019 status is still `REVIEW`
   - T-0018 is `DONE`
   - T-0017 remains `REVIEW`
   - T-0010 through T-0016 and T-0018 are `DONE`
   - T-0017 failure evidence is unchanged

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
   pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'
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

9. Verify `D:\actions-runner` exists.
10. Verify runner `pharaon` is Offline immediately before dispatch.
11. Inspect GitHub Actions for unexpected queued or running jobs.

    Stop if any PR, fork, or unrelated workflow job could target a self-hosted Windows
    runner or the `pharaon-publisher` labels.

12. Record the exact current `main` SHA that will be supplied as `expected_sha`.

If `main` changes at any time before dispatch, discard the prepared values and rerun the
entire preflight.

## Exact workflow inputs

Use only these values:

- branch: `main`
- `confirm_publish`: `PUBLISH`
- `expected_sha`: exact current full 40-character lowercase `main` SHA
- `release_tag`: empty

Show these values to the owner before dispatch and require explicit confirmation:

`DISPATCH T-0019 SHA-ONLY PUBLICATION`

Dispatch the canonical workflow exactly once. Do not use a release tag. Do not dispatch
a second run.

## Empty release-tag requirement

The second publication is SHA-only. `release_tag` must remain empty, and no `latest`,
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

After dispatch, record the workflow run ID and URL. Confirm the event is
`workflow_dispatch`, the source branch is `main`, the source SHA equals `expected_sha`,
`confirm_publish` is `PUBLISH`, release tag is empty, the expected job is waiting for
`ghcr-publish`, and the runner is still Offline. Then stop and tell the owner to approve
the `ghcr-publish` environment in GitHub. Do not bypass or programmatically approve the
environment. Continue only after the owner confirms:

`ghcr-publish environment approved for T-0019.`

## Queued-job inspection

After environment approval, verify the expected publication job is queued for the labels
`self-hosted`, `Windows`, `X64`, and `pharaon-publisher`. Verify no unexpected PR or fork
job is queued for the runner. Stop if any unexpected job could target the self-hosted
runner.

## Manual runner startup

Do not start the runner automatically. Display this exact command to the owner:

```powershell
cd D:\actions-runner
pwsh --version
.\run.cmd
```

Stop. The owner must manually start the runner in a fresh PowerShell window. Continue
only after the owner confirms:

`pharaon runner is online and processing the expected T-0019 job.`

## Workflow observation

Monitor only the expected workflow run. Do not trigger another run, do not modify the
workflow during execution, do not retry automatically, and do not use a release tag.
Capture:

- workflow run URL and ID
- exact source commit
- runner name
- start and end times
- workflow conclusion
- failed step, if any
- SHA tag
- digest
- digest-qualified image reference
- Linux AMD64 verification
- cleanup/logout result
- package visibility observed

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
8. Keep T-0019 in `REVIEW`.
9. Stop for independent review and proceed only to failure evidence creation.

No automatic retry is permitted.

## Evidence fields

After verified success or failure and runner shutdown, update
`validation/records/T-0019-second-ghcr-publication.md` with observed facts only:

- publication UTC time
- exact main SHA
- HEAD equals origin/main
- PowerShell version
- Docker version
- Docker OSType
- Buildx version
- D-drive free space
- runner status before dispatch
- unexpected self-hosted jobs absent
- workflow run ID and URL
- exact input values
- environment approval
- runner status before startup
- workflow conclusion
- SHA tag
- digest
- digest-qualified reference
- Linux AMD64 result
- release tag absent
- latest tag not created by this run
- package visibility observed
- credential cleanup result
- runner manually stopped
- runner final status Offline
- no weights
- no paid/cloud resources
- final result

Do not expose tokens, Docker auth JSON, runner credentials, or secret values.

## Evidence branch and PR

After the runner is confirmed Offline, create a fresh evidence branch from current
`main`, for example `ticket/T-0019-publication-evidence`. Update only the T-0019
evidence record, ticket documentation if needed, and minimal related documentation
derived from observed facts. Do not modify executable workflow or publisher code. Set
the evidence status to `PASS` for success or `FAILED` for failure. Open exactly one
evidence PR against `main`, wait for baseline CI, and keep T-0019 in `REVIEW`. Do not
approve or merge the evidence PR.

## Status transition to DONE

Do not change the ticket status to `DONE` during publication. After the real evidence is
independently reviewed, the owner may make a final status-only commit changing
`status: REVIEW` to `status: DONE`.
