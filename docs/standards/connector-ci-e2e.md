# Connector CI: SDR + Full-DAG E2E

> **Audience:** Connector teams onboarding (or maintaining) one of the two end-to-end test pipelines this SDK ships.
> **Canonical reference adopter:** [`atlanhq/atlan-mysql-app`](https://github.com/atlanhq/atlan-mysql-app) — see its [`docs/CI-E2E.md`](https://github.com/atlanhq/atlan-mysql-app/blob/main/docs/CI-E2E.md) for the full connector-side walkthrough.

This doc covers what the SDK ships — the composite action, the reusable workflow, conventions, and inputs. Connector-side wiring lives in each connector repo; see the mysql-app walkthrough for a copy-pasteable example.

## What the SDK ships

| Component | Location | Purpose |
|---|---|---|
| `sdr-e2e` composite action | `.github/actions/sdr-e2e/action.yaml` | Build PR image, configurator + Dapr + Temporal stack-up, pytest, PR sticky comment, teardown. Used by both pipelines. |
| `e2e-full-reusable.yaml` reusable workflow | `.github/workflows/e2e-full-reusable.yaml` | Boilerplate (120-min timeout, concurrency group, env wiring, agent-name resolution) for the full-DAG pipeline. Connector repos `uses:` it as a 5-line wrapper. |
| `e2e-apps` cross-repo dispatcher | `.github/actions/e2e-apps/action.yaml` | Fires `workflow_dispatch` on the connector repo with the apps-sdk PR's head SHA. Polls for completion, surfaces a sticky status comment on the SDK PR. |
| `BaseSDRIntegrationTest` | `application_sdk/testing/sdr/` | pytest base for the SDR pipeline. Connector test class declares `Scenario(...)` instances. |
| `SQLAppE2EFullTest` / `BaseFullDAGE2ETest` | `application_sdk/testing/full_dag/` | pytest base for the full-DAG pipeline. Connector subclasses with `include_filter`, `expected_min_asset_counts`, `database_spec()`, etc. |

## The two pipelines

| Pipeline | What it validates | Stack | Wall time | Triggers |
|---|---|---|---|---|
| **SDR Integration Tests (testcontainer)** | Credential → secret-store → connector-client chain. Auth / preflight / extract polled to `COMPLETED` on CI tenant Temporal. | Hermetic — testcontainer DB + worker + Dapr + Temporal. | ~3 min | Auto on every connector PR push |
| **E2E Full Tests (system apps)** | Full DAG: connector extract → publish → query-intelligence → lineage-app → lineage-publish. Asset counts + lineage assertions in Atlas. | Live — configurator-generated compose, worker on a dynamic Temporal queue against the CI tenant's full Atlan stack. | ~20–40 min | Label-gated (`e2e-full`) |

Both call the same composite action; difference is test target, Dapr components, compose overlay, and secret-bundle shape.

## SDR composite action inputs

```yaml
- uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main
  with:
    app-name:           # REQUIRED. Connector short name (e.g. "mysql"). Used as the
                        # ATLAN_APPLICATION_NAME and label for log lines + artifacts.
    app-image-name:     # REQUIRED. GHCR image name (e.g. "atlan-mysql-app"). The
                        # composite tags as ghcr.io/atlanhq/<name>:sdr-test-<short-sha>.
    test-path:          # OPTIONAL. pytest target dir. Defaults: tests/sdr/.
    report-title:       # OPTIONAL. Override the auto-derived PR-comment title.
                        # Auto: tests/sdr/ → "SDR Integration Tests (testcontainer)",
                        #       tests/full_dag/ → "E2E Full Tests (system apps)".
    secrets-script:     # OPTIONAL. Path to the script that writes
                        # <sdr-config-dir>/secrets/credentials.json from env vars.
                        # Default: .github/sdr-e2e/make-secrets.sh.
    container-health-timeout-seconds:  # OPTIONAL. Default 120s. Bump for heavy native deps.
    pytest-extra-args:  # OPTIONAL. Appended to the pytest invocation.
    application-sdk-ref:               # OPTIONAL. Cross-repo dispatch: re-pin
                        # atlan-application-sdk in pyproject.toml to this ref
                        # before the docker build AND after setup-deps (so both
                        # the image and the host pytest runtime use the dispatched
                        # SDK).
    components-dir:     # OPTIONAL. App-level Dapr components dir. Defaults to
                        # $SDR_CONFIG_DIR/components. Override when SDR and
                        # full-DAG share one config dir but need different
                        # components (e.g. mysql's e2e-full-components/).
    compose-overlay:    # OPTIONAL. App-level docker-compose overlay. Defaults to
                        # $SDR_CONFIG_DIR/docker-compose.ci.yml. Override per-
                        # pipeline same as components-dir.
```

## `$SDR_CONFIG_DIR` resolution

The composite resolves a single connector config directory by checking, in order:

1. `.github/sdr-e2e/` — new convention introduced in [#1746](https://github.com/atlanhq/application-sdk/pull/1746).
2. `.github/e2e/` — legacy, still supported indefinitely.

Then it locates `app.yaml`:

1. `$SDR_CONFIG_DIR/app.yaml`
2. Repo root `app.yaml`

`app.yaml` shape (3 lines):

```yaml
app_name: <connector>
app_image: ${APP_IMAGE}    # envsubst'd at run time with the just-pushed image tag
app_port: 8000
```

The action runs `envsubst < app.yaml > app-resolved.yaml` and feeds it to `atlan-configurator --app`.

## Full-DAG reusable workflow inputs

```yaml
jobs:
  e2e-full:
    uses: atlanhq/application-sdk/.github/workflows/e2e-full-reusable.yaml@main
    with:
      app-name:                 # REQUIRED.
      app-image-name:           # REQUIRED.
      test-path:                # OPTIONAL. Default tests/full_dag/.
      secrets-script:           # OPTIONAL. Default .github/e2e/make-secrets-e2e-full.py.
      components-dir:           # OPTIONAL. Default .github/e2e/e2e-full-components.
      compose-overlay:          # OPTIONAL. Default .github/e2e/e2e-full-docker-compose.yaml.
      timeout-minutes:          # OPTIONAL. Default 120. Must be > ae_poll_timeout_seconds
                                # + atlas_poll_timeout_seconds + ~10 min build/setup overhead.
      agent-name-override:      # OPTIONAL. Default ci-<run_id>.
      application-sdk-ref:      # OPTIONAL. Cross-repo dispatch SDK pin.
      distinct-id:              # OPTIONAL. codex-/return-dispatch correlation id.
      clouds:                   # OPTIONAL. Default "" = the SDK's cloud list.
                                # See Cross-CSP matrix below; "none" disables it.
    secrets: inherit
```

Threaded secrets the reusable workflow expects on the caller side:

| Secret | Required | Used by |
|---|---|---|
| `E2E_TENANT_MATRIX_JSON` | for the cross-CSP matrix | Per-leg tenant + credentials. See [Cross-CSP matrix](#cross-csp-matrix). Org-level; shared with `application-sdk` and every `atlan-*-app`. |
| `SDR_TEST_TENANT` | fallback only | configurator |
| `SDR_CLIENT_ID` / `SDR_CLIENT_SECRET` | fallback only | configurator OAuth |
| `ATLAN_API_KEY` | fallback only | full-DAG AE-management (`/automation/api/v1/*`). Service account must carry `realm-admin` which the OAuth client does not. |
| `SDR_OAUTH_CLIENT_ID` / `SDR_OAUTH_CLIENT_SECRET` | no | Dapr S3 binding + pyatlan asset queries. Falls back to API-key when absent. |

`ATLAN_BASE_URL` is **not** a secret you set: it is always derived as
`https://<resolved tenant>` so it can never name a different tenant than the rest
of the leg's credentials do.

The four `SDR_*` / `ATLAN_API_KEY` entries were the only way to name a tenant
before the cross-CSP matrix. They are now the fallback used when
`E2E_TENANT_MATRIX_JSON` is not available to the repo, which is what keeps a
repo that has not been onboarded running exactly as it did before.

## Cross-CSP matrix

Every e2e suite runs once per cloud provider, against that cloud's tenant, so
CSP-specific behaviour — the objectstore binding `atlan-configurator` emits, the
tenant's blobstorage proxy, Temporal host resolution — is exercised before
release rather than after. This is FND-6.

**The secret.** One org secret, `E2E_TENANT_MATRIX_JSON`, keyed by cloud:

```json
{
  "aws":   {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…"},
  "azure": {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…"},
  "gcp":   {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…"}
}
```

One secret rather than four per cloud because a `strategy.matrix` value cannot
index the `secrets` context, and the reusable workflows declare their
`workflow_call` secrets explicitly — so per-cloud names would have to be
re-declared for every cloud ever added. Adding a fourth CSP is a secret edit and
a one-line change to `DEFAULT_CLOUDS`; no app repo changes at all.

Each entry may also carry `"deployment_name"` when that tenant's system apps
(publish / quality / lineage) are not registered under `production`. It reaches
the harness as `E2E_TENANT_DEPLOYMENT_NAME`, which
`BaseE2ETest.resolved_tenant_deployment_name()` prefers over the class default.

**Per-leg resolution.** `.github/scripts/resolve_e2e_tenant.py` extracts only the
leg's own cloud and writes `SDR_TEST_TENANT`, `SDR_CLIENT_ID`,
`SDR_CLIENT_SECRET`, `ATLAN_API_KEY` and the derived `ATLAN_BASE_URL` to
`$GITHUB_ENV`. A leg therefore never holds the other tenants' credentials. It
runs in two passes (`--mask-only`, then the env write) for the same reason
`export_extra_env.py` does: registering the blob as a secret does not redact the
values inside it.

**Leg naming and isolation.** The cloud rides in the matrix leg `name`
(`<suite>-<cloud>`), which is already the job name, the concurrency-group key and
the `artifact-suffix` — and `artifact-suffix` is what `derive_deployment_name.py`
folds into `ATLAN_DEPLOYMENT_NAME`. So queues, artifacts, concurrency and job
names all pick up the cloud with no new machinery.

**Selecting clouds.** `e2e-clouds` on `tests-reusable.yaml` (`clouds` on
`e2e-full-reusable.yaml`), surfaced as the `e2e_clouds` `workflow_dispatch` input
on each app's `tests.yaml`:

| Value | Meaning |
|---|---|
| `""` (default) | The SDK's current list — `DEFAULT_CLOUDS` in `discover_e2e_suites.py`. Deliberately not "no clouds": an untouched GitHub input arrives as `""`, and that must not silently opt a repo out. |
| `aws` (or any subset) | Just those clouds. Use this to re-run one cloud, or to keep the fleet moving while one tenant is down. |
| `none` | No cloud dimension — one leg against the single fallback tenant. |

Every cloud is a **required** leg: the matrix is `fail-fast: false` and the Tests
Gate reads `needs.e2e.result`, the matrix aggregate, so any cloud failing reds the
gate. Narrowing `e2e-clouds` is the escape hatch, and trimming the secret to one
key is the org-wide one.

When the secret is not available to a repo, `clouds` is forced to `none` and the
`Discover e2e suites` job emits a `::warning::` saying so — a run that asked for
three clouds and got one must not look identical to one that got three.

## Cross-repo dispatch

A single always-on job (`connector-tests`) on apps-sdk PRs fans out to the connector matrix:

| Check on apps-sdk PR | What it is | Gating |
|---|---|---|
| `Connector E2E dispatch (<repo>)` (matrix over all registered connectors) | SDK-side job that fires `tests.yaml` in each connector and exits — success means "dispatch succeeded", not "tests passed" | _none — auto on every code-changing PR_ |
| `Connector E2E run / <repo>` | Check run tracking the actual connector run. Created (pending) by the dispatch job under the **atlan-app-fleet App** token; the connector's own run **completes it via callback** (`tests-reusable.yaml` `report-to-sdk`, same App — a check run can only be updated by its creating App). No busy-wait poll. | rolled up by `Connector Tests Gate` |

> The two are deliberately split: the dispatch job can't stay pending for the whole (8+ min) connector run without polling, so a separate `run` check holds the "is it done?" state and is closed by a push callback. Both are owned by the fleet App so the cross-repo callback can complete them and they're attributed to the fleet App's check suite (not an arbitrary `github-actions` one). The `E2E Callback Watchdog` sweeps any `run` check left pending if a connector runner dies before its callback fires.

The `tests.yaml` job in each connector runs unit + integration tests unconditionally. The full-DAG `e2e` job inside `tests.yaml` runs only when the SDK PR carries the `e2e` label — controlled via the `run_e2e` workflow input (`"true"` / `"false"`) passed by the dispatcher.

Mechanism: `codex-/return-dispatch@v4` in `e2e-apps/action.yaml` fires `workflow_dispatch` on the target repo, passing `application_sdk_ref` (the SDK PR's head SHA, used by the connector to re-pin the SDK before running tests), `run_e2e` (derived from whether the SDK PR has the `e2e` label), and `distinct_id` (the dispatching SHA — see below).

Since v4 the action reads the dispatched run's id straight out of the `workflow_dispatch` API response, so the run is identified in seconds. v3 had to trawl the dispatched run's step *names* for a `distinct-id <sha>` marker, which is why the dispatcher used to pass `workflow_timeout_seconds` / `workflow_job_steps_retry_seconds` — both are gone. Receivers still carry the `distinct-id <sha>` echo step, but it is now a human breadcrumb only: nothing reads the step name.

> **`distinct_id` is still required from the caller.** v3 injected it into the dispatch payload automatically; v4 dropped the input and injects nothing, so every `e2e-apps` caller must include a `"distinct_id"` key in `workflow-inputs`. It is not just a breadcrumb: `atlan-local-marketplace-app`'s `sdr-k8s-e2e.yaml` keys its concurrency group on `sdr-k8s-e2e-${{ inputs.distinct_id || inputs.application_sdk_ref || github.ref }}` with `cancel-in-progress: true`. Cross-repo dispatches all land on `refs/heads/main`, so an omitted `distinct_id` coarsens that group and overlapping dispatches cancel each other — the dispatched run returns `cancelled` and poll mode reds the SDK job. This is what broke `SDR K8s E2E (LM)` when the v4 bump first landed (#2923, reverted in #2939) and is the reason both call sites in `pull_request.yaml` now pass it explicitly.
>
> **This paragraph is the single source of truth for the receiver's grouping expression.** It is the one place the expression is written down: `e2e-apps/action.yaml` and the `pull_request.yaml` call site state the invariant and point here instead of restating it. The receiver lives in another repo and moves independently, so every in-repo copy of the expression goes stale silently — which is exactly what happened when the middle `inputs.application_sdk_ref` term landed there after #2939. When updating this, check the receiver rather than trusting a copy.
>
> The middle term is the receiver's own second line of defence, added after #2939: because both SDK call sites always pass `application_sdk_ref`, a dispatcher that drops `distinct_id` today degrades to per-SDK-SHA grouping rather than one global group. That narrows the blast radius; it does not remove the caller's obligation, and it only helps receivers that have adopted the fallback. On the SDK side the obligation is enforced, not just documented — `e2e-apps`'s `Require a distinct_id in workflow-inputs` step ([`validate_dispatch_inputs.py`](../../.github/scripts/validate_dispatch_inputs.py)) fails the job before dispatching if the key is missing or empty.

### Sticky-comment behaviour

The SDR composite renders one PR-comment body, writes it to `results/pr-comment-body.md`, and uploads it as part of the test artifact. Both posting sides (connector PR + cross-repo SDK PR) read this same file and post it as a sticky-update comment, swapping the marker line so updates don't collide.

## Contract regeneration before tests

The e2e/integration tests consume `app/generated/manifest.json` (the Automation Engine DAG): the host-side harness reads the committed file, and the connector Docker image `COPY`s `app/generated/` at build time and serves `manifest.json` at runtime. Nothing used to regenerate that file from `contract/app.pkl`, so a Contract Toolkit change — at the app level (`contract/app.pkl`) or the SDK level (`contract-toolkit/src`) — ran against a possibly-stale committed manifest and was never actually exercised (BLDX-1493).

The shared [`regenerate-contract`](../../.github/actions/regenerate-contract/action.yaml) composite regenerates `app/generated/**` from `contract/app.pkl` **before** the manifest is consumed (driver: [`.github/scripts/regenerate_contract.py`](../../.github/scripts/regenerate_contract.py)). It self-skips when there is no `contract/app.pkl`.

| Where it runs | Placement | Drift gate |
|---|---|---|
| `connector-integration-tests` (always-on host harness) | after the SDK-ref repin, before the app server boots | **Warn-only** — annotates a stale committed `app/generated/`, never fails |
| `sdr-e2e` (image-based, incl. the full-DAG path via `e2e-full-reusable`) | **before the image build** (bakes the fresh manifest into the connector image) | Off (`check-drift: "false"` — uv/ruff aren't installed yet; the integration job owns the gate) |

- **App-level** (default): regenerate from the app's pinned `@app-contract-toolkit` version, so a `contract/app.pkl` change is exercised even when the committed manifest was not regenerated.
- **SDK-level** (cross-repo dispatch, `application-sdk-ref` set): the `@app-contract-toolkit` dependency is overridden to the SDK PR's `contract-toolkit/src`, so a toolkit change in the SDK PR is generated against the *real* connector contract end-to-end. Drift is expected, so the gate is skipped and a `pkl eval` failure is fatal.

Because the e2e suite is matrixed one leg per test file and each leg builds its own image inside `sdr-e2e`, regeneration runs once per leg (it cannot be hoisted ahead of the matrix — the fresh `app/generated/` must exist in the workspace when each leg builds).

## Workspace-wipe defences (local-action mode)

When the SDR composite is invoked via local path (`./.application-sdk/.github/actions/sdr-e2e`) during cross-repo dispatch, `setup-deps`' inner `actions/checkout` wipes the entire workspace — including `${{ github.action_path }}` itself. The composite:

1. Stashes its full asset tree to `/tmp/sdr-e2e/` before `setup-deps` runs.
2. After setup-deps, resolves a `steps.action_root.outputs.path` that falls back to the `/tmp` stash if `${{ github.action_path }}` is now empty.
3. Restores the stash back to `${{ github.action_path }}` at the end of the action body so GH Actions can find `action.yml` for post-hook execution.

Single-pipeline apps invoking the action remotely (`@main`) never hit this code path.

## Onboarding checklist for a new connector

1. **Action manifest**: `app.yaml` at repo root (3 lines).
2. **Unified workflow**: copy `.github/workflows/tests.yaml` from mysql-app; swap connector references. This single file covers unit + integration tests (always) and full-DAG e2e (on the `e2e` label or `run_e2e=true` dispatch input).
3. **Config dir**: create `.github/sdr-e2e/` (new) or `.github/e2e/` (legacy). Files: `docker-compose.ci.yml`, `e2e-full-docker-compose.yaml`, `e2e-full-components/`, `seed.sql`, `make-secrets.py`, `make-secrets-e2e-full.py`.
4. **Tests**: unit + integration tests under `tests/unit/` and `tests/integration/`; full-DAG e2e under `tests/e2e/` (`SQLAppE2EFullTest` subclass).
5. **Repo secrets**: set the 7 entries from the table above.
6. **SDK matrix**: add `<connector>-app` to the `DEFAULT_MATRIX` in apps-sdk's `matrix-builder` job (`pull_request.yaml`) so `connector-tests` fans out to your connector automatically.

## Reference

- [Reference adopter walkthrough (mysql-app)](https://github.com/atlanhq/atlan-mysql-app/blob/main/docs/CI-E2E.md)
- SDR composite action: [`.github/actions/sdr-e2e/action.yaml`](../../.github/actions/sdr-e2e/action.yaml)
- Full-DAG reusable workflow: [`.github/workflows/e2e-full-reusable.yaml`](../../.github/workflows/e2e-full-reusable.yaml)
- Cross-repo dispatcher action: [`.github/actions/e2e-apps/action.yaml`](../../.github/actions/e2e-apps/action.yaml)
- Test harness: [`application_sdk/testing/full_dag/`](../../application_sdk/testing/full_dag/)
- Series of merged PRs that built this:
  - [#1669](https://github.com/atlanhq/application-sdk/pull/1669) — SDR composite + pytest base
  - [#1710](https://github.com/atlanhq/application-sdk/pull/1710) — Cross-repo dispatch + full-DAG harness + sticky comments
  - [#1746](https://github.com/atlanhq/application-sdk/pull/1746) — `.github/sdr-e2e/` convention + `app.yaml` requirement
  - [#1752](https://github.com/atlanhq/application-sdk/pull/1752) — Path-override inputs for multi-pipeline apps
