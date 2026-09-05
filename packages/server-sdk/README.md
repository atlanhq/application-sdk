# atlan-server-sdk

The lean, serving-only runtime for Atlan app servers: the auth / preflight /
metadata handler surface plus FastAPI assembly, with no worker or
data-processing dependencies.

## Why it lives here

`server-sdk` is a package inside the `application-sdk` repository, not a
separate repository and not a second SDK. It inherits this repo's release
automation, semantic versioning and changelog machinery, and it keeps error
codes, categories and standards single-source across the worker and serving
surfaces. App developers see one SDK; the split between serving and worker
code is an internal packaging boundary.

`application-sdk` takes `atlan-server-sdk` as a dependency, source-pinned to
this directory — so a change here is picked up immediately, with no
release-then-bump-the-pin cycle.

## What is in it

- `build_asgi_app(...)` — FastAPI assembly for an app's serving surface
- `Handler` ABC — `test_auth` / `preflight_check` / `fetch_metadata`
- `SQLHandler` — declarative base for SQL connectors
- `BaseSQLClient` — SQLAlchemy wrapper with `DatabaseConfig`
- `server_sdk.manifest` — manifest routes and the `atlan-<app>-<deployment>`
  task-queue convention
- S3 `ConfigStore`, scoped per `app_name`

## Extras

| extra | pulls in | for |
|---|---|---|
| `sql` | sqlalchemy | the SQL auth / metadata path |
| `aws` | boto3 | AWS-IAM helpers for AWS-hosted SQL sources |
| `serve` | uvicorn | running a server standalone |
| `workflow` | temporalio | enables `POST /workflows/v1/start` |

The serving path deliberately excludes temporalio, dapr, daft, duckdb, pandas,
pyarrow, boto3 and pyatlan — those are worker-side. The `workflow` extra is
installed by an app running standalone and never in the consolidated serving
image; the `/start` route is not registered when it is absent.

## Design rule

App identity is passed explicitly and never read from process-global
environment variables. Once several apps share one process, a process-global
value (`ATLAN_APPLICATION_NAME`, `ATLAN_CONTRACT_GENERATED_DIR`,
`ATLAN_TASK_QUEUE`) cannot identify any one of them — there is no correct
value. `app_name` is passed in at assembly and the config store, generated
contracts and task queue all resolve from it.

## Tests

```bash
cd packages/server-sdk
uv run --extra sql --extra test pytest tests/ -q
```
