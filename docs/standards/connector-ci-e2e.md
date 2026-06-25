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
    container-health-timeout-seconds:  # OPTIONAL. Default 60s. Bump for heavy native deps.
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
    secrets: inherit
```

Threaded secrets the reusable workflow expects on the caller side:

| Secret | Required | Used by |
|---|---|---|
| `SDR_TEST_TENANT` | yes | configurator |
| `SDR_CLIENT_ID` / `SDR_CLIENT_SECRET` | yes | configurator OAuth |
| `ATLAN_BASE_URL` | yes | full-DAG harness |
| `ATLAN_API_KEY` | yes | full-DAG AE-management (`/automation/api/v1/*`). Service account must carry `realm-admin` which the OAuth client does not. |
| `SDR_OAUTH_CLIENT_ID` / `SDR_OAUTH_CLIENT_SECRET` | no | Dapr S3 binding + pyatlan asset queries. Falls back to API-key when absent. |

## Cross-repo dispatch

A single always-on job (`connector-tests`) on apps-sdk PRs fans out to the connector matrix:

| Job on apps-sdk PR | Connector workflow dispatched | Gating |
|---|---|---|
| `Connector Tests (<repo>)` (matrix over all registered connectors) | `tests.yaml` | _none — auto on every code-changing PR_ |

The `tests.yaml` job in each connector runs unit + integration tests unconditionally. The full-DAG `e2e` job inside `tests.yaml` runs only when the SDK PR carries the `e2e` label — controlled via the `run_e2e` workflow input (`"true"` / `"false"`) passed by the dispatcher.

Mechanism: `codex-/return-dispatch@v3` in `e2e-apps/action.yaml` fires `workflow_dispatch` on the target repo, passing `application_sdk_ref` (the SDK PR's head SHA, used by the connector to re-pin the SDK before running tests) and `run_e2e` (derived from whether the SDK PR has the `e2e` label).

### Sticky-comment behaviour

The SDR composite renders one PR-comment body, writes it to `results/pr-comment-body.md`, and uploads it as part of the test artifact. Both posting sides (connector PR + cross-repo SDK PR) read this same file and post it as a sticky-update comment, swapping the marker line so updates don't collide.

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
