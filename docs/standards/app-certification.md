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
| **Contract drift** | `poe generate` must produce no diff vs the committed `app/generated/` artifacts | no `contract/app.pkl`, or `poe` unavailable |
| **Unit + coverage** | `pytest tests/unit --cov=app --cov-fail-under=85` | no `tests/unit/` directory |

These are the layers that are **identical for every app**. App-specific layers
— integration, SDR, and e2e/Playwright — are **not** run here: they need the
app's own live stack (databases, Dapr, Temporal) and secrets, so they stay in
the app's own CI (`tests.yaml`, the marketplace-releases e2e reusables, …).

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

All three layers are **warn-only** during the initial rollout — every check runs
and the verdict is annotated on the workflow summary, but none exit non-zero or
block publish.

| Layer | Mode |
|-------|------|
| **Unit + coverage** | Warn-only — annotated, does not block. |
| **v3 shape** | Warn-only — annotated, does not block. |
| **Contract drift** | Warn-only — annotated, does not block. |

Checks that don't apply to an app skip cleanly (➖) so non-conforming apps are
never hard-failed before they onboard. In particular, an app with no
`tests/unit/` directory skips the unit check (➖).

## Known rollout gaps (before enforcement)

- **Coverage threshold fixed at 85%.** Before flipping unit + coverage to
  enforcing, verify active connector repos meet this bar. Raise the enforcement
  question in `#pod-app-distribution`.
- **Contract drift needs the pkl toolchain.** If `poe generate` requires pkl
  and it isn't installed on the runner, the check is recorded as a failure.
  Installing pkl in the `certify` job is a prerequisite for flipping
  contract-drift to blocking.

## Relationship to the other layers

`certify` is one of the pre-build gates in `build-and-publish-app.yaml`:

```
validate-channel ─┐
unit-tests-gate ──┤
certify ──────────┴─> prepare ─> build ─> merge ─> security-scan ─> publish
```

It complements — does not replace — the image security scan
(`build-and-scan.yaml`, Trivy + Snyk + allowlist), which runs after the build.
