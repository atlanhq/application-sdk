---
name: api-server-consolidation-migration
description: Put a connector app's handler on the consolidated API host with atlan-application-sdk-api. The code stays in app/; api/ is generated. Use when onboarding an app to the common API server.
---

# Serve an app's handler from the consolidated API host

The host installs `atlan-application-sdk-api` (a subset of `application_sdk`,
see `docs/standards/api-distribution.md`) plus one small package per app, built
from `app/` at build time. The handler **stays in `app/`**, and imports stay `application_sdk.*`.
The reference migration is atlan-mysql-app#778.

## Steps

1. Add the config to the app's root `pyproject.toml`:

   ```toml
   [tool.atlan-app-api]
   handler = "app.handler:MySQLAppHandler"      # the Handler subclass
   data = ["app/sql/test_authentication.sql"]   # files the handler reads
   dependencies = ["aiomysql>=0.3.0"]           # the handler's own deps
   extras = ["sql", "aws"]                      # atlan-application-sdk-api extras
   ```

2. Run the fixer from the app repo root, and commit the result:

   ```bash
   python3 <sdk>/.github/scripts/gen_app_api.py fix
   ```

   It makes imports between the handler's `app/` files relative
   (`from .client import SQLClient`), and moves `run_in_thread` to
   `application_sdk.common.concurrency` and `get_logger` to
   `application_sdk.handler`. Nothing else is committed: there is no `api/`
   folder.

3. Check it the way CI will:

   ```bash
   python3 <sdk>/.github/scripts/gen_app_api.py check
   python3 <sdk>/.github/scripts/gen_app_api.py build --out /tmp/dist-api
   uv venv /tmp/api-only && uv pip install --python /tmp/api-only /tmp/dist-api/*.whl
   /tmp/api-only/bin/python <sdk>/.github/scripts/probe_app_api_member.py \
       --name <app> --package atlan_<app>_api
   ```

   A `ModuleNotFoundError` names a worker-only import in handler code. Use the
   api-shipped equivalent, or ask for the SDK module to be added to a group in
   `[tool.atlan-api.seeds]`.

## Before the SDK release that ships the api distribution

Pin both packages to the same SDK commit in `[tool.uv.sources]`
(`atlan-application-sdk`, and `atlan-application-sdk-api` with
`subdirectory = "packages/api"`); `build` copies the pin into the wheel's
dependencies. After the release, drop both sources.
