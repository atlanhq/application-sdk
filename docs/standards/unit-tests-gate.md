# Unit-Tests Gate Contract

> **Most apps no longer need this.** The generic certification layers — v3
> shape, contract drift, and **unit tests + coverage** — now run centrally in
> the `certify` job with no per-app wiring. See
> [`app-certification.md`](./app-certification.md). This gate remains for apps
> that want the publish step to additionally block on their **own** test
> workflow (e.g. integration/e2e jobs that the centralized job can't run).

The `build-and-publish-app.yaml` reusable workflow can block a publish until
the app's `unit_tests_workflow_file` has completed successfully for the
publish SHA. The gate tracks runs by ID (not by check name), so apps don't
need any particular naming convention on their test jobs.

This document defines the contract a consumer repo must satisfy **if it opts
into this gate**.

## Why this exists

Before this gate, an app could ship to production with broken unit tests or
sub-threshold coverage because the publish step only ran an image scan. The
gate closes that gap automatically: a failing tests run blocks publish at
minute 0, before any build minutes are spent.

Tracked under [DISTR-456 — App Certification Framework][distr-456].

[distr-456]: https://linear.app/atlan-epd/issue/DISTR-456/app-certification-framework

## How the gate works

1. Resolves the publish SHA (`inputs.ref || github.sha`).
2. Looks for an existing run of `unit_tests_workflow_file` on that commit:
   - **Found** → reuses that run.
   - **Not found** → dispatches the workflow on the publish ref and captures
     the new run's ID.
3. Polls the run by ID every 30 s until `status: completed`.
4. Gate passes iff `conclusion: success`. Any other conclusion blocks publish
   with a link to the failing run.

## Contract — what every consumer repo MUST provide

1. **A workflow file** in `.github/workflows/` (commonly `unit-tests.yml`,
   `tests.yml`, or `checks.yml`).
2. **`workflow_dispatch:` trigger** in the workflow's `on:` block — without
   this, the gate cannot fire fresh runs on commits that haven't been tested.
3. **Pass / fail signal** — the workflow's overall conclusion must reflect
   whether tests passed. If the workflow contains multiple jobs (e.g. unit
   + e2e), any required failure must bubble up to the run's conclusion.

## Caller setup

To opt into this gate, a consumer of the SDK's `build-and-publish-app.yaml`
sets `unit_tests_workflow_file`:

```yaml
jobs:
  build-and-publish:
    uses: atlanhq/application-sdk/.github/workflows/build-and-publish-app.yaml@main
    with:
      publish: ${{ github.event.inputs.publish != 'false' }}
      unit_tests_workflow_file: "unit-tests.yml"   # opt-in
    secrets: inherit
```

The input is **optional** and defaults to `""`. When unset, this gate is
**skipped entirely** — apps onboard at their own pace and existing callers are
unaffected. (The generic unit-coverage check still runs in the centralized
`certify` job regardless; see [`app-certification.md`](./app-certification.md).)

## Reference template — `.github/workflows/unit-tests.yml`

```yaml
name: Unit Tests

on:
  push:
    branches: [main]
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    branches: [main]
  workflow_dispatch:    # required by the gate

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v4.9.1
        with:
          python-version: "3.13"

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          version: "0.7.13"
          cache: true

      - name: Install dependencies
        run: uv sync --all-extras

      - name: Run unit tests with coverage
        run: |
          uv run coverage run -m pytest tests/unit
          uv run coverage report --fail-under=0
```

> The job name and check name don't matter — the gate keys on the run, not
> on a check label.

## Behaviour matrix

| Publish SHA state | Gate behaviour |
| --- | --- |
| Tests workflow already ran on this commit, conclusion `success` | Gate reuses the existing run; passes in seconds |
| Tests workflow already ran on this commit, conclusion `failure` | Gate reuses the existing run; fails immediately with a link |
| Tests workflow already ran on this commit, still `in_progress` | Gate polls the existing run until completion |
| Tests workflow has not run on this commit | Gate dispatches `unit_tests_workflow_file` on the publish ref and waits |
| `unit_tests_workflow_file` doesn't exist / lacks `workflow_dispatch:` | Gate fails at dispatch with onboarding instructions |
| `unit_tests_workflow_file` not set by caller | Gate is skipped entirely (input defaults to `""`) |

## Branch publishes (`workflow_dispatch` from a feature branch)

The gate dispatches the tests workflow on `inputs.ref || github.ref_name`. If
you're publishing from `feat/my-branch`, the tests workflow runs on that
branch.

There is no opt-out — branch publishes get the same coverage bar as `main`
publishes.

For a one-off hotfix, raise in `#pod-app-distribution`; the SDK maintainers
will route to the release-cert override path on the marketplace side instead
of patching the gate.

## Operational notes

- The gate runs the tests workflow's natural duration plus ~10–30 s of API
  polling overhead. For most apps this adds well under a minute to publish.
- The 30-min gate timeout is generous. Most unit-tests runs complete within
  10 min. If your tests routinely run longer, split them or stop calling
  them unit tests.
- The gate uses GitHub's Actions API (`/repos/{repo}/actions/runs/{id}`). The
  reusable workflow's `permissions:` block already includes `actions: write`
  (for dispatching) and `checks: read`.
