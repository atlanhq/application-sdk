---
name: api-server-consolidation-migration
description: >
  Move an Atlan app's API surface onto the shared common-app-server host (API
  Server Consolidation) by serving its REAL app/ handler through
  atlan-application-sdk-api. Refactors the app to the canonical shape (openapi,
  mysql, metabase) instead of adding SDK or host special cases, adds the one
  [tool.atlan-app-api] block, runs application-sdk's gen_app_api.py fix and
  check, proves the handler with the api-only probe, on a host test image and
  on a tenant, then releases and lets the host's Renovate-managed pin move.
  Clears conformance rule P061 (HostedHandlerMigration). Use when onboarding an
  app to the consolidated host, replacing a hand-written server/ package, when
  P061 or P060 fires, or when a hosted app 503s, returns app_not_hosted, is
  ejected, or fails the api-member CI job.
metadata:
  author: connector-platform-team
  version: "1.0"
  category: connector-apps
  last_updated: "2026-10-21"
  staleness_days: 60
  keywords:
    - /api-server-consolidation-migration
    - P061
    - HostedHandlerMigration
    - move this app to the consolidated API host
    - migrate to common-app-server
    - delete the server/ package
    - atlan-app-api block
    - routeToCommonAPIServer
    - app_not_hosted
    - api-member job failing
---

# Serve an app's handler from the consolidated API host

`common-app-server` serves many apps' HTTP surfaces from one process per
tenant, so an app stops running its own always-on API-server pod. It serves the
app's **real handler** from `app/`, packaged at build time from one config block.
The older approach, a hand-written `server/` copy on `atlan-server-sdk`, drifted
from the real handler and is being removed.

Reference material, read on demand (paths relative to this skill):

- `references/routes-and-host.md`: every route the host serves, host
  behaviour, the trust model, onboarding the host pin, rollback.
- `references/validation.md`: the full local, host and tenant validation
  recipe, and the relock recipe.
- `references/troubleshooting.md`: symptom → cause → fix.

## Conformance rules this skill clears

P061. It names this skill as its `remediation_reference`, and `/remediate` hands
its findings here. When done, run
`atlan-application-sdk-conformance detect --rule P061` and confirm it is no
longer reported. P060 (logging in the hosted handler module, tier `block`) has
its own guide; step 3 clears it on the way.

| P061 finding | Steps |
|---|---|
| `<Handler> is not served from the consolidated API host yet` | 0 to 8, then 9 if the app has `server/` |
| `server/ is a hand-written handler copy on atlan-server-sdk` | 0 to 8 if not migrated yet, then **9 only after the host's pin serves the new package** (step 7). Deleting `server/` earlier takes the app's API offline on every tenant still routed to it. |
| `[tool.atlan-app-api].handler … does not name a class …`, `data pattern … matches nothing` | 2 |
| `Absolute import …`, `… is absent on the host` | 3 (`fix`), then 5 |
| `APPLICATION_NAME is the host's own name …` | 1 (use `self.context.app_name`), then 5 |
| `compute_manifest runs app code per request …` | 1: make the manifest static. Do not suppress it. |

## The stance: fix the app, never special-case the SDK or the host

**The target is the canonical shape**, the one atlan-openapi-app,
atlan-mysql-app and atlan-metabase-app have: a `Handler` subclass in `app/`,
a contract-generated `app/generated/`, static manifests, typed `AppError`
failures, no custom middleware, lifespan or startup work. When an app uses the
handler differently or diverges from those apps, **the remedy is to refactor
the app** until it fits. This is a forced approach: do not add a flag, hook,
seed, allowlist or per-app branch to `atlan-application-sdk-api`, the SDK's
routes, `gen_app_api.py`, or common-app-server to accommodate one app.

**Decision rule.** Change the SDK or the host only when all three hold:

1. the need is **generic**: openapi, mysql or metabase would need it too, or
   any app with that kind of source would;
2. it is **validated**: shown failing on the canonical shape, not only on this
   app's divergent code;
3. it lands as an SDK feature on its own PR, reviewed as such, and the app
   waits for its release.

Otherwise refactor the app. If the app cannot be refactored without a feature
that does not exist yet, **stop and escalate** (FND, connector-platform-team)
with the finding and the canonical comparison. Do not work around it.

| Divergence found | Remedy |
|---|---|
| Custom HTTP endpoints, or a FastAPI app/router built by hand | Return the routers from `Handler.routers()`, which already exists. Keep router code in the handler's files, imported relatively. A router redefining an SDK path is refused at startup. |
| Per-request manifest (`compute_manifest` in `app/<entrypoint>/core.py`) | Refactor to static data in the contract-generated manifest. |
| Logging in the handler module | Report through the return value or a typed `AppError`; the SDK routes log outcomes (step 3). |
| Handler relies on raw exception text reaching the caller | Raise typed `AppError` leaves (as mysql `app/failures.py`) or return failed checks. Response bodies are redacted on purpose. |
| Monkeypatching SDK private internals, e.g. `application_sdk.handler.service._CREDENTIAL_KEYS` or `_normalize_credentials` | **Must fix.** The SDK bridges these patches today, temporarily; they will break. Move to a supported, public SDK path (read what the handler needs from `input.credentials`). If no public path covers it, escalate under the decision rule. |
| Code that detects worker vs host with `import application_sdk` | **Must fix.** The api package provides `application_sdk`, so that import succeeds on the host and the app then loads worker-only code (this is how enrichment-studio was ejected on the host). Prefer code that does not need to know. If it must, use `importlib.util.find_spec("application_sdk.main") is None` (an api-only install). |
| MCP mounts, custom middleware, lifespan or startup work, heavy routes outside the handler | Out of scope until a generic SDK feature exists. **Stop and escalate**; leave the app on its own server. |

## Facts the steps rely on

- **Lockstep versions.** `atlan-application-sdk-api` is always released at the
  SDK's version from the same commit, and the SDK depends on it `==` its own
  version. Apps never declare it directly, **except** to test an unreleased
  SDK commit: then pin **both** `atlan-application-sdk` and
  `atlan-application-sdk-api` (subdirectory `packages/api`) to the same git
  rev, as atlan-mysql-app #778 does (step 4).
- **Behaviour is the worker's.** The shared routes answer exactly like the
  worker's handler service did before the split, **except two deliberate
  fixes**: secrets are redacted from response bodies, and a malformed request
  body (not JSON, or not an object) answers 422. Never rely on raw error text
  reaching the caller; the routes log every outcome with the request id.
- **The worker is unchanged.** It still runs the handler in-process for the
  run-start preflight gate and for Self-Deployed Runtime. **Never delete the
  handler** because "the host serves it now".
- **Hosted-handler rules** (P060, P061 and `gen_app_api.py check` enforce them):
  - the handler module does not log. Other `app/` files it imports also run on
    the worker and may log, through `application_sdk.common.logger.get_logger`;
  - `run_in_thread` comes from `application_sdk.common.concurrency`;
  - never read `APPLICATION_NAME` / `ATLAN_APPLICATION_NAME` in the handler's
    files: on the host they name the host. Use `self.context.app_name`;
  - static manifests only, no `compute_manifest` hook;
  - the handler's `app/` files import each other relatively
    (`from .client import SQLClient`).

## Migration checklist

Work in order; each step ends in a check that must pass. Placeholders: `<sdk>`
is an application-sdk checkout at the commit or release the app pins, `<app>`
the routing name, `<project>` the root `[project].name`.

- [ ] **0. Assess**
- [ ] **1. Refactor to the canonical shape**
- [ ] **2. Add `[tool.atlan-app-api]`**
- [ ] **3. `fix`, then review every removal**
- [ ] **4. Pin both SDK packages to one commit** (only before the SDK release)
- [ ] **5. Prove it locally: `check`, `build`, install alone, probe**
- [ ] **6. Test on a host test image and a tenant**
- [ ] **7. Release, and let the host pin move**
- [ ] **8. Flip `routeToCommonAPIServer` (image before flag)**
- [ ] **9. Delete `server/`** (only after the host pin moved)

### 0. Assess

```bash
atlan-application-sdk-conformance detect --rule P061   # what stands between the app and the host
grep -rnE '\b(ATLAN_)?APPLICATION_NAME\b|compute_manifest|_CREDENTIAL_KEYS|_normalize_credentials|^[[:space:]]*import application_sdk[[:space:]]*$' app/
```

Diff the handler against mysql `app/handler.py` (SQL) or metabase
`app/handler.py` (non-SQL), and list each divergence against the stance table.
Once the block exists, `python3 <sdk>/.github/scripts/gen_app_api.py check`
lists the rest.

**Verify:** every finding and divergence has a remedy from the stance table,
or the work stops at an escalation.

### 1. Refactor to the canonical shape

Apply the stance table's remedies in `app/`, with the app's own tests:
typed `AppError` leaves in a failures module, `self.context.app_name`, static
manifests, `Handler.routers()`, no private-internals patches, no worker/host
detection.

**Verify:** the app's unit tests pass, and the grep in step 0 finds none of
the patterns.

### 2. Add the config block

In the root `pyproject.toml` (mysql #778):

```toml
[tool.atlan-app-api]
handler = "app.handler:MySQLAppHandler"      # must be app.<module>:<Class>, a Handler subclass
data = ["app/sql/test_authentication.sql"]   # non-Python files the handler reads (globs ok)
dependencies = ["aiomysql>=0.3.0"]           # the handler's own third-party deps
extras = ["sql", "aws"]                      # atlan-application-sdk-api extras: sql, aws, pandas, pyatlan
# name = "..."   # only when the k8s Service name is not <project> minus atlan- / -app
```

The routing key (entry-point name, `app.state.app_name`) is `name`, else
`<project>` minus `atlan-` and `-app` (`mysql`). The package is `<project>`
with `-app` → `-api` (`atlan-mysql-api` / `atlan_mysql_api`); `name` does not
change it. Extras per the api package README: `sql` for the SDK's SQL client,
`aws` for the SDK's AWS helpers **or the config and file routes** (S3), `pandas`
for DataFrame results, `pyatlan` for `ConnectionRef`'s Atlas helpers.

**Verify:** the routing key equals the app's Service name; every `data` glob
matches a file.

### 3. `fix`, then review

```bash
python3 <sdk>/.github/scripts/gen_app_api.py fix   # app repo root, Python 3.11+, ruff on PATH
```

In the handler's import closure inside `app/` it rewrites `from app.x import y`
to relative imports, and moves `run_in_thread` (from
`application_sdk.execution.heartbeat`) and `get_logger` (from
`application_sdk.observability.logger_adaptor`) to their host paths. In the
**handler module only** it deletes logging (logger binds, `logger.*`,
`self.context.log_*`, logger-only imports), putting `pass` in an emptied block
and printing each deletion as `file:line`.

It **refuses, changing nothing,** on a log call sharing a line, a logger on an
attribute (`self.logger`), a logger import mixed with other names, an `except`
that would be left as `pass` (raise a typed `AppError` there), or a removed
logger still used. If its edits introduce ruff F401/F841/F821 it restores
every file and fails.

**Review every removal.** Where a log line was the only report of a failure,
raise a typed `AppError` or return a failed check. Remove comments and
variables that existed only for the deleted lines.

**Verify:** unit tests pass (mysql #778 needed no test changes) and
`gen_app_api.py check` reports no P060 line.

### 4. Pin both SDK packages to one commit (before the SDK release)

Needed while the SDK version the app needs is not on PyPI as
`atlan-application-sdk-api` (the package first ships with application-sdk
#4058's release). Check PyPI; if it is there at that version, skip to step 5.
Without a git pin, `build` floors the wheel's `atlan-application-sdk-api`
requirement at the SDK version in `uv.lock`, which cannot install before the
release. Pin both to the **same** rev:

```toml
[tool.uv.sources]
atlan-application-sdk     = { git = "https://github.com/atlanhq/application-sdk.git", rev = "<sha>" }
atlan-application-sdk-api = { git = "https://github.com/atlanhq/application-sdk.git", rev = "<sha>", subdirectory = "packages/api" }
```

Relock against PyPI (`references/validation.md`, "Relock"). The SDK's
connector-integration-tests and sdr-e2e actions, dispatched with an
`application-sdk-ref`, pin both the same way (`repin-application-sdk.sh`).

**Verify:** the `uv.lock` diff changes only the SDK entries, and every
registry source in it is `https://pypi.org/simple`.

### 5. Prove it locally

```bash
python3 <sdk>/.github/scripts/gen_app_api.py check     # → "N app/ files ship as atlan_<app>_api"
python3 <sdk>/.github/scripts/gen_app_api.py build --out /tmp/dist-api
uv venv /tmp/api-only && uv pip install --python /tmp/api-only /tmp/dist-api/*.whl httpx2==2.13.1
/tmp/api-only/bin/python <sdk>/.github/scripts/probe_app_api_member.py --name <app> --package atlan_<app>_api
```

Install the built wheel **alone** (no full SDK); that is what the host has.
The probe list is in `references/validation.md`.

**Verify:** the probe prints `passes the host matrix`. CI runs the same in the
tests-reusable `api-member` job (`check`, then build, install and probe), once
the SDK's `tests-reusable.yaml` on `main` carries it.

### 6. Test on a host test image and a tenant

Follow `references/validation.md`: stage the app's commit through
common-app-server's `hosted-api-apps.overrides.toml` with
`ATLAN_API_APPS_ALLOW_OVERRIDES=1`, run `stage_api_apps.py probe` and the host
tests, then a throwaway pod on an internal tenant.

**Verify:** the tenant test passes end to end: test credentials, test and
update (auth plus `POST /workflows/v1/config/{guid}?type=credentials`),
preflight, metadata, configmaps, `POST /manifest` with the task queue stamped,
and a workflow run that scales the worker through KEDA and publishes assets.

### 7. Release, and let the host pin move

1. After the SDK release: remove both git sources, raise the
   `atlan-application-sdk` floor to that release, relock, rerun step 5.
2. Release the app.
3. **First time only:** a common-app-server PR adds `[apps.<app>]` (`repo`,
   `tag`, `rev` = that tag's full 40-character SHA) to `hosted-api-apps.toml`,
   the `atlan-<app>-api` path dependency, and removes the app's old
   `#subdirectory=server` dependency (`references/routes-and-host.md`).
4. After that, the host **pulls**: Renovate opens the PR moving `tag` and
   `rev` together (7-day release-age cooldown), labelled `no-serving-change`
   when the staged package hash is unchanged. Restage and relock on that PR.
   The SDK pushes nothing to the host.

**Verify:** the overrides file is gone, the host's `release-pins` check is
green, and its image build's `stage_api_apps.py probe` passes.

### 8. Flip the flag (image before flag)

Only when every route the app needs is served (`references/routes-and-host.md`)
and the host pod has its S3 role and `ATLAN_S3_BUCKET` / `ATLAN_S3_REGION`.

1. The host image with the app's pin rolls to the tenant. No traffic moves.
2. On the tenant's host pod: `/server/ready` lists the app with an empty
   `failed` list, `openapi.json` `info.title` for its Host label is the app,
   `/workflows/v1/check` against an unreachable source is 200 with a not-ready
   verdict, and an unknown Host returns `503 app_not_hosted`.
3. Only then, in the app's `atlan.yaml`, set `routeToCommonAPIServer: true`
   under `deploy:`. The app's Service becomes an alias to the host and its own
   server Deployment goes away.

**Verify:** the step 6 tenant test passes against the routed app. Rollback is
in `references/routes-and-host.md`.

### 9. Delete the old `server/` folder

Only after step 7 has merged and the host no longer depends on the app's
`server/` package. Delete `server/` and any CI, Dockerfile lines or
`[tool.uv.sources]` entries that existed only for it. **Keep
`app/handler.py`.**

## Done bar

- [ ] `atlan-application-sdk-conformance detect --rule P061` reports nothing,
      and `gen_app_api.py check` passes (so P060 is clear).
- [ ] No stance-table divergence remains: no private-internals patch, no
      `import application_sdk` detection, no `compute_manifest`, no handler
      logging, no `APPLICATION_NAME` read.
- [ ] No SDK or host change was made for this app alone; any genuine gap was
      escalated.
- [ ] The api-only probe passes locally and in the `api-member` job.
- [ ] Host test image staged the app; `stage_api_apps.py probe` and host
      tests pass; the tenant test in step 6 passed.
- [ ] Both SDK packages are on a release (no git sources); `uv.lock` has only
      pypi.org registry sources.
- [ ] The host pins the app's release tag and SHA; no overrides file.
- [ ] Flag flipped only after the image was live; `server/` deleted only after
      the pin moved; `app/handler.py` kept.
