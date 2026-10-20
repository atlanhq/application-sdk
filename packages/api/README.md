# atlan-application-sdk-api

> **Experimental.** This package exists for the consolidated API host
> (common-app-server). Its contents and the way it is built may change between
> releases while the host rolls out. Apps should not depend on it directly.

The handler surface of the
[Atlan Application SDK](https://pypi.org/project/atlan-application-sdk/):
`application_sdk.handler`, `.contracts`, `.errors`, the credential specs, the
HTTP routes an app serves (auth, preflight, metadata, forms, manifest, config,
file upload), `run_in_thread`, and the SQL-client and AWS helpers handlers use.
It is a listed slice of the SDK's own source tree, not a copy: the files live in
`application_sdk/` at the repository root and are listed in
[`api-files.txt`](api-files.txt), generated from `[tool.atlan-api.seeds]`.

It has none of the worker runtime (no Temporal, Dapr, object store or
OpenTelemetry), so the consolidated host can serve many apps' handlers from one
small process.

## You almost certainly do not install this yourself

`atlan-application-sdk` depends on this package at exactly its own version, so
installing the SDK installs both. Import paths are unchanged:

```python
from application_sdk.handler import Handler  # works exactly as before
```

The consolidated host installs this package on its own, plus one small package
per app built from that app's `app/` handler (see
`.github/scripts/gen_app_api.py` and `docs/standards/api-distribution.md`).

## Extras

| Extra | For |
|---|---|
| `sql` | Handlers that use the SDK's SQL client base |
| `aws` | Handlers that use the SDK's AWS helpers |
| `pandas` | Handlers that read query results as DataFrames |
| `pyatlan` | The input-contract models' Atlas conversion helpers |

## Upgrading with pip from an SDK release before this package existed

Before this package, `atlan-application-sdk` shipped these files itself. A
`pip install -U atlan-application-sdk` across that boundary installs this
package and then deletes its files while removing the old SDK. Recover with:

```bash
pip install --force-reinstall --no-deps atlan-application-sdk-api
```

uv is not affected. It happens once: later upgrades are clean.

## Releases

Released together with `atlan-application-sdk`, at the same version, from the
SDK's release workflow: this package is published first, then the SDK.
