# The api distribution (`atlan-application-sdk-api`)

The consolidated API host serves every hosted app's handler (auth / check /
metadata) from one process. It cannot install `atlan-application-sdk` per app:
the worker SDK carries Temporal, Dapr, the object store and the rest of the
worker runtime. So a **listed subset of `application_sdk/`** is also published
on its own, as `atlan-application-sdk-api`.

There is one source tree and one import path. `from application_sdk.handler
import Handler` is the same file whether the full SDK or only the api
distribution is installed. Nothing is copied, shimmed or deprecated.

## How it is built

- `packages/api/api-files.txt` is the one list of files the api wheel ships.
- `packages/api/hatch_build.py` copies those files into the sdist and the wheel.
  An editable build copies nothing, so the SDK's dev env keeps importing the
  source tree.
- `atlan-application-sdk` also ships every listed file, byte for byte, and pins
  `atlan-application-sdk-api==<its own version>`. The overlap is deliberate.
  With disjoint wheels, `pip install -U` from a release older than the split
  deletes the files the api wheel has just written, and nothing reports it.
- `release.py` moves the api version and the pin together, and
  `tag-and-publish` publishes the api package first.

## Rules for a listed file

`check_api_surface.py` enforces these in CI (the `API Suite Tests` gate job):

1. It imports only other listed `application_sdk` modules at module level.
2. Its third-party module-level imports are dependencies of `packages/api`.
   Function-level imports of an **extra** (`sql`, `pandas`, `aws`) are fine.
   A handler that needs one declares the extra, as it does on the worker.
3. A function-level import of an unlisted module is wrapped like this:

   ```python
   try:
       from application_sdk.storage.ops import download_file
   except ModuleNotFoundError as exc:
       if not worker_only_missing(exc):
           raise
       raise ObjectStoreNotConfiguredError() from exc  # the stated fallback
   ```

   `worker_only_missing` (`application_sdk._install`) is true only on an
   api-only install. With the full SDK installed a missing module still raises.

`probe_api_wheels.py` then checks the **built** wheels:

- identical bytes in both wheels;
- a pip upgrade from the last release keeps every listed file;
- an api-only venv imports every listed module within 120 MB RSS, loads nothing
  unlisted, and serves auth, check, metadata and an `AppError` path without
  leaking a DSN.

## Adding a file

Add the path to `api-files.txt` and run
`python3 .github/scripts/check_api_surface.py`. It names every import that
needs to become listed, declared, or guarded.

## App side

An app with a handler keeps it in a uv workspace member (`api/`). The member
depends on `atlan-application-sdk-api` (plus any extras) and declares
`[project.entry-points."atlan.app_api"]`. The worker depends on
`atlan-application-sdk` and imports the handler from the member. The
tests-reusable `api-member` job installs the member alone and mounts it with
`application_sdk.handler.asgi.build_asgi_app`, exactly as the host does. See
the `api-server-consolidation-migration` skill.
