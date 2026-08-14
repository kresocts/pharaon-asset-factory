# T-0017 first controlled GHCR publication evidence

Status: FAILED

## Run identity

- Workflow run ID: 31800647785
- Workflow run URL: https://github.com/kresocts/pharaon-asset-factory/actions/runs/31800647785
- Workflow: Publish container to GHCR
- Exact source commit: 60d62bb6cbee247799da3a64ea86f62b935f20d9
- Branch: main
- Trigger: workflow_dispatch
- Dispatch count: 1
- Environment approval completed: Yes
- Runner manually started: Yes
- Runner manually stopped: Yes
- Runner final state: Offline

## Observed failure

- Failed step: Refuse existing requested tags
- Observed absent-tag message: Requested tag is not present: sha-60d62bb6cbee247799da3a64ea86f62b935f20d9
- Process exit code: 1
- Build not reached: Yes
- Push: None
- Digest: None
- SHA tag absent: Yes
- Release tag absent: Yes
- Latest tag absent: Yes
- No weights: Yes
- No paid/cloud resources: Yes
- No automatic retry: Yes

## Verification notes

- Workflow conclusion: failure
- Release tag input was empty: Yes
- No release tag published by this run: Yes
- No latest tag published by this run: Yes
- Docker auth JSON and credentials omitted from this record: Yes

## Operator checkpoints

- Stage A preparation PR merged: Yes
- Owner authorized Stage B: Yes
- Owner approved ghcr-publish: Yes
- Owner manually started runner: Yes
- Owner manually stopped runner: Yes

This record preserves the failed T-0017 run observed facts and does not mark T-0017 DONE.
A separate T-0019 publication ticket will be prepared only after T-0018 is merged and
independently approved.
