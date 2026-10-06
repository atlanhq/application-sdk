---
name: migrate-orchestration
description: >
  Move a connector app off direct Temporal and SDK-internal orchestration
  onto the SDK's public seam: workflow and run ids from the App context, the
  App primitives and decorators from application_sdk.app, the client, error
  and converter types from application_sdk.execution, dev boot through
  run_dev_combined, HTTP through @entrypoint, and workflow-context I/O moved
  into a @task. Clears P004 (temporalio imports), P005 (private SDK
  orchestration imports), P017 (hand-built worker / client boot), P018
  (hand-built HTTP server) and P021 (I/O inside run / @entrypoint / signal /
  query / update). The done-bar is runtime parity: the workflow type,
  activity names, retry and timeout values, task queues, HTTP routes and the
  run paths the app derives from its ids stay the same, unless the developer
  accepts the change. Anything that changes workflow history or routing stops
  for an owner decision.
runs_before: [migrate-deprecated-symbols]
routes_to: [migrate-storage, migrate-deprecated-symbols]
mandatory_triggers:
  - "/migrate-orchestration"
  - "remove temporalio imports"
  - "P004 temporalio import"
  - "private SDK orchestration import"
optional_triggers:
  - "workflow.info replacement"
  - "run_dev_combined migration"
  - "move I/O out of run()"
owner: connector-platform-team
last_updated: "2026-10-06"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app code and tests with no temporalio import and no private SDK orchestration import, except agreed ignores
  - run() / @entrypoint reading ids from self.context and self.run_id; @task reading input.workflow_id
  - dev boot through application_sdk.main.run_dev_combined; HTTP through @entrypoint
  - workflow-context I/O moved into @task methods that return their decision
  - tests rewritten on the SDK's integration fixture kit, or the v2 residue removed after the developer agrees
---

# Migrate to the orchestration seam (P004, P005, P017, P018, P021)

## Conformance rules this skill clears

P004, P005, P017, P018, P021. These rules name this skill as their
`remediation_reference`, and `/remediate` hands their findings here. When the
skill is done, run
`atlan-application-sdk-conformance detect --rule P004,P005,P017,P018,P021`
and confirm none of these rule ids is still reported. P005 sites in `app/`
are usually B008 sites too; re-check with
`atlan-application-sdk-conformance detect --rule B001,B008`.

## What each rule fires on

| Rule | Fires on | Stops when |
|---|---|---|
| P004 | any `temporalio` import, at any depth (an import inside a function counts) | the code imports from `application_sdk.app` / `application_sdk.execution` instead |
| P005 | an import of a private SDK path (`application_sdk.<...>._x`) or a private name from a public SDK module | the import is public, or carries a justified ignore |
| P017 | imports of the v2 boot surface (`application_sdk.worker`, `application_sdk.application`, `application_sdk.clients.temporal`); calls to `create_worker`, `create_temporal_client`, `AppWorker`; `setup_workflow` / `start_workflow` / `start_worker` on `self`, `app` or an SDK name | boot goes through `run_dev_combined` or the base-image CLI |
| P018 | `FastAPI(...)`, `uvicorn.run` / `Server` / `Config`, `setup_server` / `start_server` / `include_router` on `self`, `app` or an SDK name | HTTP is `@entrypoint` methods, or a test fake uses the SDK's fake-source helpers |
| P021 | curated I/O (`requests`, `httpx`, `open`, `os.listdir`, `os.environ`, `shutil`, `subprocess`, pandas / pyarrow reads, `application_sdk.storage` calls …) directly in the body of `run`, an `@entrypoint`, or a `@signal` / `@query` / `@update` | the I/O is in a `@task` that returns its result |

**Tests:** P004, P005, P017 and P018 scan `tests/` too; P021 does not. The
call checks of P017 are exempt under `tests/integration/` only. Suppression
form, on the line or a comment-only line directly above:
`# conformance: ignore[<ID>] <reason>`.

**Trap:** rewriting a private `from application_sdk.execution._temporal.worker import create_worker`
to the public path makes its **call** fire P017 anywhere outside
`tests/integration/`. Move such a test onto the fixture kit instead. The
P004 finding message suggests `create_worker` / `create_temporal_client` for
temporalio worker and client imports; in a test, ignore that hint and follow
this skill (P017 does not see a bare temporalio `Worker(...)` or
`WorkflowEnvironment`, but they are still v2 residue).

**Routing:** P005 also fires on private SDK names that are not orchestration.
Storage helpers (`storage.ops._resolve_store`, `storage.formats.utils._download_files`,
`activity_utils.get_object_store_prefix` / `build_output_path`) belong to
`migrate-storage`; other non-orchestration privates (`constants._HTTP_POOL_*`,
`outputs._current_outputs`, `handler.service._app_error_to_http_status`)
belong to `migrate-deprecated-symbols`. List them; do not migrate them here.

## After-shape APIs

| Need | Public API | Import | SDK floor |
|---|---|---|---|
| workflow id in `run()` / `@entrypoint` | `self.context.workflow_id` | App | 3.20.1 |
| run id in `run()` | `self.run_id` | App | 3.0.0 |
| workflow id in a `@task` | `input.workflow_id` | typed Input | 3.0.0 |
| start time, current time | `self.context.started_at`, `now()` | App / `application_sdk.app` | 3.0.0 / 3.13.0 |
| workflow primitives | `now`, `sleep`, `uuid4`, `wait_condition`, `signal`, `query`, `update` | `application_sdk.app` | 3.13.0 |
| decorators | `task(name=, timeout_seconds=, heartbeat_timeout_seconds=, retry_policy=, retry_max_attempts=, pool=)`, `entrypoint(name=, default=)` | `application_sdk.app` | 3.0.0; `pool` 3.21.0 |
| retry policy | `RetryPolicy(max_attempts=3, initial_interval=1s, max_interval=5m, backoff_coefficient=2.0, non_retryable_errors=())`; the `@task` keyword defaults differ: `retry_max_attempts=3`, `retry_initial_interval_seconds=1`, `retry_max_interval_seconds=30` | `application_sdk.app` | 3.0.0 |
| sandbox passthrough | `passthrough_modules: ClassVar[set[str]]` on the App class | App | — |
| workflow-type rename alias | `legacy_workflow_types` on the App class | App | 3.29.0 |
| client type, failure types | `TemporalClient`; `TemporalWorkflowFailureError`, `TemporalActivityError`, `TemporalCancelledError`, `TemporalChildWorkflowError`, `TemporalTerminatedError`, `TemporalTimeoutError` | `application_sdk.execution` | 3.20.0 / 3.21.0 |
| converters, backend | `create_data_converter_for_app`, `TemporalExecutorBackend` | `application_sdk.execution` | 3.20.0 |
| dev boot | `run_dev_combined(app_class, *, credential_stores=, example_input=, task_queue=, ...)` | `application_sdk.main` | 3.0.0 |
| integration tests | `from application_sdk.testing.integration.fixtures import *` (the kit owns worker and client) | `application_sdk.testing.integration.fixtures` | — |

**No public equivalent today** (justified ignore with a tracked
`atlanhq/application-sdk` issue id; never invent one): `PreflightGateInput`,
`input_type_supports_gate`, `_resolve_gate_enforcement`, `_is_gate_broken`;
per-run dynamic task-queue routing (`execute_activity(..., task_queue=...)`
— `@task(pool=)` is static); `temporalio.exceptions.FailureError`,
`temporalio.client.WorkflowExecutionStatus`,
`temporalio.testing.WorkflowEnvironment`; `workflow.patched`; a public setter
for the App context in unit tests.

## Reference shapes

Dev boot (`atlan-mysql-app` `app/run_dev.py`):

```python
import asyncio
from application_sdk.main import run_dev_combined
from app.mysql import MySQLApp

async def main() -> None:
    await run_dev_combined(MySQLApp, temporal_ui=True, example_input={...})

if __name__ == "__main__":
    asyncio.run(main())
```

Production boots through the base-image CLI with
`ATLAN_APP_MODULE=app.mysql:MySQLApp`; `main.py` is a thin shim.

Ids without Temporal (`atlan-openapi-app` `app/connector.py`):

```python
workflow_run_at_ms = int(self.context.started_at.timestamp() * 1000)  # in run()
workflow_id = input.workflow_id                                       # in a @task
```

A module-level helper that called `workflow.info()` takes the id as a
parameter instead.

## Step 0 — Preconditions

1. Run the skills listed before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first
   (`adopt-preflight-gate` creates the preflight test imports this skill
   meets). To check, run `detect --rule` with each earlier skill's rule ids;
   if findings remain, ask the developer whether to finish that skill first.
2. Read the SDK version from `uv.lock`. Only if an API this skill needs is
   above it (see the floors), raise the SDK to
   the newest release that is at least 7 days old and at or above that floor
   (dependency cooldown; never a release younger than 7 days unless it fixes
   a known vulnerability). List releases with dates:
   `curl -s https://pypi.org/pypi/atlan-application-sdk/json | jq -r '.releases | to_entries[] | "\(.key) \(.value[0].upload_time)"'`.
3. Record the baseline outside the repo:
   `atlan-application-sdk-conformance detect --rule P004,P005,P017,P018,P021,B008 --exit-zero --output "$TMPDIR/before.sarif"`
   (`detect` prints SARIF and exits non-zero on findings; list each result's
   rule id and location from the file);
   `UV_FROZEN=1 uv sync --all-groups --all-extras` once (frozen, so the lock
   file is not rewritten), then `uv run --no-sync pytest tests/unit tests/integration`.
   Tests that already fail do not block this skill and must not get worse.
4. Record the runtime identity, outside the repo, from the running code, not
   from grep:
   - the App's `name` (the workflow type) and its `legacy_workflow_types`;
   - every registered task, inherited ones included:
     `TaskRegistry.get_instance().get_tasks_for_app(<App>.name)` from
     `application_sdk.app.registry`, in a script outside the repo; plus the
     preflight gate activity `<App name>:preflight` and any string activity
     name the app calls;
   - the **effective** retry and timeout values per task (decorator defaults
     included), and every task queue — the **production** queue, from the
     deployment's environment (for example `atlan-<app>-<deployment>`); the
     queue `run_dev_combined` uses locally differs and is for information
     only;
   - every HTTP route the app itself builds (routes the SDK serves do not
     count);
   - the run paths the app derives from its ids, with a sample id, and every
     environment variable that feeds them (for example
     `ATLAN_APPLICATION_NAME`) with its production value.
   If ids feed published object-store keys, capture delivery parity as
   `migrate-storage` Step 0.4 describes.

## Step 1 — Inventory and classify every site

One row per **use**, not per finding, `tests/` included: one import of
`workflow` can serve several `.info()` calls, and each call changes.

- **swap** — a public import of the same object, or a context value equal to
  the one it replaces: `workflow.info().workflow_id` in `run()` →
  `self.context.workflow_id`; `workflow.info().run_id` → `self.run_id`;
  `temporalio.client.Client` in an annotation → `TemporalClient`; temporalio
  failure classes → the `Temporal*Error` aliases; converters and backend →
  `application_sdk.execution`. Check each one against the installed SDK and
  cite the evidence: for the ids, the lines of `application_sdk/app/base.py`
  that build the `AppContext` from `workflow.info()` before `run()` starts.
- **retry / timeout** — a temporalio `RetryPolicy` or timeout. The SDK
  defaults differ (`max_attempts=3`, `max_interval=5m`; temporalio's
  `maximum_attempts=0` means unlimited) and fields are renamed
  (`non_retryable_error_types` → `non_retryable_errors`). Copy **every**
  value explicitly; never rely on a default.
- **context** — a helper outside `run()` that reads `workflow.info()`, a
  derivation inline in `run()`, or a unit test that patches it. Pass the id in
  as a parameter; when the derivation is inline, extract a module-level
  helper that takes the ids, so a unit test can call it without a context
  (the call from `run()` is then covered by the integration kit only). Off Temporal,
  `self.context.workflow_id` is a local placeholder, not the app's own
  fallback, so compare the derived paths. A unit test that calls `run()`
  directly needs a context: the SDK's `app_context` test fixture builds one,
  but attaching it means setting the private `app._context` (see owner
  decision 6).
- **boot** — a hand-built worker, client or server (P017, P018). Dev boot →
  `run_dev_combined`; HTTP → `@entrypoint` methods; a test that boots a worker
  → the integration fixture kit; a fake third-party API server in a test →
  the SDK's fake-source helpers or a justified ignore.
- **io** — workflow-context I/O (P021). Move it into a `@task` that returns
  its decision; transfers (`self.upload`, `self.download`, `upload_refs`) stay
  in `run()` (P008 forbids them in a task).
- **v2 residue** — a test or module written for the v2 harness (`Worker`,
  `Client`, `WorkflowEnvironment`, activity stubs). If the app's fixture-kit
  suite already runs the workflow end to end, or the test only exercises its
  own mocks, recommend removal at Stop 1; otherwise rewrite it on the kit.
  Remove a file only after the developer confirms.
- **no public equivalent** — see the list above.
- **routed** — a private SDK name that is not orchestration (see Routing).

**Owner decisions** — record each; do not apply without an answer:

1. **Activity names and workflow type.** Replacing `@activity.defn(name=...)`
   or string activity names with `@task` changes the registered activity type
   (`{app name}:{task name}`); in-flight workflows replay against the old
   names. Renaming the App or an entrypoint changes the workflow type and
   strands scheduled DAGs unless `legacy_workflow_types` lists the old one.
2. **Workflow history.** Moving I/O from `run()` into a `@task` (P021) adds an
   activity to the history: workflows in flight at deploy time fail replay,
   and `workflow.patched` is not public. Agree a drain or deploy window.
3. **Retry and timeout values** that the migration would change.
4. **Task queues and routing.** `run_dev_combined` sets its own task queue
   and interceptors; per-run agent-queue routing has no public seam.
5. **HTTP routes.** Replacing a hand-built route with an `@entrypoint`
   changes the URL; every external caller must move.
6. **Unit-test context.** No public setter exists for the App context.
   Options: set the private `app._context` from the `app_context` fixture,
   with a justified ignore; test the helper with the id passed explicitly; or
   remove a test that only covers its own mock.

**Stop 1** (see Agent protocol).

## Step 2 — Apply, in this order

1. **swap** and **context** sites; the run paths must equal the Step 0.4
   record.
2. **retry / timeout** sites, with every value copied.
3. **boot** sites; then **io** sites as agreed.
4. **v2 residue** as agreed; the agreed ignores for **no public
   equivalent**.
5. Leave **routed** sites to their skills.

Remove a `workflow.unsafe.imports_passed_through()` block only together with
its replacement: list the module in `passthrough_modules`, or move the
import to module scope.

## Step 3 — Prove it

1. `atlan-application-sdk-conformance detect --rule P004,P005,P017,P018,P021 --exit-zero --output <file>`
   and the same with `--rule B008` — nothing left from this skill's inventory
   except the agreed ignores, the owner-decision sites, the routed sites (name
   the skill each goes to) and the residue the developer chose to keep.
2. The tests pass, with the Step 0.3 command, and no failure that was not in
   the baseline. Report skipped tests separately: a skipped kit test hides a
   coverage gap.
3. Runtime parity: compare against the Step 0.4 record — the workflow type,
   activity names, retry and timeout values, the production task queue,
   routes and derived run paths are the same, or the difference was accepted
   at Stop 1. To derive run paths for a sample id off Temporal, call the
   extracted helper; if only `run()` can produce them, setting the private
   `app._context` in a script outside the repo is acceptable.
4. Boot check: start `run_dev_combined` (no Temporal or Dapr needed for this
   check), wait for the log lines `Registered app <name>` and the
   combined-mode start with its queue, confirm the health endpoint returns
   200, then stop the process.

**Stop 2** (see Agent protocol).

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site and its class, the Step 0.4
   record, and the owner decisions with a recommendation for each. No edits
   yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   the runtime-parity comparison and the boot check. Then hand off for PR
   review.
