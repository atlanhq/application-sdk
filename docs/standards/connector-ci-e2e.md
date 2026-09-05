# Connector CI: SDR + Full-DAG E2E

> **Audience:** Connector teams onboarding (or maintaining) one of the two end-to-end test pipelines this SDK ships.
> **Canonical reference adopter:** [`atlanhq/atlan-mysql-app`](https://github.com/atlanhq/atlan-mysql-app) — see its [`docs/CI-E2E.md`](https://github.com/atlanhq/atlan-mysql-app/blob/main/docs/CI-E2E.md) for the full connector-side walkthrough.
> **All four canonical apps:** `docs/agents/canonical-apps.md` — hello-world, openapi, mysql, metabase. These are the only connector repos worth copying from; an arbitrary `atlan-*-app` may be mid-migration or carry patterns the SDK has since deprecated.

This doc covers what the SDK ships — the composite action, the reusable workflow, conventions, and inputs. Connector-side wiring lives in each connector repo; see the mysql-app walkthrough for a copy-pasteable example.

> **Do not invest in "SDR" or "full_dag" as test tiers.** Both names describe *where* a connector runs, or *which* pipeline once ran a suite — never a category of thing under test. `application_sdk.testing.full_dag` is frozen and removed in v4.0; `BaseSDRIntegrationTest` is deprecated and removed in v4.0. Neither needs a replacement harness, a compatibility layer, or new SDK surface. New work is placed per concern — see [The SDR base class](#the-sdr-base-class) for the table. If a proposal starts "to preserve SDR coverage we should…", the premise is wrong: read that section first.

## What the SDK ships

| Component | Location | Purpose |
|---|---|---|
| `sdr-e2e` composite action | `.github/actions/sdr-e2e/action.yaml` | Build PR image, configurator + Dapr + Temporal stack-up, pytest, PR sticky comment, teardown. Used by both pipelines. |
| `build-app-image` composite action | `.github/actions/build-app-image/action.yaml` | SDK-ref repin → manifest regeneration → buildx build/push → platform assert → interpreter assert. Extracted from `sdr-e2e` so the image can be built **once per run** ahead of the e2e matrix, and optionally multi-arch (see [Building the image once](#building-the-image-once)). |
| `e2e-full-reusable.yaml` reusable workflow | `.github/workflows/e2e-full-reusable.yaml` | Boilerplate (120-min timeout, concurrency group, env wiring, agent-name resolution) for the full-DAG pipeline. Connector repos `uses:` it as a 5-line wrapper. |
| `e2e-apps` cross-repo dispatcher | `.github/actions/e2e-apps/action.yaml` | Fires `workflow_dispatch` on the connector repo with the apps-sdk PR's head SHA. Polls for completion, surfaces a sticky status comment on the SDK PR. |
| ~~`BaseSDRIntegrationTest`~~ | `application_sdk/testing/sdr/` | pytest base for the SDR pipeline. Connector test class declares `Scenario(...)` instances. **Deprecated since 3.23.0, removed in v4.0** — it emits a `DeprecationWarning` on subclass and conformance B001 flags consumers fleet-wide. There is no single replacement because SDR is a deployment mode, not a test tier: see [The SDR base class](#the-sdr-base-class). |
| `BaseE2ETest` / `SQLAppE2ETest` | `application_sdk/testing/e2e/` | pytest base for the full-DAG pipeline. `BaseE2ETest` is connector-agnostic and is what the codegen'd `app/generated/_e2e_base.py` subclasses; SQL connectors use `SQLAppE2ETest` on top of it, subclassing with `include_filter`, `expected_min_asset_counts`, `database_spec()`, etc. |
| ~~`SQLAppE2EFullTest` / `BaseFullDAGE2ETest`~~ | `application_sdk/testing/full_dag/` | **Deprecated, removed in v4.0.** The predecessor of the row above; it emits a `DeprecationWarning` on import and its client / error types are already re-exports of the `testing/e2e` ones. Do not start a new suite here — see [Which harness](#which-harness). |

## Which harness

New suites use `application_sdk.testing.e2e`. Nothing new should be written against `application_sdk.testing.full_dag`.

| Your connector | Subclass |
|---|---|
| SQL | `application_sdk.testing.e2e.SQLAppE2ETest` |
| Anything else (BI, API, object-store, agent apps) | the generated `app/generated/_e2e_base.py`, which subclasses `application_sdk.testing.e2e.BaseE2ETest` |

`BaseE2ETest` is connector-agnostic and is already the base for every scaffolded app. The SQL-shaped parameter rows (`include-filter` / `exclude-filter`) come from `SQLAppE2ETest`, not from the base — so "my connector is not SQL, so I need the old harness" does not follow. If a non-SQL connector genuinely cannot express its manifest tokens through `BaseE2ETest`, that is an SDK gap worth filing, not a reason to start a `full_dag` suite.

`application_sdk.testing.full_dag` is deprecated and removed in v4.0. It emits a `DeprecationWarning` on import and on subclassing `BaseFullDAGE2ETest` / `SQLAppE2EFullTest`, and its `client` / `_errors` modules are already thin re-exports of the `testing/e2e` ones. Suites still on it (domo, looker, saperp at time of writing) are pinned to released SDKs where it still works; they need migrating before a v4 repin, not preserving as a second supported path.

The `full_dag` package is **frozen** (FND-245): it gets no backports from `application_sdk/testing/harness/` and no drift repair. Its duplicate mustache substitution and its unconditional sleep stay as they are and die with the package at v4.0. Effort that would have gone into collapsing it into re-export shims goes into migrating the three remaining suites instead.

### The SDR base class

`BaseSDRIntegrationTest` is deprecated on the same v4.0 clock. Removing it needs **no new SDK surface**, because there is no SDR-shaped hole to fill.

**SDR is a deployment mode, not a test tier.** `RunMode` in `application_sdk/testing/e2e/payload.py` is documented as "whether the connector runs in tenant or in caller-controlled CI" — `DIRECT` dispatches to the tenant's production-deployed pod, `AGENT` puts an `agent-name` on the *same* Temporal queue so a caller-deployed worker picks it up. That is placement. Auth, preflight, credential resolution and metadata extraction are handler functionality that behaves identically either way; none of them is unique to SDR, and none of them should be pinned to an "SDR" tier.

`testing/e2e` already models this correctly: one harness, one `mode` ClassVar, tiers 4 and 5 from the same class. `testing/sdr` is the vestige of an older framing in which a deployment mode got its own test base — which is why its three documented additions over `BaseIntegrationTest` turn out not to be about SDR at all:

1. **Workflow completion polling** — a generic convenience, mode-independent.
2. **"Agent credential routing"** — injects `extraction_method="agent"` plus an `agent_json`. That *is* the mode flag; it is not a distinct capability.
3. **Multi-entrypoint `workflow_type` injection** — mode-independent.

The deprecation notice on `sdr/base.py` ("use `BaseE2ETest` with `RunMode.AGENT`") repeats the original error: it answers "what replaces the SDR *tier*" by naming a *mode*. Ignore that framing. The honest guidance is per-concern, and none of it mentions SDR:

| Concern | Where it belongs | Canonical example |
|---|---|---|
| Handler functionality — auth, preflight | call the handler directly | mysql `tests/integration/test_mysql_handler.py` against a real MySQL, including wrong-password and unreachable-host; metabase `tests/unit/test_handler.py` against a mocked client |
| Credential resolution | this repo, once | `tests/unit/credentials/` (337 tests, `resolve_agent_json` / `secret-manager` / `secret_path` included); per-app, mysql `tests/integration/test_credential_resolution.py` against fake secret stores |
| A full DAG, in **either** mode | `tests/e2e/` | the generated `_e2e_base.py`; pick tier 4 or 5 with the `mode` ClassVar, not with a different base class |

The canonical apps (`atlan-openapi-app`, `atlan-mysql-app`, `atlan-metabase-app`) each have exactly `unit/`, `integration/`, `e2e/`. None has a `tests/sdr/`; none uses `Scenario` or `BaseIntegrationTest`.

Two corollaries worth stating, because the intuition tends to run the other way:

- **Credentials are already first-class in `tests/e2e/`.** `application_sdk/testing/e2e/credential.py` generates a typed per-connector `CredentialBody` from `contract/app.pkl` — a stronger contract than a hand-written `agent_spec_template` dict, and the reason connectors needing real credentials (mysql, metabase) run e2e at all.
- **The compose stack is not SDR-specific.** `e2e-full-reusable.yaml` uses the same `.github/actions/sdr-e2e` composite action, so both pipelines bring up the same atlan-configurator + Dapr + Temporal stack. Running "in the SDR stack" distinguishes nothing.

So: write no new SDR suites, and do not look for an SDR replacement. Place each concern per the table above.

## The two pipelines

The two pipeline **names** below are the report titles the composite action emits, derived from the test path. They are labels on CI legs, not test tiers — do not read a category of coverage off them.

| Pipeline (report title) | How much of the DAG runs | Source system | Wall time | Triggers |
|---|---|---|---|---|
| **SDR Integration Tests (testcontainer)** | The connector's own handlers and workflow only — no system apps. | Local testcontainer. | ~3 min | Auto on every connector PR push |
| **E2E Full Tests (system apps)** | Full DAG: connector extract → publish → query-intelligence → lineage-app → lineage-publish, with asset-count and lineage assertions in Atlas. | Whatever the suite points at. | ~20–40 min | Label-gated (`e2e`) — see below |

The distinguishing axis is **how much of the DAG runs**, and therefore wall time and trigger policy. It is not SDR-vs-not, and it is not hermetic-vs-live: both legs call the same composite action, and both reach the CI tenant's Temporal (`<tenant>-temporal.atlan.com`, requiring `ATLAN_BASE_URL` + the OAuth pair). The difference is test target, Dapr components, compose overlay, and secret-bundle shape.

Neither leg is the place credential resolution is proven. That is `tests/unit/credentials/` in this repo — see the table in [The SDR base class](#the-sdr-base-class).

### What the `e2e` label actually gates

Adding the `e2e` label starts the suite; a subsequent push (`synchronize`) on a
PR still carrying it re-runs the suite. What does **not** re-run it is an
unrelated label add — `size/`, `area/`, dependency and review-state labels churn
constantly on an open PR, and every one of those used to re-fire the whole
matrix (FND-48).

The `Discover e2e suites` gate in `tests-reusable.yaml` therefore asks two
questions, not one:

```yaml
contains(github.event.pull_request.labels.*.name, 'e2e') &&
(github.event.action != 'labeled' || github.event.label.name == 'e2e')
```

You will still see a *workflow run* appear for every label add — GitHub has no
trigger-level label filter, so the run is created and then skips within seconds.
That is expected; it costs no tenant time and nothing queues behind it. Do not
"fix" it by removing `labeled` from your `tests.yaml` trigger list: that is what
makes adding the label start a run in the first place. See
[`docs/standards/ci.md`](ci.md#label-gates-must-be-event-aware).

Note that a genuine re-trigger still **queues** behind an in-flight run rather
than cancelling it (`cancel-in-progress: false`). That is deliberate —
cancelling mid-run abandons a live Automation Engine run and leaves tenant state
behind — and it is a separate decision from the gating above.

### One dispatch per commit, however many events GitHub sends

GitHub can emit several `pull_request` events for one head SHA. Observed on
PR #3306: `opened`, then `labeled e2e` **twice**, one second apart — three
events, one commit, three full `PR Checks` runs, and three independent
dispatches into the same connector. Those three connector runs then fought over
the same three cloud tenants: one ran the suite in ten minutes, the other two
split the freed leases between them and blocked on each other for the whole
90-minute wait budget (FND-646).

No workflow `if:` can tell two identical `labeled` events apart, so `e2e-apps`
(`wait-mode: callback`) claims a dispatch slot before it dispatches:

* One ref per commit and app — `refs/e2e-dispatch/<app>/<sha>`, created by an
  atomic CAS. `POST /git/refs` returns 422 when the ref exists, and exactly one
  of N simultaneous callers sees 201. Same primitive as the `(app, cloud)`
  tenant lease.
* A run that loses creates **no check run** and dispatches nothing. It stays
  green: the verdict for that commit is the single `Connector E2E run / <app>`
  check the winner created, and every duplicate run's own Connector Tests Gate
  polls that same check and reports the same answer.
* **Re-running the dispatch job still re-dispatches.** The claim is owned by a
  *run*, not a run attempt, so a re-run of the run that claimed it proceeds.
* A claim whose run died before dispatching is reclaimed by the next contender,
  so a crashed dispatcher cannot leave a commit with no e2e.
* Every failure of the guard itself — no permission, a rate limit, an
  unreadable answer — **dispatches anyway** with a warning. A commit whose e2e
  silently never ran is far worse than a duplicate, which is only the
  pre-existing behaviour.

Duplicate `PR Checks` runs still appear, and each still pays for its base-image
build. That is accepted: it is cheap and it never reaches a tenant.

Explicitly rejected: `concurrency: cancel-in-progress` on the dispatching
workflow. Once the dispatch has fired it achieves nothing — the dispatch is
fire-and-forget, so cancelling the SDK-side run orphans the connector run it
already started rather than stopping it, and a cancelled connector run is not
even safe, because `prepare-tenant` carries `if: always()` and finishes
installing onto the tenants it had already leased on its way out.

That is not an argument that cancellation *never* helps, and the section below
is the counter-example: for the ~8 minutes a run spends building the base image
before it dispatches, cancelling it would have stopped the fan-out outright. It
is still not the lever to reach for there — a run cancelled between creating its
check run and dispatching leaves a check nothing will ever complete, and a
queueing group holds exactly ONE pending run, so a third arrival is evicted with
no log at all (FND-218). The head check below buys the same tenant time without
either hazard.

### No dispatch for a commit the PR has moved past

The claim above keys on the SHA, which makes it blind by construction to the
*sequential* duplicate: commit A's `PR Checks` run is still working its way
towards the dispatch when commit B lands. Two SHAs, two uncontested claims, two
full fan-outs — nothing duplicated in the CAS's terms, everything duplicated in
the tenants'. The lease then behaves exactly as advertised and **queues**, so the
head commit waits out the obsolete commit's entire install-plus-legs cycle. On
PR #3322 that was 3m38s, 10m12s and 12m of pure lease wait across three
connectors, all of it behind a commit already superseded by a bot push 59
seconds later (FND-696).

So the guard also asks whether `check-sha` is still the head of the PR it came
from, and skips if it is not:

* **One API call, on the `pull_request` path only.** A merge-queue entry's SHA is
  not any PR's head and cannot fall behind, so `--pr-number` is empty there and
  the check does not run.
* **Skip, not cancel.** At that point the stale run has not dispatched yet, so
  there is nothing to cancel — and cancelling a connector run that *has* started
  is unsafe for the `prepare-tenant` reason above.
* **Unreadable means "not superseded".** A stale run costs tenant time; a
  wrongly-skipped head commit costs the PR its e2e outright.
* The stale run's own `Connector Tests Gate` would otherwise wait 130 minutes for
  a check nobody is going to create, so `poll_check_runs_gate.py` takes the same
  `--pr-number` and stops as soon as it can see that its SHA is no longer the
  head. It exits **0**: no verdict is required from a commit that is no longer
  under review, and a red there is a false alarm on an abandoned run that
  automation reads as a real failure. Only the head commit's gate can satisfy a
  required check.

This closes the window up to the dispatch, and nothing after it. A push landing
later leaves a connector run already in flight, and that run splits into two
cases that want opposite answers:

* It has **acquired a lease**. Nothing to be done: cancelling is unsafe for the
  `prepare-tenant` reason above, so the queue is the right answer and the head
  commit waits.
* It has **dispatched but not leased yet** — it is still building, unit-testing
  and integration-testing, which was 2m30s on the openapi leg of PR #3322
  (dispatched 21:07:38, leased 21:10:10). It holds nothing, so it can stand down
  for free, and the head commit takes the tenant instead.

The second case is the connector-side recheck below.

### Standing down before the lease

`sdk-head-recheck` runs in `tests-reusable.yaml` immediately before
`lease-tenant` and asks the same question the dispatch guard asked, at the last
moment the answer can still save a tenant. When it says the SHA has been
superseded, `lease-tenant` skips — and `prepare-tenant`, the `e2e` legs and
`release-tenant` skip with it (FND-701).

**Finding the pull request.** The connector run is handed `application_sdk_ref`
and nothing else, and a SHA alone is not enough:
`GET /commits/{sha}/pulls` answers with an **empty list** for a commit a
force-push has moved past, which is precisely the case worth detecting. Verified
against the incident itself — `be82fade` is associated with no pull request,
while `d47789e0`, the head that replaced it, resolves normally.

So the PR number comes from the record that authorised the dispatch. The guard
already writes `refs/e2e-dispatch/<app>/<sha>` pointing at a blob describing the
claim; that blob now carries `pr_number`, and the recheck reads it back. It is a
positive identification rather than an inference, and the claim is guaranteed to
outlive the run that needs it: the guard's prune only deletes a claim once that
SHA's `Connector E2E run / <app>` check has settled, which cannot happen while
this run is the thing that has yet to complete it.

A run with **no** claim ref is therefore not an SDK pull-request dispatch —
someone pinning `application_sdk_ref` by hand to test a connector against a
particular SDK commit — and is left alone. Without that, a deliberate manual run
would be skipped as a silent no-op.

**Where the gates read from, and why it matters.** Both `lease-tenant` and the
`e2e` legs gate on the job's `outputs.superseded`, never on its `result`:

* A `needs.<job>.result` check would make an infrastructure failure of the
  recheck **skip** the lease, and a skipped lease skips the install and greens
  the run vacuously. Reading the output means an absent answer — job failed, job
  skipped, output never written — leases exactly as before. The script exits 0
  on every path for the same reason.
* `lease-tenant` needs `always()` for that gate to be consulted at all: without
  a status-check function GitHub applies an implicit `success()` over every
  need and skips the job before the `if:` is read. That is also why
  `discover-e2e` and `merge-e2e-image` are now named explicitly there.
* The `e2e` legs need their **own** clause. A *skipped* `lease-tenant` is the
  benign value in their existing gate — it is the `install-app-to-tenant: false`
  path — so gating the lease alone would leave the legs running against a tenant
  nobody installed onto with `expected-app-version` empty: a silently passing
  wrong-version run, the exact FND-31 failure the lease exists to prevent.

**The Tests Gate has to be told.** A stand-down produces the exact tuple the gate
driver's "matrix skipped despite discovered suites" anomaly exists to catch — a
successful discovery, a skipped matrix, and no install-path failure to explain
it. Left untold, `tests-passed` reds the required check *and* `report-to-sdk`
mirrors `conclusion=failure` onto the dispatching SDK commit: "your change broke
the connector" for a run that deliberately stood down, which is exactly the
misattribution the cancelled/failure split exists to prevent (FND-218).

So `verify-test-gate` takes a `superseded` input, and both call sites pass it —
they are one decision evaluated twice, and a gate told while the callback is not
would put them back in disagreement. Only the literal `"true"` explains the skip:
an absent, empty or unparseable value means the recheck job never answered, and
an unanswered skip is still unexplained. The input is optional and defaults to
`"false"`, so a connector pinned at `@main` that has not wired the job keeps the
previous behaviour, and a future re-wiring of the e2e `if` still cannot green the
gate by skipping the matrix. The e2e row then reads
`⊘ Stood down — superseded SDK commit` rather than pointing a reader at a
workflow misconfiguration that is not there.

A stood-down run therefore reports green to the dispatching SDK commit — the same
vacuous green the SDK-side gate gives that commit, for the same reason: it is no
longer the commit under review, and only the head commit's run can satisfy a
required check.

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
                        # tests/e2e/ has no special-case title and derives
                        # "E2E Tests"; set this if you want the fuller label.
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
    prebuilt-image:     # OPTIONAL. Full image reference to use INSTEAD of
                        # building one. Skips the build + interpreter assert and
                        # uses this everywhere the built image would have been.
                        # See "Building the image once" below.
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
      test-path:                # OPTIONAL. Default tests/full_dag/ — the deprecated
                                # layout, kept only so existing callers don't break.
                                # New suites live in tests/e2e/, so set this
                                # explicitly rather than taking the default.
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
  "aws":   {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…", "tenant_id": "…"},
  "azure": {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…", "tenant_id": "…"},
  "gcp":   {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…", "tenant_id": "…"}
}
```

One secret rather than four per cloud because a `strategy.matrix` value cannot
index the `secrets` context, and the reusable workflows declare their
`workflow_call` secrets explicitly — so per-cloud names would have to be
re-declared for every cloud ever added. Adding a fourth CSP is a secret edit and
a one-line change to `DEFAULT_CLOUDS`; no app repo changes at all. Both halves
are needed to *add* one — the narrowing below is an intersection, never a union,
so a key appearing in the secret does not widen the fleet's fan-out behind
`DEFAULT_CLOUDS`'s back. Removing a cloud needs only the secret edit.

`"tenant_id"` is the tenant's **vcluster instance name** (`markeznp37`, `home-mt`)
— *not* its hostname, which is what `"tenant"` holds. It is required only by the
tenant-install path (`install-app-to-tenant`, FND-31): GM matches a release's
`allowed_tenants` against this id exactly, so scoping with a hostname publishes
successfully and produces a release visible to **no** tenant, whose symptom
appears one call later as `version not found` on install. It reaches the drivers
as `E2E_TENANT_ID`.

There is no way to derive it client-side — Heracles reads it from the
`atlan-defaults` ConfigMap key `instance`, and deliberately not from the JWT,
whose Keycloak realm is `default` for every tenant. So an entry without it cannot
use the install path, and neither can the single-tenant fallback (which has no
entry to add the field to); the `E2E Tenant Install` workflow's `tenant_id` input
covers one-off runs in both cases.

### When the FAILED verdict is about somebody else's pod

LM's deployment health check is **namespace-scoped**: it reports `Pods failed in
namespace <ns>: <pod>` for any unhealthy pod in the app's namespace, not just the
ones belonging to the deployment it is reconciling. So a pod orphaned by an earlier
install — stuck in `ImagePullBackOff` on a tag that no longer resolves, say — fails
*every* later install to that tenant, however healthy the new version is.

That is not hypothetical: it is how the first successful multi-arch install
presented. Our pods pulled the image in 12.4s and were scaled to zero by KEDA as
designed, while a pod from an earlier attempt sat on a different tag
(`x1048 over 3h59m`) and took the verdict down with it.

`e2e_tenant_app.py install` therefore reads the pod events before accepting the
verdict:

- If **our** image is among the ones failing to pull, the failure stands. Kubelet
  can name an image by digest (`…@sha256:…`, with or without the tag) while
  `--image` arrives as a tag, so "ours" is decided by repository identity, not
  string equality: a failing reference in our repository counts as ours unless it
  carries a resolvably *different* tag, and only a different repository or a
  different tag of ours is foreign.
- If every failing image is provably some *other* version, the verdict is not
  evidence about this install — so it falls through to the installed-version
  read-back, which decides. A `::warning::` names the foreign images either way,
  and a `::notice::` says they still need deleting, because they will fail this
  check on every future install until someone does.
- If the read-back **disagrees**, it still fails. The override moves the decision
  to direct evidence; it never skips it.

A timeout is never downgraded this way — an accepted-but-unreconciled deploy is
nobody else's fault, and it is the silent wrong-version failure this whole
mechanism exists to remove.

### When the FAILED verdict is about pod churn the platform caused

The verdict is also an **instant**, not just a namespace: any pod unhealthy at
the moment LM looks reds the install, including a pod the platform itself just
deleted on purpose. The first install onto a new tenant failed in 45s that way —
KEDA scaled the server deployment to zero while the platform-injected
`atlan-env-seeder` init container was still blocked on a Vault secret whose name
encodes the namespace and had not synced yet, so the pod was SIGKILLed mid-init
and left phase `Failed`. The workers recovered, the app was fine, and the two
warm namespaces on the other clouds passed the same run.

The pull-failure reading above cannot catch that — there was no pull failure
(`Normal Pulled`) — so a second, narrower tolerance sits behind it. It needs
**both** halves, and the failing container has to be named:

- **Whose container broke.** The container under test is the one running the
  image we published; every other container in the pod came from the platform's
  pod template, so it can be neither broken nor fixed by the app. Decided by the
  same repository-identity compare as above (an unreadable `Image:` counts as
  ours), so there is no list of platform container names to keep current.
- **Why, for our own container.** `CreateContainerConfigError` (the kubelet could
  not *generate* the container's config — a missing Secret or ConfigMap — so the
  image was never instantiated), a node eviction or
  shutdown, or exit 137 where the events *name this pod's* deletion
  (`KEDAScaleTargetDeactivated` **and** `Deleted pod: <name>`). `OOMKilled` exits
  137 too and stays fatal. Anything unrecognised stays fatal — including plain
  `CreateContainerError`, which the kubelet sets *after* config generation when
  the CRI `CreateContainer` call fails (a bad security context, a volume the
  chart asked for, an image that cannot be instantiated). Those are app faults
  and are what this tolerance exists to keep fatal, so the reason list is not to
  be widened without a fixture from a real `describe`.
- **Then it must not happen again.** `--settle-seconds` (90 by default) later the
  live events are re-read, and any unhealthy event still inside that window keeps
  the verdict. A platform container that never recovers keeps warning, so it
  still fails us. The re-read is of the **live events** and nothing else: LM's
  `deployment_status` is a terminal record, and its failure snapshot is
  *failure-triggered*, so it can only ever depict a failure — never a recovery.
  The snapshot's `pod_events` must never reach the age comparison either: its
  ages are relative to its own capture moment (8s behind the live read on the run
  above, unbounded in general), so the same lines would read as young forever.
  `--settle-seconds 0` disables the tolerance entirely.

None of this proves the app is healthy, and nothing downstream should read it
that way — a one-shot warning on a still-wedged pod ages out of the window and
takes the window quiet with it. What the two conditions establish is that LM's
verdict is not evidence **either way**, which hands the decision to the
installed-version read-back: unconditional, direct, and the only positive
evidence in the flow. Its failure message says when a verdict was downgraded, so
a version mismatch is never read in isolation.

Two platform-side fixes would remove the churn rather than tolerate it — KEDA
`initialCooldownPeriod` on the server ScaledObject, and LM not counting a pod it
deleted itself — but both live outside this repo.

`pod_describe` is printed **per pod, unhealthy first**, for the same reason: under
one shared budget it printed as "tail 200 of 542 lines" and the only pod that
failed the install was in the dropped head, leaving two healthy workers as the
entire visible evidence.

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
| `""` (default) | The SDK's current list — `DEFAULT_CLOUDS` in `discover_e2e_suites.py` — **intersected with the clouds `E2E_TENANT_MATRIX_JSON` actually carries**. Deliberately not "no clouds": an untouched GitHub input arrives as `""`, and that must not silently opt a repo out. |
| `aws` (or any subset) | Just those clouds, for re-running one cloud on one repo. Exact, and never narrowed: a named cloud the secret does not carry fails its leg. |
| `none` | No cloud dimension — one leg against the single fallback tenant. |

Every cloud is a **required** leg: the matrix is `fail-fast: false` and the Tests
Gate reads `needs.e2e.result`, the matrix aggregate, so any cloud failing reds the
gate.

### Taking a cloud out of the rotation

**Remove its entry from `E2E_TENANT_MATRIX_JSON`.** That is the whole hatch: one
secret edit, fleet-wide, effective on the next run, no connector PR and no SDK
PR. The `Discover e2e suites` job reads the secret's *keys* (never its values —
`e2e_tenant_matrix_clouds.py` emits a key list and nothing else), hands them to
discovery, and the defaulted fan-out narrows to the intersection with a
`::warning::` naming every cloud it dropped. A run that got two clouds when the
SDK ships three says so in its own log.

Defaulted narrows; **named does not**. `e2e-clouds: aws,azure` naming a cloud the
secret does not carry still reaches `resolve_e2e_tenant.py` and still exits
non-zero — somebody asserted that cloud should run, and skipping it silently
would be a coverage hole rather than a narrowing. The asymmetry is deliberate and
is pinned by `test_a_defaulted_absent_cloud_is_dropped_with_a_warning` /
`test_a_named_absent_cloud_still_reaches_the_resolver`; it is the kind of
distinction a later reader flattens on the grounds that both paths "just check
the cloud list" (FND-354).

Narrowing to *nothing* is an error, not an empty matrix: a secret carrying none
of `DEFAULT_CLOUDS` fails discovery rather than emitting zero legs, which would
green the gate having run no e2e at all.

When the secret is not available to a repo, `clouds` is forced to `none` and the
`Discover e2e suites` job emits a `::warning::` saying so — a run that asked for
three clouds and got one must not look identical to one that got three. The same
applies when the payload cannot be parsed: the key read degrades to "not known",
narrowing is skipped, and the per-leg resolver still reports the real defect.

### Reporting coverage to the test-readiness scorecard

The `::warning::` above is per-run, ephemeral and buried in one app repo's
Actions log, and nothing fails either way — so at any point of *central*
visibility a repo running degraded looks identical to a fully covered one. The
`scorecard` job closes that (FND-33, FND-34): it feeds the e2e tier's evidence
and records cross-CSP coverage into `results/test-readiness.json`, which
`update-dashboard.yaml` publishes and connector-pulse ingests as the
`test_readiness` metric.

**Two facts, kept apart.** `raw.crossCloud.configured` is what this repo is
*wired* for — the requested fan-out narrowed exactly as discovery would narrow
it, resolved from the tenant matrix's key list with no e2e run required.
`raw.crossCloud.observed` is what a run actually *exercised*, from the
`clouds` output of the same discovery call that built the matrix. Collapsing
them would make "not rolled out" indistinguishable from "rolled out and
broken", which is a state apps really are in.

**Absent is not zero.** Three states have to stay distinguishable, and the wire
format carries all three because `exclude_none=True` drops an unset field:

| Wire | Meaning |
|---|---|
| `crossCloud` absent, or `observed` absent | e2e did not run — nothing is known |
| `observed: []` | e2e ran with no cloud dimension: the degraded single-tenant fallback |
| `observed: ["aws","azure"]` | e2e ran on those clouds |

The same rule governs the tier itself: when e2e did not run the `e2e` tier stays
`applicable: false` and the `e2e-present` gate stays `na` — excluded from the
aggregate, no grade cap. Scoring absent evidence as zero would drag every app's
grade on every routine push.

**Neither field is scored.** No `Check` reads them and no `Gate` caps on them.
Promoting cross-CSP to a scored dimension before the fleet is onboarded would
move every app's aggregate down at once, so a rollout would read as a fleet-wide
regression. Record first; score once a low value is actionable.

**`observed` is sparse, and that is structural.** The `scorecard` job runs on
push/merge_group; e2e runs on `workflow_dispatch + run_e2e=true` or an
`e2e`-labelled PR. Only a dispatched run on the default branch carries both, so
`observed` appears on those runs and not the rest. It cannot be fixed by also
running the scorecard on the PR path: `update-dashboard.yaml` only ingests
default-branch runs, and on a PR the integration job is skipped, so such a
scorecard would publish a zeroed integration tier — a fabricated regression.
This is precisely why `configured`, which needs no e2e run, is the field that
carries rollout visibility.

**Per-leg junits are merged worst-case per test.** Each leg uploads a junit at
the same inner path, so the scorecard downloads them unmerged (`pattern:`
without `merge-multiple`) and folds them on `(classname, name)`, taking the
worst outcome across legs. Summing would make the denominator a function of how
many clouds a repo has onboarded — a failure on one of three clouds would score
better than the same failure on the only cloud, so onboarding a cloud would
*raise* the score by diluting an existing failure.

### Requiring a tenant ID on the install path

That degradation is honest for a repo that only *runs legs against* a tenant, and
insufficient for one that *installs onto* one. The single-tenant fallback supplies
`SDR_TEST_TENANT` plus credentials and an API key, and no `tenant_id` — there is no
matrix entry to carry one. So on the install path the missing secret is fatal, not
a warning.

Failing rather than skipping the install, deliberately. Skipping would leave
`prepare-tenant` green having done nothing, the tenant on whatever version it was
already running, and every leg reding on its own version check instead: one
confusing failure per leg in place of one clear failure. Heracles re-fetches the
manifest from the tenant-deployed pod at AE submit, so running the legs against an
install that did not happen tests the version already on the tenant while
reporting on the PR's — the exact bug `install-app-to-tenant` exists to remove.
A caller on the install path without a resolvable `tenant_id` is misconfigured
rather than on a supported path, and since FND-128 made the install path the
default, the supported way to decline it is `install-app-to-tenant: false` — not a
tenant the install cannot be scoped to.

**The same precondition, checked twice.** "A `tenant_id` can be resolved" is
knowable in two halves at two different times, so it is checked at both (FND-203):

| Where | Sees | Catches | Costs |
| --- | --- | --- | --- |
| `discover-e2e` → *Require the tenant matrix on the install path* | whether `E2E_TENANT_MATRIX_JSON` exists at all | the install path — the default since FND-128 — on a repo the secret was never shared with | seconds |
| `prepare-tenant` → *Require a tenant ID before publishing anything* | the resolved `E2E_TENANT_ID` for **this** cloud | matrix present, this cloud's entry missing `tenant_id` | after two per-arch image builds and the manifest merge |

The early check exists because the late one is expensive to reach: the install
path builds `linux/amd64` and `linux/arm64` on separate native runners and merges
the manifest before `prepare-tenant` runs, so a repo that could never install
burned ~4 minutes of GitHub-hosted runner time per attempt to discover that.

Neither check subsumes the other. The secret's *presence* says nothing about
whether the entry inside it carries a `tenant_id`, so the early check cannot cover
the late one's case; and the late check is the one that runs too late to be cheap.
Both are pinned by `test_prepare_tenant_wiring.py`
(`test_both_install_preconditions_are_checked`), because side by side each looks
redundant — which is how one of them gets deleted.

## Cross-repo dispatch

A single always-on job (`connector-tests`) on apps-sdk PRs fans out to the connector matrix:

| Check on apps-sdk PR | What it is | Gating |
|---|---|---|
| `Connector E2E dispatch (<repo>)` (matrix over all registered connectors) | SDK-side job that fires `tests.yaml` in each connector and exits — success means "dispatch succeeded", not "tests passed" | _none — auto on every code-changing PR_ |
| `Connector E2E run / <repo>` | Check run tracking the actual connector run. Created (pending) by the dispatch job under the **atlan-app-fleet App** token; the connector's own run **completes it via callback** (`tests-reusable.yaml` `report-to-sdk`, same App — a check run can only be updated by its creating App). No busy-wait poll. | rolled up by `Connector Tests Gate` |

> The two are deliberately split: the dispatch job can't stay pending for the whole (8+ min) connector run without polling, so a separate `run` check holds the "is it done?" state and is closed by a push callback. Both are owned by the fleet App so the cross-repo callback can complete them and they're attributed to the fleet App's check suite (not an arbitrary `github-actions` one). The `E2E Callback Watchdog` sweeps any `run` check left pending if a connector runner dies before its callback fires.

**The `run` check reports the connector's Tests Gate verdict, and nothing else.**
`report-to-sdk` runs the same [`verify-test-gate`](../../.github/actions/verify-test-gate/action.yaml)
action as the connector's own `Tests Gate` job, over the same job results, and
completes the check run with its `conclusion` output. This is a contract, not an
implementation detail: a red connector run must be red on the SDK PR that
triggered it. It was not always so — the callback used to derive its own verdict
in `build_callback_summary.py` from a subset of the gate's inputs (no
`discover-e2e`, none of the install-path jobs, no "discovery found suites but the
matrix skipped" anomaly rule), so a connector run whose `Tests Gate` failed
because its arm64 e2e image build failed still completed the SDK-side check as
**success**. That second verdict is gone — `build_callback_summary.py` builds the
check-run body and emits no `conclusion` at all. Every half is pinned by
`test_build_callback_summary.py`: the script must expose no verdict, the two jobs
must feed one action with identical inputs, and each must `need` every job it
judges. If you add a job to the gate, add it to both.

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

## Building the image once

The test image reference is deterministic — `ghcr.io/atlanhq/<app-image-name>:sdr-test-<short-sha>`
— so every cloud leg of a run used to rebuild the identical tag. The build
sequence (SDK-ref repin → manifest regeneration → buildx → push → interpreter
assert) therefore lives in its own [`build-app-image`](../../.github/actions/build-app-image/action.yaml)
composite, and `sdr-e2e` takes a `prebuilt-image` input that skips its own build
and uses the supplied reference everywhere the built one would have gone (the
configurator's `app_image`, and the action's `image` output).

This exists for FND-31, not just to save build minutes: the full-DAG pipeline has
to install the app under test **onto the target tenant before any leg starts**, so
the image must exist before the matrix fans out. Saving N−1 duplicate builds is the
side benefit.

> **Why it matters that the tenant runs the app under test.** At AE submit,
> Heracles re-fetches the manifest from the **tenant-deployed pod** and *that* DAG
> is what executes (`processAutomationEngineWorkflow`); the harness's local
> `manifest_path` seed DAG establishes the workflow record, not the graph. So the
> DAG contract a full-DAG e2e exercises is whatever version is installed on that
> tenant. With `install-app-to-tenant: false`, that is whatever was last
> hand-deployed there.

### Asserting the executed DAG, not just the installed version (FND-129)

The install path verifies the **version** on the tenant (`expected-app-version`,
self-skipping when empty). That is a proxy. What actually executes is the DAG
Heracles fetches from the tenant-deployed pod at submit — `CreateVersion(slug,
dag)` + `PublishVersion` on the same slug the harness seeded, superseding the
seed version. So the version check says *the right image is installed*; the
harness also asserts *the graph that ran is the graph we built*.

Right after submit and before the poll loop, `BaseE2ETest` reads back the
published version:

```
GET /automation/api/v1/workflows/{slug}/versions?is_published=true&page=0&page_size=1
```

and compares its DAG's **node identity** — node set, plus each node's `app_name`
and `inputs.workflow_type` — against the identities of the manifest-derived seed
DAG. A divergence raises `DeployedManifestMismatchError` with the node-level
diff. Post-submit is the one thing lost versus a preflight: the originally
planned `?submit=false` does not exist (`processCreateWorkflow` routes native
execution to `processAutomationEngineWorkflow` without forwarding query params,
and that function ends in an unconditional submit), and there is no
`GET /package-workflows/{name}`. It still fails within seconds of submit and
long before any assertion, with a precise diff instead of a confusing
downstream failure.

**Identity, not the DAG blob.** Template variables are substituted at submit
(`substituteTemplateVars`), so a byte comparison would fail on every run. Node
name, owning app and workflow type survive substitution; the `{app_name}`
placeholder is resolved on both sides before comparing.

**It only ever reds a leg on a positive finding.** Three outcomes are
*unanswerable*, and each logs and continues rather than failing:

| Outcome | Why nothing is asserted |
|---|---|
| The read did not get through (transport error, non-2xx, unparseable envelope) | No answer is not a mismatch |
| AE still serves the harness's own seed version after `deployed_manifest_timeout_seconds` (60s) | Comparing the seed DAG to itself would pass whatever the tenant runs |
| The suite has no `manifest_path` (a hand-crafted legacy seed DAG) | The seed is an approximation of the app's graph, not a copy of it |

So an unonboarded caller is untouched without opting out — the same self-skip
posture as the CI version check. `assert_deployed_manifest = False` on the test
class turns it off outright, but the default position is that a divergence is
the bug this check exists to find.

### Adoption: on by default (FND-128)

`install-app-to-tenant` defaults to **true**. No app repo has to opt in, and none
should need a PR to get the behaviour.

It shipped opt-in under FND-31 for one reason: the install cannot resolve a tenant
without `E2E_TENANT_MATRIX_JSON`, and that secret was shared with a handful of
repos. Once it went org-wide, the opt-in stopped protecting anything and started
costing something — an un-adopted repo still fanned out across every cloud in the
matrix (the fan-out is gated on the secret, not on this input) and each leg tested
whatever version that cloud's tenant already served. Three legs of wrong-version
green in place of one.

**Opting out.** Set `install-app-to-tenant: false` in the app's `tests.yaml` when
the app genuinely cannot be installed onto the e2e tenants — not published to GM,
or a tenant carrying an orphan that fails every install (FND-131). Every job on
the install path is gated on the input, so opting out restores the previous
behaviour exactly: per-leg builds, no lease, no install, legs against whatever the
tenant runs. It reinstates the wrong-version risk along with it, so fixing the
tenant is the better move where there is a choice.

**What a first run tells you.** The install path is a hygiene report for that app's
footprint on each tenant. `prepare-tenant` names offending images in its log, so a
dirty tenant produces a precise cleanup list rather than a blanket "install
failed". Expect the FND-131 shapes: an unpullable orphan, an `Evicted` straggler,
a TWD version skew.

### Multi-arch on the install path

Two machines pull that image, and they are not the same architecture:

| Puller | What it runs | Architecture |
|---|---|---|
| The GitHub runner | The per-leg worker, under docker compose | amd64 |
| The tenant's cluster node | The app pod Heracles fetches the DAG from at submit | may be arm64 |

A single-arch image satisfies whichever of the two matches the build and fails the
other. Nothing in between catches it: GM accepts the version, LM accepts the
install, `deployment_status` even goes green for a while, and the tenant's kubelet
fails the pull ~2 minutes later with `no matching manifest for linux/arm64`.
FND-31's first live install ended exactly there.

It must be *both*, not retargeted to the tenant's architecture: dropping amd64
would break the local worker instead.

**One architecture per job, each on a runner native to it.** `build-e2e-image` is a
matrix — `ubuntu-latest` builds `linux/amd64`, `ubuntu-24.04-arm` builds
`linux/arm64` — and `merge-e2e-image` combines the two with `docker buildx
imagetools create`, a registry-side operation on digests that takes seconds.

The obvious alternative, one job with `--platform linux/amd64,linux/arm64`,
emulates the non-native half under QEMU. That is 5-10x native — a figure this org
has already measured and [documented](https://github.com/atlanhq/mothership), and
`mothership`'s own `build.yml` and `lh-compute-duckdb` both use `ubuntu-24.04-arm`
for exactly this reason. Two native jobs in parallel cost about one build; one job
emulating costs several. On a path that runs on every install-path e2e, that is the
whole design. arm64 runners are also ~20% cheaper per minute, so it is not a
speed-for-money trade.

Four things this shape makes load-bearing:

- **The base image's architectures.** Every leg does `FROM` the runtime base, so
  the base must serve every architecture the matrix builds. On an e2e-labelled
  SDK PR that base is not the released `app-runtime-base:3` but a PR-scoped one
  built by `build-sdk-base-image` in `pull_request.yaml` — a different workflow,
  which stayed `linux/amd64` after this matrix went two-arch. The arm64 leg then
  died on `no match for platform in manifest` before a line of the connector
  Dockerfile ran, and the error names the base image rather than the workflow
  that published it. This is the one item in this list that fails *loudly* —
  it just fails somewhere that reads like it has nothing to do with the SDK PR.
  `test_build_app_image_action.py` derives the required set from the matrix
  itself, so a third architecture cannot leave the base behind.

  **That base uses the same native-split shape**, for the same reasons: a
  per-arch `build-sdk-base-image` matrix on native runners, combined by
  `merge-sdk-base-image` with `imagetools create`. Adopting it required fixing
  `secure-build-push-apps` first — its scan step hardcoded
  `platforms: linux/amd64` beneath a comment claiming it used the runner's
  native platform. On an x64 runner the two agreed, so the lie was invisible;
  on `ubuntu-24.04-arm` it builds an emulated amd64 image for Trivy and then
  pushes a different one, so the scan stops describing the artefact it gates.
  It now follows `runner.arch`.

  **Both base-image jobs are named in every downstream gate**, and that is the
  same trap the e2e matrix documents: a failed arch leg leaves the merge
  *skipped* rather than failed, and skipped is the benign value. Gating on the
  merge alone would dispatch the connectors with `base_image_ref` pointing at a
  manifest tag that was never created — every connector build then failing on a
  missing image, one repo from the cause. `connector-tests` gates on both, and
  `verify_connector_gate_upstream.py` additionally rejects the
  build-succeeded-but-merge-skipped pair outright.

The other three fail *silently*:

- **The runner/platform pairing.** `platform: linux/arm64` on an x64 runner still
  succeeds — just emulated. Nothing goes red; the build is simply several times
  slower forever. `test_build_app_image_action.py` pins each leg's platform to a
  runner native to it.
- **The cache scope.** `tag-suffix` suffixes the buildx cache scope as well as the
  tag, because two concurrent builds sharing one `type=gha` scope overwrite each
  other's cache manifest — after which every run finds the other architecture's and
  misses. One input drives both so the wrong combination can't be expressed. It
  defaults to empty, so the 17 repos calling `sdr-e2e` directly resolve to the
  byte-identical scope they always had.
- **`pkl`'s architecture.** `regenerate-contract` runs inside this build and used to
  fetch the x86 `pkl` asset unconditionally; on an ARM runner that is `cannot
  execute binary file`, several steps before anything mentions architecture. It now
  selects from `runner.arch`.

**Reading back a just-pushed tag is a race, so the build legs don't.** buildx's
container driver exports only to the registry, so `docker run` used to fetch back
the image the same step had just uploaded — 22.7s on a measured amd64 leg, and a
read the registry doesn't always serve yet (a live arm64 leg got `manifest
unknown` 0.7s after its own push reported success). The build now also `--load`s
into the local daemon, so the interpreter assert is a local container start: 0.17s,
and no registry read to race. Measured end to end, the amd64 leg went 89s → 76s.

`--load` is skipped for a multi-platform build, because the docker exporter can't
express a manifest list; such a caller falls back to pulling, which still works.

The one read that can't be avoided is `merge-e2e-image`'s: `imagetools create` is
purely registry-side, so the manifest list exists *only* there and inspecting it a
step later is inherently a fresh read. That one is wrapped in
[`with-retry.sh`](../../.github/scripts/with-retry.sh) — around the *inspect*, and
captured into a variable before parsing, because piping a retried command
concatenates every attempt's output into the reader's stdin and would fail the
parse on healthy data.

`merge-e2e-image` then asserts the combined manifest serves both architectures
(`assert_image_platforms.py`). That is the reference `prepare-tenant` publishes and
the tenant pulls, so it is the one worth asserting: a leg that quietly built the
wrong architecture produces an index with two of the same, and nothing downstream
notices — GM accepts the version, LM accepts the install. Failing here, where the
fix is obvious, replaces the diagnostic distance that cost FND-31 four runs.

Two seams worth knowing about when editing either action:

- **Skipped-step outputs are empty.** With `prebuilt-image` set, the build step is
  skipped and `steps.build.outputs.image` is `""`. One resolver step
  (`${PREBUILT:-$BUILT}`) is the single writer of the effective reference, and it
  hard-fails when both are empty rather than letting an empty `app_image` reach the
  configurator and surface as an opaque compose pull error. `test_build_app_image_action.py`
  regression-guards this.
- **Nested actions resolve at `@main`.** `sdr-e2e` invokes `build-app-image` as
  `@main`, so on the local-action dispatch path
  (`./.application-sdk/.github/actions/sdr-e2e`) the build steps come from main
  rather than from the application-sdk PR under test. `regenerate-contract` and
  `setup-deps` — both already inside this sequence — have the same property. A PR
  editing the build steps is covered by the remote `@main` path and the merge
  queue, not by a local-action dispatch.

## When a tenant-side node stalls (FND-708)

A full-DAG run dispatches most of its nodes to **tenant-side system apps** — `publish`, `lineage-publish` and `qi` run on the tenant's own queues (`atlan-publish-<deployment>`, …), not on the connector under test. So a node can sit for the whole poll for two quite different reasons: nothing is polling that queue (those apps are KEDA scale-to-zero on the Temporal queue, so a mid-redeploy app picks nothing up), **or** a worker took the activity promptly and then stopped making progress. Either way the connector is not at fault; the tell is the other clouds' legs passing on the same commit and the same manifest.

AE's node status cannot tell you which. It holds a node at `Pending` while that node's child workflow is running: on the run this section was written from, `lineage-publish` read `Pending` from t=489s to the ceiling while its child workflow had started 331ms into that window and spent 4,810 of its 4,882 seconds retrying one activity through repeated heartbeat timeouts (~72s was real work). It then completed successfully. So the harness reports the status, names the queue, and points at the child workflow instead of asserting a cause.

**Reading the failure.** A poll that ends on `ae_poll_timeout_seconds` is not a verdict on the nodes — it means the harness stopped watching. `poll_native_status` stamps that on the result it returns (`timed_out_after_seconds`, `seconds_since_last_progress`), and the assertion says so, per node:

```
DAG did not complete within 1800s (AE status=Running); no DAG node changed state for the last 1311s
DAG nodes:
  - publish: succeeded in 152s
  - lineage-publish: AE reports Pending at the 1800s poll ceiling — task queue
    'atlan-publish-production', app_name=publish, per the seed DAG; no DAG state change
    for the last 1311s. AE holds a node at Pending whether nothing picked it up OR its
    child workflow is running, so read the child workflow
    '<ae_run_id>-lineage-publish' on the tenant's Temporal: no such execution means
    nothing polled that queue (check the owning app's workers); an execution means it is
    running or retrying (check its history for heartbeat timeouts).
```

The child workflow ID is `{ae_run_id}-{node_id}`, and both halves are already in the failure, so that next click needs nothing the message does not carry.

Three states used to render identically as `status=<X> error=None`, which read as a node failure and named no queue: AE-reports-not-started (`Pending` / `Scheduled`), dispatched and then frozen (`Running` at the ceiling), and ran-and-failed. Only the third is a node failure. The queue name comes from the harness's seed DAG, which is the only place it is knowable locally — `native-status` reports statuses, not routing — so the line says "per the seed DAG" rather than claiming to know what the tenant dispatched.

**The watchdog must stay reachable.** `dag_progress_stall_seconds` fires when `elapsed - last_progress_elapsed` reaches the window, and the poll returns as soon as `elapsed` reaches `ae_poll_timeout_seconds`. A window that is not *strictly* below the ceiling can therefore only ever close on a run that stalls at t=0 — for every real stall the poll exits first. It used to default to an absolute 1800s, which silently disabled it on every suite with a ceiling of 1800s or lower; those suites burned the full 30 minutes on a wedge and then reported the ceiling. It now defaults to `None` = derived from the ceiling (a third of it, floored at 300s and capped at 1800s), so raising the ceiling widens the watchdog instead of putting it out of reach, and `setup_method` rejects a pinned value that is not below the ceiling. Set `0` to opt out deliberately.

Because a reachable watchdog closes *before* the ceiling, it — not the ceiling — is now the exit a stall actually takes on any suite whose ceiling is 1800s or lower. It raises `DAGProgressStalledError` rather than returning, so the same per-node breakdown is rendered onto that exception: the error carries the last observation (`DAGRunResult.progress_stalled_after_seconds`, alongside the ceiling's `timed_out_after_seconds`) and `run_full_dag` re-raises it through the one renderer above. The only difference in the output is the clause naming which exit closed — `AE reports Pending when the 600s progress watchdog closed` instead of `at the 1800s poll ceiling`. A node wedged `Running` reads the same way rather than falling back to `status=Running error=None`.

**Why the harness cannot shorten the timeouts that actually bind.** The node-level budget is generous by design: `publish` and `lineage-publish` default to a 3-day `startToCloseTimeoutSeconds` (right for a large tenant doing real work, see [contract-toolkit reference](../../contract-toolkit/docs/reference.md)). But the timeout that produced the stall above was one layer down — a `heartbeat_timeout_seconds` of 3600 on the `publish` activity, which turns each lost heartbeat into an hour of dead time before the retry, well past any e2e budget. Neither is reachable from the harness: at AE submit Heracles re-fetches the manifest from the **tenant-deployed pod** and that DAG is what executes (see [Building the image once](#building-the-image-once)); the seed DAG establishes the workflow record, not the graph. The only effective place is the app's own committed contract — which is the manifest that also ships to production, so shortening it for CI would mean e2e no longer exercises the contract we ship. Precise reporting plus a reachable watchdog is what the harness can do; a heartbeat watchdog in the owning system app ([ADR-0018](../adr/0018-progress-aware-heartbeat.md)) is where that class of stall actually gets fixed.

## What a red leg means when Atlas could not be read (FND-225)

`BaseE2ETest` and `SQLAppE2ETest` are unchanged names with unchanged class attributes and unchanged hooks — a connector suite, and the codegen'd `app/generated/_e2e_base.py` it subclasses, need no edit. What changed underneath is that the harness plumbing now lives in `application_sdk.testing.harness`, and one consequence is visible from a connector repo.

**An Atlas search that could not be read is no longer graded as a low count.** It used to arrive at the assertion ladder as zeros, so an expired token or a 503 from the asset server was reported as:

```
Atlas inventory under default/mysql/… did not meet expectations:
  - Table: got 0, expected >= 2
```

— a confident claim about the connector, made by a run that never read it. The harness readers report "could not read" as its own answer, and the ladder now raises `AtlasReadIndeterminateError` (`DEPENDENCY_UNAVAILABLE_ATLAS_READ_INDETERMINATE`) instead. It is deliberately **not** an `AssertionError`, so pytest reports the leg as an **error** rather than a failure:

```
Atlas could not be read under default/mysql/…, so 2 expectation(s) went ungraded.
This is not a verdict on the connector — the run never saw what it landed:
  - Table: could not be read, so the floor expectation was not graded: …
```

Read that as "re-run it", not as "the connector regressed". The same distinction closes the mirror bug on the location check (`expected_asset_qn_depth`), which used to fail **open**: a failed sample read returned `[]`, an empty sample is skipped, and so an auth fault was graded as a pass.

**`self.client` is deprecated.** Nothing in the base class routes through `AEWorkflowClient` any more; it is built on first access, warns, and goes away in v4.0. A suite that calls it directly — waiting on a connection it seeded itself, say — should call the harness functions instead:

```python
from application_sdk.testing.harness import atlas, run_sync

async def _wait() -> bool:
    async with atlas.atlas_client(base_url, api_key) as client:
        outcome = await atlas.poll_for_connection(
            client, qn, budget=self._atlas_connection_budget()
        )
        return isinstance(outcome, Settled) and outcome.value
```

**One behaviour changed on the seeded-connection path.** `seed_connection()` creates the Connection at the qualified name *this run minted*, rather than letting `Connection.creator` derive `default/<type>/<epoch>` and adopting whatever came back. That derivation had one second of resolution, so two legs of one e2e matrix starting in the same second shared a connection — and the first to finish purged the other's assets while both reported a clean teardown.

**Optionally, Temporal can be asked who is polling the extract queue.** `NoWorkerOnTaskQueueError` fires on an inference: nothing started inside `ae_stall_grace_seconds`, so probably nothing is polling. Set `temporal_address` on the suite (or export `E2E_TEMPORAL_ADDRESS`, plus `E2E_TEMPORAL_NAMESPACE`) and the harness reads the queue's pollers and attaches what it saw to the same error. Off by default because the connector CI runner has **no route into a tenant's vcluster** — the same constraint that makes the AE submit the only tenant-facing probe of the installed app pod — so it is for a suite driving a cluster it can actually reach. A read that fails changes nothing: the inference still stands.

## Contract regeneration before tests

The e2e/integration tests consume `app/generated/manifest.json` (the Automation Engine DAG): the host-side harness reads the committed file, and the connector Docker image `COPY`s `app/generated/` at build time and serves `manifest.json` at runtime. Nothing used to regenerate that file from `contract/app.pkl`, so a Contract Toolkit change — at the app level (`contract/app.pkl`) or the SDK level (`contract-toolkit/src`) — ran against a possibly-stale committed manifest and was never actually exercised (BLDX-1493).

The shared [`regenerate-contract`](../../.github/actions/regenerate-contract/action.yaml) composite regenerates `app/generated/**` from `contract/app.pkl` **before** the manifest is consumed (driver: [`.github/scripts/regenerate_contract.py`](../../.github/scripts/regenerate_contract.py)). It self-skips when there is no `contract/app.pkl`.

| Where it runs | Placement | Drift gate |
|---|---|---|
| `connector-integration-tests` (always-on host harness) | after the SDK-ref repin, before the app server boots | **Warn-only** — annotates a stale committed `app/generated/`, never fails |
| `build-app-image` (image-based; reached from `sdr-e2e`, incl. the full-DAG path via `e2e-full-reusable`) | **before the image build** (bakes the fresh manifest into the connector image) | Off (`check-drift: "false"` — uv/ruff aren't installed yet; the integration job owns the gate) |

- **App-level** (default): regenerate from the app's pinned `@app-contract-toolkit` version, so a `contract/app.pkl` change is exercised even when the committed manifest was not regenerated.
- **SDK-level** (cross-repo dispatch, `application-sdk-ref` set): the `@app-contract-toolkit` dependency is overridden to the SDK PR's `contract-toolkit/src`, so a toolkit change in the SDK PR is generated against the *real* connector contract end-to-end. Drift is expected, so the gate is skipped and a `pkl eval` failure is fatal.

Regeneration is bound to the build, so it runs wherever the build runs: once per leg while each leg builds its own image, and once per run for a caller that builds ahead of the matrix and passes `prebuilt-image` (see [Building the image once](#building-the-image-once)). The binding is the invariant — the fresh `app/generated/` must exist in the workspace at the moment the image is built — not the per-leg cardinality.

## Workflow-setup routes (FND-1667)

A contract change can 404 a connector's setup page while **every** local and CI check stays green. That is what shipped in FND-1593: the generated artifacts were self-consistent, conformance was clean, the generated-artifact freshness gate passed, and both `/workflows/setup/*` pages returned 404 in the UI. Nothing was stale or hand-edited — the break lived only in the join between what the contract generates and what the tenant serves, and no gate looked there.

`sdr-e2e`'s **Verify workflow-setup routes resolve** step closes that. It runs on the install path only (gated on `expected-app-version`, the same gate as the version verify), after the version check and before the suite.

What it asserts, in the direction the UI walks it:

1. locate this app's marketplace cards by app `name` **and** `entrypoint` — facts that are *not* the thing under test. `entrypoint` alone is not app-scoped: every connector's crawler card carries `entrypoint: "crawler"`.
2. `card.id` equals the `id` in the committed `app/generated/<ep>/<config>.json`. This is the assertion that bites — a check asserting `GET configmaps/<known-good-name> == 200` would have passed straight through FND-1593, because that name never stopped working. What moved was the card pointing at it.
3. `GET /api/service/configmaps/<card.id>` returns 200 and echoes back the name asked for.
4. the served form declares every input the committed contract does — a **subset** check, so platform-added fields are not brittle while a stale image still fails.
5. a negative control runs **first**: an unknown config name must really be rejected, or every 200 above is vacuous.

### Skips, and what they mean

| Situation | Outcome |
|---|---|
| No `manifest.json` anywhere under `app/generated/` (nothing generated) | **Skipped**, with a `::notice::` — no setup form exists to serve. Costs zero tenant calls. |
| Caller did not install to the tenant (`expected-app-version` empty) | Step does not run — the tenant serves some other version, and the subset check would report a stale image as a contract break |
| A declared entrypoint has no generated config, or a config has no `id` | **Fails** — the committed artifacts are incoherent, which is not "nothing to check" |

Skip-not-fail on the first two is deliberate: without it this would be a fleet-wide false positive on its first run.

### Timing

The catalog read is a **bounded poll** (`--wait-seconds`, default 120s), not a single read. `install()` polling the *deployment* to `SUCCEEDED` is not evidence that LM's catalog snapshot and the pod's configmap endpoint have caught up — nothing sequences those against the deployment verdict — so a single read would be flaky-by-construction on exactly the path CI takes. Progress lines are flushed, so a patient step does not read as a hung one.

### Where the logic lives, and why

The check is `application_sdk/testing/setup_routes.py`; the CI shell around it is [`verify_setup_routes.py`](../../.github/actions/sdr-e2e/verify_setup_routes.py) in the composite. The split is not arbitrary:

- The SDK is on **both** sides of the join being asserted. `/api/service/configmaps/<name>` is Heracles proxying to the app pod's own `GET /workflows/v1/configmap/{id}`, so the response envelope, the form-file selection rule and the generated-tree layout are read from `application_sdk/app/_generated_tree.py` — the same authority the server reads. A second copy would let the server serve one file while the check compared against another, and that mismatch would read as a contract regression.
- It therefore needs the SDK importable, which rules out `prepare-tenant`: that job runs a bare `python3` with no `uv sync`. By this point in `sdr-e2e` the app's environment is synced.
- The `e2e` job that invokes this composite has `prepare-tenant` in its `needs:`, so every step here is strictly after the install.

One insertion covers both e2e callers (`tests-reusable`'s `e2e` and `e2e-full-reusable`'s `e2e-full`), and no app repo carries any of it. `.github/scripts/tests/test_setup_routes_wiring.py` pins the placement, gate and injection discipline; `tests/unit/testing/test_setup_routes.py` proves the check bites, including a round-trip against the live configmap endpoint.

### Known limitation

The endpoint paths are `atlan-frontend`'s, not ours — `BASE_PATH = 'service'` plus `getAPIPath`, confirmed live. If the frontend changes how it derives the setup route, this check goes stale. The mitigation is that it is in one place rather than in every connector repo.

## Workspace-wipe defences (local-action mode)

When the SDR composite is invoked via local path (`./.application-sdk/.github/actions/sdr-e2e`) during cross-repo dispatch, `setup-deps`' inner `actions/checkout` wipes the entire workspace — including `${{ github.action_path }}` itself. The composite:

1. Stashes its full asset tree to `/tmp/sdr-e2e/` before `setup-deps` runs.
2. After setup-deps, resolves a `steps.action_root.outputs.path` that falls back to the `/tmp` stash if `${{ github.action_path }}` is now empty.
3. Restores the stash back to `${{ github.action_path }}` at the end of the action body so GH Actions can find `action.yml` for post-hook execution.

Single-pipeline apps invoking the action remotely (`@main`) never hit this code path.

## Multi-entrypoint (bundle) apps: one suite per entrypoint

An app whose contract declares `entrypoints` generates one manifest per
entrypoint — `app/generated/<ep>/manifest.json` — and each entrypoint is its own
Automation Engine submit, against its own DAG, its own task queue, and its own
served manifest. **A green crawler leg is no evidence about the miner.** So the
"one representative run" rule (see `T012`) means one run *per entrypoint* here,
and `T025 EntrypointWithoutE2ECoverage` reports any entrypoint without one.

This is only about **bundle mode**. An app that keeps a single marketplace card
and invokes secondary entrypoints as DAG nodes (`workflow_type: "<app>:<wire>"`
— the route/card split) has those executed inside the parent's own full-DAG run,
so they are covered transitively and `T025` never fires for them.

### Add a file per entrypoint

The e2e matrix fans out **one leg per `tests/e2e/test_*.py` file**, so a second
entrypoint needs a second file and no workflow change at all:

```
tests/e2e/test_myconn_crawler_e2e.py    → leg "myconn-crawler-e2e" × each cloud
tests/e2e/test_myconn_miner_e2e.py      → leg "myconn-miner-e2e"   × each cloud
```

Each subclasses that entrypoint's generated base:

```python
# tests/e2e/test_myconn_miner_e2e.py
from app.generated.miner._e2e_base import MinerGeneratedE2EBase
from application_sdk.testing.e2e import RunMode


@pytest.mark.e2e
class TestMyConnMinerE2E(MinerGeneratedE2EBase):
    mode = RunMode.AGENT
```

The generated base already carries this entrypoint's `manifest_path` and
`entrypoint` (without which AE fetches the bare manifest and 404s "No manifest
available"), the bundle's own identity fields, and expectations derived from that
entrypoint's `pipeline` — `expect_connection`, `require_nonempty_assets`,
`expect_lineage`, `required_dag_nodes`. A miner therefore is not graded against
crawler-shaped assertions: with no `publish` step its pass criterion is its DAG.

### Seeding state a dependent entrypoint consumes

A miner enriches a connection it does **not** create. The harness mints an
ephemeral qualified name but no Connection entity, so a miner run bare has
nothing to enrich. Create it in `seed_prerequisites()`, which runs immediately
before the DAG:

```python
class TestMyConnMinerE2E(MinerGeneratedE2EBase):
    mode = RunMode.AGENT

    def seed_prerequisites(self) -> None:
        # Creates the Connection under this test's own ephemeral QN, waits until
        # it is searchable, and retries the probe write until the connection's
        # access policies go live (a fresh connection 403s child writes).
        self.seed_connection(probe=self._write_a_table)
```

Seed under the harness's **own** `self.connection_qualified_name` — which
`seed_connection` handles — so `teardown_method` purges it with everything
beneath it. Never point a suite at a long-lived shared connection to skip the
seeding work: a left-over, half-set-up connection is exactly what greens a later
run that should have failed.

### When the state is an artifact only another DAG produces

`seed_prerequisites()` writes to Atlas through pyatlan, so it can seed only what
pyatlan can write. Some entrypoints consume something else entirely: a query-history
miner resolves lineage against an **entity-cache artifact in object storage** that
nothing but a crawl of the same connection writes. Seeding the Connection in Atlas
does not produce it, and the miner then runs to four green DAG nodes and zero
lineage — a pass that asserts nothing.

For that shape, declare the crawl as a run of its own. `dag_runs` is an ordered
tuple of `DAGSpec`s, each submitted, polled and graded on its own, all against the
one connection the suite mints:

```python
from application_sdk.testing.e2e import DAGSpec


class TestMyConnMinerE2E(MinerGeneratedE2EBase):
    mode = RunMode.AGENT

    dag_runs = (
        # Produce what the miner consumes: a real crawl of this connection.
        DAGSpec(
            manifest_path="app/generated/crawler/manifest.json",
            expect_connection=True,
            require_nonempty_assets=True,
            required_dag_nodes=("extract", "publish"),
        ),
        # Then this suite's own entrypoint, from the class attributes.
        DAGSpec(),
    )
```

Four things to know:

- **A spec overrides only what it sets.** Every field defaults to `None`, meaning
  "inherit the class attribute of the same name", so a suite that declares nothing
  runs exactly the one DAG it always did. `dag_runs = ()` is the default and the
  single-run path is unchanged — no signature break, no new required ClassVar.
- **Expectations are per run, not just identity.** They decide which Atlas probes
  *run at all*: `expect_connection` gates the connection poll and every count under
  it. A crawl declared inside a miner suite (`expect_connection = False`) would
  otherwise never observe the connection it just landed.
- **One connection, one teardown.** All runs share the suite's minted
  `connection_qualified_name`, and cleanup stays a single purge in
  `teardown_method` — which pytest runs on pass, fail **and** error. That
  guarantee is the reason this lives inside one pytest process instead of two
  ordered CI legs sharing a connection, where teardown would have to move to an
  `if: always()` job a cancelled workflow can still skip, on a leased shared tenant.
- **Grade each run on its own** by overriding `assert_dag_outcome(dag, outcome)`
  and branching on `dag.label` (the entrypoint name unless you set `DAGSpec.label`).
  The default is the standard ladder, resolved against that run. Outcomes
  accumulate on `self.dag_outcomes` in run order; they are never merged into a
  composite verdict, because a crawl outcome and a mine outcome assert different
  things.

**Cost.** The runs are serial by nature — each adds its own wall clock to that leg
(a postgres crawl is ≈5m40s; its miner is ≈3m plus a 5m lineage poll). Chain a run
only when a later one genuinely cannot work without it.

**This is not extra T025 coverage.** A prerequisite crawl inside a miner suite does
not count as the crawler's e2e suite, deliberately: it exists to seed, and it is
graded against the consuming suite's intent. The rule stays *one collectable class
per entrypoint, which may run prerequisite DAGs for others*.

### Seeding lineage parents another *source* owns

A lineage-only connector — Coalesce, ADF, Mode — publishes Process /
ColumnProcess entities whose `inputs`/`outputs` reference **another source's**
assets by qualified name: Snowflake tables under a Coalesce run, warehouse
tables under an ADF pipeline. On a connector-scoped e2e tenant that source has
never been crawled, so the publish fails wholesale (`ATLAS-404-00-00A`) — 72
entities on adf, 9 on mode, 19,210 on coalesce. Neither `seed_connection` (the
run's *own* connection) nor a prerequisite `dag_runs` crawl covers this shape.

#### Choose the approach first: crawl if you can reach it, seed if you cannot

There are now two ways to put a referenced source in place, and picking wrongly
is the expensive mistake. The rule:

> **Run a real crawl when the referenced source is reachable inside the leg. Use
> synthetic-publish seeding only when it is not.**

| Case | Reachable? | Approach |
| --- | --- | --- |
| postgres miner | Same app, two entrypoints; the hermetic container is already in the job | Real crawl via `dag_runs` |
| coalesce → Snowflake | Different app, external warehouse, no tenant credentials | `seed_assets` |
| adf → ADLS / Cosmos / Salesforce | Three external sources, none reachable | `seed_assets` |

A real crawl seeds from the producer that owns the data, so its QN parity holds
by construction and its assertions are calibrated against real crawl behaviour.
Seeding does neither — which is why every segment of a `SeedSpec` is validated
and the whole batch is checked offline before it is submitted. Reach for it only
when there is nothing to crawl.

#### What `seed_assets` does

**Two failure modes stack here, and only one of them is an Atlas entity.**

1. *Ref emission* is connector-side and needs the **connection cache**: with no
   cache loaded, coalesce sets `cache_unavailable` and emits every ref
   unvalidated, and mode falls back to PartialObjects. All three connectors
   above declare `connection_cache_enabled` + `connection_cache_via_app_enabled`.
2. *Ref resolution* is Atlas-side and needs the **entity**: the emitted ref must
   bind to something, by exact-match qualified name and exact type — no fuzzy
   matching, no case folding, and a `Table` never resolves a ref that said
   `View`.

Writing skeleton entities straight into Atlas with pyatlan addresses (2) and
nothing else. `build_connection_cache` in `atlan-publish-app` builds the cache
from a connection's *own transformed JSONL*; it does not snapshot arbitrary
connections out of Atlas, so a direct write produces no cache — and a
harness-authored cache blob would mean reimplementing a producer we do not own.
(This is [FND-1147](https://linear.app/atlan-epd/issue/FND-1147) one connection
over: *"it read 'prior crawl' as 'prior ASSETS' and seeded them with pyatlan,
which the lineage app cannot see."*)

So `seed_assets` seeds **through publish**: it serialises the transformed NDJSON
a crawler of that source would have emitted, uploads it, and submits one
`PublishWorkflow` node. Publish then owns the entities *and* the cache, from the
producer that owns them. No new app is needed — `publish` is a platform service
already on every tenant.

Declare the tree and call it from `seed_prerequisites()`:

```python
from application_sdk.testing.harness import seed as harness_seed


class TestCoalesceE2E(CrawlerGeneratedE2EBase):
    def seed_prerequisites(self) -> None:
        seeded = self.seed_assets(
            harness_seed.SeedSpec(
                connector_type="snowflake",   # the REFERENCED source's type
                # qualified_name / display_name omitted → minted per run
                databases=(
                    harness_seed.DatabaseSpec(
                        name="ANALYTICS",
                        schemas=(
                            harness_seed.SchemaSpec(
                                name="PUBLIC",
                                tables=(
                                    harness_seed.TableSpec(
                                        name="ORDERS", columns=("ID", "AMOUNT")
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )
        # Rebase the refs the connector will emit onto seeded.qualified_name
        # (e.g. via a mustache substitution or the connector's config), and point
        # its own publish node's `ars_lookup_connection_qns` at the same QN.
```

The seeded tree hangs under a **second** ephemeral connection, minted per run
exactly like the suite's own. Its QN *and* its object-store prefix are registered
before the seed runs, so `teardown_method` reclaims both (connections first, the
run's own before the seeded ones) even when the seed half-fails. Nothing here
touches a long-lived shared connection.

#### Three things to get right

- **QN parity is the whole contract.** Every segment must match what the
  connector under test emits **byte for byte, case included** — Snowflake refs
  are `.upper()`-d, so seed them upper-cased. Derive the spec from the
  connector's own committed transform goldens where you can; that is parity by
  construction rather than by hope. A connector whose warehouse QNs are not
  config-pinnable must precompute them from its source fixture, never invent
  them. Segments that cannot compose cleanly (empty, padded, or carrying a `/`)
  are rejected at declaration.
- **The pre-submit check needs the `[storage]` extra.** `seed_assets` runs
  `validate_transformed_dir(..., check_referential_integrity=True)` offline
  before it uploads anything, which is what turns "every parent is present" from
  hoped-for into asserted. The referential pass is backed by `rocksdict`; without
  it the walk degrades to per-asset validation and logs a warning. A leg that
  relies on this check should install the extra.
- **Cross-batch parity is still on you.** The check validates integrity *within*
  the seed. It cannot tell you the seed covers every ref the connector will
  emit — the coalesce pilot published 82 ColumnProcesses against a golden of
  110, silently dropping 28. Diff the seed's QN set against the connector's
  golden refs if the count matters.

#### CI wiring

`seed_assets` writes to a store the **tenant's** publish app reads, not the
connector's deployment store. `BaseE2ETest.seed_object_store()` resolves the
configurator-emitted `atlan-objectstore` Dapr component out of
`ci-deploy/components` — the tenant blobstorage binding the `sdr-e2e` action
already selects and mounts into the worker. A leg whose layout differs sets
`E2E_SEED_COMPONENTS_DIR` / `E2E_SEED_STORE_BINDING`; a suite that needs
something else entirely overrides `seed_object_store()`.

#### Sequencing a run against a connection the suite did not mint

`DAGSpec.connection_qualified_name` names the connection one run is submitted and
graded against. Left unset — the default, and what every run did before — it is
the suite's own minted connection, which is still right whenever the runs are
sequenced *because they share state on one connection* (the miner-after-crawl
case above). Set it for the opposite case: a run that prepares a **different**
connection for a later one to reference. The QN joins the same teardown registry
`seed_assets` writes to, so it is purged even if the run that was to consume it
never got that far.


## Onboarding checklist for a new connector

1. **Action manifest**: `app.yaml` at repo root (3 lines).
2. **Unified workflow**: copy `.github/workflows/tests.yaml` from mysql-app; swap connector references. This single file covers unit + integration tests (always) and full-DAG e2e (on the `e2e` label or `run_e2e=true` dispatch input).
3. **Config dir**: create `.github/sdr-e2e/` (new) or `.github/e2e/` (legacy). Files: `docker-compose.ci.yml`, `e2e-full-docker-compose.yaml`, `e2e-full-components/`, `seed.sql`, `make-secrets.py`, `make-secrets-e2e-full.py`.
4. **Tests**: unit + integration tests under `tests/unit/` and `tests/integration/`; full-DAG e2e under `tests/e2e/` (`SQLAppE2ETest` subclass for SQL connectors, otherwise the generated `BaseE2ETest` subclass — see [Which harness](#which-harness)). On a bundle app, one `tests/e2e/test_*.py` **per entrypoint** — see [Multi-entrypoint (bundle) apps](#multi-entrypoint-bundle-apps-one-suite-per-entrypoint).
5. **Repo secrets**: set the 7 entries from the table above.
6. **SDK matrix**: add `<connector>-app` to the `DEFAULT_MATRIX` in apps-sdk's `matrix-builder` job (`pull_request.yaml`) so `connector-tests` fans out to your connector automatically.
7. **Required check**: make `tests / Tests Gate` a required, unbypassable status check on the default branch, and remove any stale required checks left over from older workflows (`unit-tests`, `tests-passed`, …). Do this as soon as step 2 is merged — see below.

### The tests gate does not wait on the coverage bar

The exact `required_status_checks` context is **`tests / Tests Gate`** — the
caller's job **id** (`tests:` in the scaffolded `tests.yaml`), then the gate
job's name. The workflow name is not part of it, even though the UI's checks
list displays it as a leading segment; verified against the live `main` rulesets
on `atlanhq/atlan-mysql-app` (`tests / Tests Gate`, `suite / Conformance Gate`)
and `atlanhq/application-sdk` (inline jobs are a single segment, e.g. `SDK
Gate`). A context that matches no check protects nothing, so get this string
right before rolling it out.

Making it required is its **own** lever, with no prerequisite
beyond the check running something real. It is **not** gated on the four-tier
test bar, the 85% coverage target, or every tier being wired up — pytest already
exits non-zero when it collects nothing, so a vacuous pass is not possible, and
the gate's verdict comes from a tested driver
([`verify-test-gate`](../../.github/actions/verify-test-gate/action.yaml)) rather
than from a job's own exit status. Turn it on with a thin suite and grow the
suite behind it; a suite nothing enforces has no authority and degrades under
pressure.

The bar belongs to the *other* lever — **0-touch**, meaning conformance findings
block CI and Renovate merges its own PRs without a human. That is what
`atlan-application-sdk-conformance bootstrap --enforce true` sets, and it is the
one an app graduates to once its tests are meaningful. The two halves of 0-touch
are separately expressible too, for a repo that wants one without the other:

| Lever | Flag | Prerequisite |
|---|---|---|
| `tests / Tests Gate` is a required check | none — a GitHub branch-protection setting; no bootstrap flag governs it | none |
| Conformance findings block CI | `--conformance-blocking true\|false` | meaningful automated tests (four-tier bar) |
| Renovate merges without a human | `--renovate-automerge true\|false` | meaningful automated tests (four-tier bar) |
| Both of the above at once | `--enforce true\|false` (shorthand) | as above |

Coverage stays warn-only at the publish-time certification gate as well — see
[`app-certification.md`](app-certification.md#enforcement), where unit-test
pass/fail already blocks publish while the 85% threshold only annotates.

## Reference

- [Reference adopter walkthrough (mysql-app)](https://github.com/atlanhq/atlan-mysql-app/blob/main/docs/CI-E2E.md)
- SDR composite action: [`.github/actions/sdr-e2e/action.yaml`](../../.github/actions/sdr-e2e/action.yaml)
- Full-DAG reusable workflow: [`.github/workflows/e2e-full-reusable.yaml`](../../.github/workflows/e2e-full-reusable.yaml)
- Cross-repo dispatcher action: [`.github/actions/e2e-apps/action.yaml`](../../.github/actions/e2e-apps/action.yaml)
- Test harness: [`application_sdk/testing/e2e/`](../../application_sdk/testing/e2e/) (the deprecated predecessor, [`application_sdk/testing/full_dag/`](../../application_sdk/testing/full_dag/), is removed in v4.0)
- Series of merged PRs that built this:
  - [#1669](https://github.com/atlanhq/application-sdk/pull/1669) — SDR composite + pytest base
  - [#1710](https://github.com/atlanhq/application-sdk/pull/1710) — Cross-repo dispatch + full-DAG harness + sticky comments
  - [#1746](https://github.com/atlanhq/application-sdk/pull/1746) — `.github/sdr-e2e/` convention + `app.yaml` requirement
  - [#1752](https://github.com/atlanhq/application-sdk/pull/1752) — Path-override inputs for multi-pipeline apps
