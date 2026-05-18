# Unit-Tests Gate Contract

The `build-and-publish-app.yaml` reusable workflow refuses to publish a version
unless the app's Tests workflow has reported a successful `unit-tests` check
(or whatever name is passed via `unit_tests_check_name`) for the same SHA.

This document defines the contract every consumer repo must satisfy.

## Why this exists

Before this gate, an app could ship to production with broken unit tests or
sub-threshold coverage because the publish step only ran an image scan. The
gate closes that gap automatically: a missing or failing check blocks publish
at minute 0, before any build minutes are spent.

Tracked under [DISTR-456 — App Certification Framework][distr-456].

[distr-456]: https://linear.app/atlan-epd/issue/DISTR-456/app-certification-framework

## Contract — what every consumer repo must provide

1. **A workflow file** in `.github/workflows/` (conventionally `tests.yml`)
   containing **a job that produces a check named `unit-tests`** (or override
   via `unit_tests_check_name` in your `build-and-publish.yaml`).
2. **Trigger coverage** — the workflow MUST trigger on:
   - `push` to `main`, AND
   - `push` (or `workflow_dispatch`) on any branch you publish from via
     `workflow_dispatch` of `build-and-publish.yaml`.
3. **Coverage enforcement** — the job MUST fail when unit-test coverage drops
   below the agreed floor. Default: **60%**. Use
   `coverage report --fail-under=60` (or equivalent for non-pytest stacks).

If any of these is missing, the gate fails at minute 0 with a link back to
this doc.

## Behaviour at publish time

| State | Gate result |
| --- | --- |
| Check absent on the SHA being published | Fails immediately with onboarding instructions |
| Check present, still running | Polls every 30s, max 30 min, then fails as timeout |
| Check present, `conclusion = success` | Proceeds to `prepare` → build → publish |
| Check present, `conclusion != success` | Fails immediately with the failing run's conclusion |

## Reference template — `.github/workflows/tests.yml`

The example below mirrors what `atlan-automation-engine-app` ships today.
Adapt `tests/unit`, threshold, and any app-specific prerequisites
(Dapr, AWS creds, etc.) to your repo.

```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
    branches: [main]

jobs:
  unit-tests:
    name: unit-tests
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      pull-requests: write
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

      - name: Run unit tests with coverage gate
        run: |
          uv run coverage run -m pytest tests/unit
          uv run coverage xml --fail-under=60
          uv run coverage html --fail-under=60
          uv run coverage report --fail-under=60
```

> **Job name = check name.** The `name:` field on the job is what shows up in
> the GitHub Checks API, and that is what the gate searches for. If you rename
> it from `unit-tests`, pass the new name to the reusable workflow via
> `unit_tests_check_name`.

## Caller-side override (optional)

If your app's check is named differently from the default, pass an explicit
name in your `build-and-publish.yaml`:

```yaml
jobs:
  build-and-publish:
    uses: atlanhq/application-sdk/.github/workflows/build-and-publish-app.yaml@main
    with:
      publish: ${{ github.event.inputs.publish != 'false' }}
      unit_tests_check_name: "py-unit-tests"   # <-- override
    secrets: inherit
```

## Branch publishes (`workflow_dispatch`)

When you dispatch a publish from a feature branch:

- The Tests workflow MUST be configured to trigger on that branch (most repos
  trigger Tests on `push` to any branch — confirm yours does).
- If Tests has not run on the dispatched SHA, the gate fails with the
  onboarding error. There is no opt-out — branch publishes get the same
  coverage bar as `main` publishes.

If you genuinely need to bypass the gate for a one-off hotfix, raise it in
`#pod-app-distribution`; the SDK maintainers will guide you through the
release-cert override path on the marketplace side instead of patching the
gate.

## Operational notes

- The gate uses GitHub's Checks API (`/commits/{sha}/check-runs`). The reusable
  workflow's `permissions:` block already includes `checks: read`.
- The wait step uses
  [`fountainhead/action-wait-for-check`](https://github.com/fountainhead/action-wait-for-check)
  pinned to a SHA per `CLAUDE.md` supply-chain rules.
- The 30-minute wait ceiling is generous — most unit-test runs complete in
  under 10 minutes. If your tests routinely take longer than 30 minutes,
  split them or stop calling them unit tests.
