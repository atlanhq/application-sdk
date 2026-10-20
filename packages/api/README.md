# atlan-application-sdk-api

> **Experimental.** The contents of this package and how it is built may change
> between releases. If you are building an Atlan app, depend on
> [`atlan-application-sdk`](https://pypi.org/project/atlan-application-sdk/),
> which installs this package for you.

The **handler** part of the
[Atlan Application SDK](https://github.com/atlanhq/application-sdk), packaged
on its own.

An Atlan app has two halves:

- a **worker**, which runs the app's workflows (extraction, publishing) on
  Temporal and Dapr;
- a **handler**, a small HTTP service that answers Atlan while a user sets up a
  connection: *do these credentials work?*, *is the source ready?*, *which
  databases and schemas can I pick?*

This package contains everything the handler needs and none of the worker
runtime. Its core dependencies are FastAPI, Pydantic, orjson, httpx and
python-multipart. That lets one lightweight process serve the handlers of many
apps.

## Do I need this?

Usually not. `atlan-application-sdk` depends on this package at exactly the
same version, so installing the SDK gives you both, and your imports stay the
same:

```python
from application_sdk.handler import Handler  # same module either way
```

Install this package directly only to run handlers **without** the worker
runtime, for example to serve them from a separate, smaller service.

## Installation

```bash
pip install atlan-application-sdk-api
# or
uv add atlan-application-sdk-api
```

Requires Python 3.11 or newer. Add an extra when your handler uses that part of
the SDK:

| Extra | When your handler uses |
|---|---|
| `sql` | The SDK's SQL client (SQLAlchemy) |
| `aws` | The SDK's AWS helpers (boto3), or the config and file routes |
| `pandas` | Query results as pandas DataFrames |
| `pyatlan` | The Atlas conversion helpers on `ConnectionRef` |

```bash
pip install "atlan-application-sdk-api[sql]"
```

The extras have the same version pins as the matching extras of
`atlan-application-sdk`.

## Quick start

Write a handler by subclassing `Handler` and implementing its three methods.
Then turn it into a FastAPI app with `build_asgi_app`:

```python
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.handler.asgi import build_asgi_app


class DemoHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Check input.credentials against your source.
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        # Confirm the source is ready to be extracted.
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        # List what the user can choose to extract.
        return SqlMetadataOutput(
            objects=[{"TABLE_CATALOG": "analytics", "TABLE_SCHEMA": "public"}]
        )


app = build_asgi_app(DemoHandler(), app_name="demo")
```

Serve it with any ASGI server (not included), for example
`pip install uvicorn` and then `uvicorn demo:app`:

```bash
curl -X POST localhost:8000/workflows/v1/auth \
  -H 'content-type: application/json' -d '{"credentials": []}'
# {"success": true, "data": {"status": "success", ...}, "message": "Authentication success"}
```

## Routes

`build_asgi_app` serves the same routes the full SDK's handler service does, so
an app answers identically from either one:

| Route | Answered by |
|---|---|
| `GET /health` | Always `{"status": "ok"}` |
| `POST /workflows/v1/auth` | `Handler.test_auth` |
| `POST /workflows/v1/check` | `Handler.preflight_check` |
| `POST /workflows/v1/metadata` | `Handler.fetch_metadata` |
| `POST /workflows/v1/warmup` | `Handler.warmup` (optional) |
| `GET /workflows/v1/configmap/{id}`, `/configmaps`, `/input-contract`, `GET`/`POST /workflows/v1/manifest` | The app's `generated/` folder, if the app package ships one |
| `GET`/`POST /workflows/v1/config/{id}`, `POST /workflows/v1/file` | An S3 bucket named by `ATLAN_S3_BUCKET` (and `ATLAN_S3_REGION`); needs the `aws` extra. These routes return 503 when it isn't set |
| Your own routes | FastAPI routers returned from `Handler.routers()`, served after the SDK's |

`build_asgi_app` takes a few keyword options:

- `app_package`: where the app's code and `generated/` folder live;
- `blocking_threads`: the size of this app's thread pool, default 8;
- `generated_dir`;
- `object_store`.

Each app built this way gets its own thread pool for `run_in_thread`, so several
apps in one process don't block each other.

## What's included

| Module | Contents |
|---|---|
| `application_sdk.handler` | `Handler`, `HandlerContext`, and the request and response models |
| `application_sdk.handler.asgi` | `build_asgi_app` |
| `application_sdk.errors` | `AppError` and typed error classes; secrets and URL credentials are redacted from messages |
| `application_sdk.credentials` | Credential specs, types and parsing |
| `application_sdk.contracts`, `application_sdk.templates.contracts` | Input-contract models |
| `application_sdk.common.concurrency` | `run_in_thread`, for blocking calls inside async handlers |
| `application_sdk.common.logger` | `get_logger` |
| `application_sdk.clients.sql` (`sql` extra) | The SQL client base |
| `application_sdk.common.aws_utils` (`aws` extra) | AWS helpers |

The complete file list is in
[`api-files.txt`](https://github.com/atlanhq/application-sdk/blob/main/packages/api/api-files.txt).

**Not included:** Temporal, Dapr, the SDK's object-store layer, its structured
logging and telemetry, or anything else that runs workflows. That stays in
`atlan-application-sdk`.

A few handler features depend on the worker runtime. Without it they raise a
clear, typed error instead of failing on an import. For example,
`self.context.get_secret()` raises `SecretStoreNotConfiguredError` because
there is no secret store. Credentials sent with the request work normally.

## Versioning

This package is released together with `atlan-application-sdk`, from the same
commit and always with the **same version number**. For example,
`atlan-application-sdk` 3.51.0 requires `atlan-application-sdk-api` 3.51.0.

A new version can contain the same code as the previous one when a release
changed only the worker part of the SDK.

## Upgrading from an SDK release older than this package

Before this package existed, `atlan-application-sdk` shipped these files itself.
Upgrading across that boundary **with pip** can delete them. pip installs this
package first, then deletes the same files when it removes the old SDK.
Starting the app (`python -m application_sdk.main`) reports this and tells you
to run:

```bash
pip install --force-reinstall --no-deps atlan-application-sdk-api
```

uv is not affected. This only happens once; later upgrades are clean.

## Links

- Source and documentation:
  [github.com/atlanhq/application-sdk](https://github.com/atlanhq/application-sdk)
- Design notes:
  [docs/standards/api-distribution.md](https://github.com/atlanhq/application-sdk/blob/main/docs/standards/api-distribution.md)
- Full SDK: [atlan-application-sdk on PyPI](https://pypi.org/project/atlan-application-sdk/)
- License: Apache-2.0
