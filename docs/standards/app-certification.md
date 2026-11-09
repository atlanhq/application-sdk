# App Certification (centralized)

The `build-and-publish-app.yaml` reusable workflow runs a **`certify`** job
before it publishes a version. The job centralizes every certification layer
that needs **no per-app wiring**, so every app is certified at publish time
without editing its own workflows.

Tracked under [DISTR-456 — App Certification Framework][distr-456].

[distr-456]: https://linear.app/atlan-epd/issue/DISTR-456/app-certification-framework

## What it checks

The job checks out the app source (plus the SDK, for the migration tooling)
and runs:

| Layer | Command | Skips when |
|-------|---------|------------|
| **v3 shape** | `tools.migrate_v3.check_migration` — no deprecated imports, `@task`-only, typed `Input`/`Output`, async clients, … | SDK install fails (annotated as incomplete) |
| **Contract drift** | the SDK's contract regeneration must leave the whole working tree clean — see [Contract drift: the whole-tree rule](#contract-drift-the-whole-tree-rule) | no pkl contract under `contract/` (fails, not skips, when the contract does not regenerate) |
| **Unit tests** | `pytest tests/unit --cov=app --cov-report=term` | no `tests/unit/` directory |
| **Coverage threshold** | `coverage report --fail-under=85` | unit tests skipped or failed |

Unit tests need the app's dependencies installed
(`uv sync --all-extras --all-groups --frozen`). If that install fails, they
cannot run, and publish is blocked (see [Enforcement](#enforcement)). Contract
drift needs only `pkl`, so it runs either way.

### Private dependencies and GitHub tokens

An app with a private atlanhq git dependency sets `private_git_auth: true`.
The install step then fetches the app's private atlanhq git dependencies with
a fleet App token, minted with `contents: read` and scoped to those
repositories. Only git sees it, in a step of its own: the dependencies are
mirror-cloned with the token and that step exits. The install step then runs
`uv sync` against the local mirrors with no token in its environment or in
any ancestor process's. So no build backend `uv sync` runs, and none of the unit-test and
coverage steps, which run app code, have a token to read. Those steps use `uv run --frozen` against what the install
synced, so they never fetch.

There is no `ORG_PAT_GITHUB` fallback for this credential. If the App token
does not mint (`FLEET_APP_ID` / `FLEET_APP_PRIVATE_KEY` missing), the install
fails and publish is blocked. Pass the secrets with `secrets: inherit`.

Both certify jobs also run with a `contents: read` job token, and no checkout
leaves a token in `.git/config`.

These are the layers that are **identical for every app**. App-specific layers
— integration, SDR, and e2e/Playwright — are **not** run here: they need the
app's own live stack (databases, Dapr, Temporal) and secrets, so they stay in
the app's own CI (`tests.yaml`, the marketplace-releases e2e reusables, …).

## Contract drift: the whole-tree rule

From the app checkout's root, the step regenerates the contract with the SDK's
own regeneration: the same function the generated-freshness check and
Renovate's toolkit bump use (`renovate_pkl_sync.regenerate`). That is
`pkl eval` of `contract/app.pkl` (or each per-entrypoint root), then the app's
`contract/post-generate.sh` if it has one, then ruff on generated Python. It
never runs the app's own `poe generate`: that is app-defined shell, and a
second generator is how committed output drifted from what the gates expect.
Then it runs:

```
git status --porcelain --untracked-files=all
```

Any output fails the step. Every path the generator touches counts, not just
one directory:

- a committed file that regeneration changes or removes, wherever it is
  (`app/generated/_input.py`, a root-level `atlan.yaml` or `app.yaml`, …);
- a file regeneration creates that was never committed.

Files matched by the app's `.gitignore` don't count. The `::error::`
annotation lists the offending paths (at most 50) and never their contents.

To fix a failure, run `pkl eval --project-dir contract -m . contract/app.pkl`
(then `contract/post-generate.sh`, if the app has one) and commit the result.
Any post-processing an app does to generated output (moving a file, a second
eval, installing a frontend bundle) must live in the contract's output paths or
in `contract/post-generate.sh`; anything else is undone by regeneration and
fails this check. If the output is genuinely build-time only and must not be
committed, add it to `.gitignore`.

## Install environment

- **Python.** Certify runs on the Python the app image ships, not on the
  runner's system `python3`. It sets `UV_PYTHON` to the major.minor of the SDK
  `Dockerfile`'s golden base tag (`container_python_version.py print-version`),
  the same source `app-runtime-base` is built from. This overrides an app's
  `.python-version`. Unpinned, the runner's newer Python made deps with no
  wheels for it build from source and fail.
- **System packages.** An app whose Python dependencies build a native
  extension the runner lacks headers for sets the `apt_packages` input to a
  space-separated list of Ubuntu packages. For example, hive's `pykerberos`
  needs `libkrb5-dev`. They install before `uv sync`, in `certify` and in
  every `certify-unit` leg. Each name must be a plain Debian package name; any
  other value fails the gate. Pass the same list as `tests-reusable.yaml`'s
  `apt-packages`.

## Sharded unit tests (opt-in)

By default `certify` runs `tests/unit` itself in one job, exactly as before,
and no extra jobs start. An app that sets nothing stays on this path.

An app whose suite outgrows one runner can opt in by setting the
`unit_test_shards` input to a leg count from 2 to 10. The tests then run in
parallel `certify-unit` legs, and `certify` folds the legs' results and
combined coverage into the verdict:

```
certify-unit (N legs in parallel) ─> certify (v3 shape, contract drift, verdict)
```

- **Default `1`:** no split, no extra jobs.
- **`2`–`10`:** that many legs. Any other value fails the run.
- **Balance:** pytest-split balances the legs from the app's committed
  `.test_durations` when it has one, and splits evenly by test count when it
  does not. Generate the file with
  `uv run --with pytest-split pytest tests/unit --store-durations` and commit
  it. A stale file only unbalances the legs, never skips a test.
- A leg with no result (crashed, timed out, upload lost) counts as failed.
- Opting in costs about one billed setup minute per leg (checkout and
  `uv sync`), in exchange for a much shorter wait before publish. There is no
  separate planning job: the leg list is looked up from the input directly.

## Why centralized (no per-app wiring)

The earlier model required each app to opt in by passing
`unit_tests_workflow_file` and adding a `workflow_dispatch:` trigger to its
test workflow (see [`unit-tests-gate.md`](./unit-tests-gate.md)). That gate
dispatches the app's **own** workflow, so it could only ever be opt-in.

The generic layers above don't need to know anything about the app's workflows
— the SDK can run them directly against the checked-out source. Centralizing
them means an app gets v3-shape, contract-drift, and unit-coverage certification
for free, with zero changes to its repo.

## Enforcement

| Layer | Mode |
|-------|------|
| **Unit tests** | **Enforced** — a ❌ exits the verdict step non-zero and blocks publish. |
| **Contract drift** | **Enforced** — a ❌ on the step fails the job (no `continue-on-error`) and blocks publish. |
| **App install** | **Enforced** — if the app's dependencies do not install (in `certify`, or in any `certify-unit` leg), contract drift and unit tests cannot run, so the verdict exits non-zero and blocks publish. |
| **Coverage threshold** | Warn-only — annotated, does not block. |
| **v3 shape** | Warn-only — annotated, does not block. |

Unit test pass/fail and contract drift are enforced: a failing run blocks
publish. Coverage threshold and v3 shape remain warn-only until their
[exit plan](#exit-plan-for-the-warn-only-layers) flips them —
every check runs and the verdict is annotated on the workflow summary, but
only a test failure, a contract-drift failure, or an app install failure
exits non-zero.

Checks that don't apply to an app skip cleanly (➖) so non-conforming apps are
never hard-failed before they onboard. In particular, an app with no
`tests/unit/` directory skips both the unit and coverage checks (➖), and an
app with no `contract/app.pkl` skips the contract-drift check. An app whose
dependencies fail to install does **not** skip: the checks it cannot run count
as failed, so an app is never published uncertified.

## Exit plan for the warn-only layers

The two warn-only layers will become enforcing. Each has its own ticket, owner
and target date. Neither flips until the fleet has been measured against it, so
the flip does not block publishes on the day it lands.

| Layer | Tracked in | Target | Before the flip |
|-------|------------|--------|-----------------|
| **Coverage threshold** | [FND-4784][fnd-4784] | 2026-12-18 | Measure every active app's coverage from its latest `certify` run and set the threshold from that data. Apps below it get a fix or a dated exception. |
| **v3 shape** | [FND-4785][fnd-4785] | 2027-01-29 | Count active apps that fail v3 shape. Flip once the fleet is on v3. |

If your app fails either layer today, the run summary already says so. Fixing
it now means the flip is a no-op for you. Questions go to
`#pod-app-distribution`.

[fnd-4784]: https://linear.app/atlan-epd/issue/FND-4784
[fnd-4785]: https://linear.app/atlan-epd/issue/FND-4785

## Relationship to the other layers

`certify` is one of the pre-build gates in `build-and-publish-app.yaml`:

```
validate-channel ─┐
unit-tests-gate ──┤
certify ──────────┴─> prepare ─> build ─> merge ─> security-scan ─> publish
```

It complements — does not replace — the image security scan
(`build-and-scan.yaml`, Trivy + allowlist), which runs after the build.
