---
name: api-server-consolidation-migration
description: >
  Migrate an Atlan app's API server onto the shared common-app-server (API Server
  Consolidation, ARUN-942) so the app stops running its own always-on server pod
  per tenant. Covers the serving-package carve-out, the get_asgi_app entry point,
  the host's mount contract, packaging, host onboarding, validation on a tenant,
  and the flag flip. Use when onboarding an app to the consolidated host, or when
  a hosted app 503s, returns app_not_hosted, or fails to mount.
---

# Migrating an app onto the consolidated API server

The consolidated host (`common-app-server`) runs **many apps' serving surfaces in
one process per tenant**, replacing one always-on API-server pod per app per
tenant. Requests are routed by the **leading label of the Host header**, so
callers are unchanged.

**Do the whole migration in one pass.** Every phase below has a verification step
that must pass before the next. Skipping verification is how apps reach
production mounted-but-broken, which is worse than not mounted: the pod is
healthy, the route 500s, and nothing says why.

## How to run this

Work the phases in order. Each ends in a check that must pass before the next —
if one fails, fix it there rather than carrying it forward.

- [ ] **0** Audit (identity, `.pth`/env writes, paths, worker coupling, Dapr, root mounts, conformance)
- [ ] **1** Carve out the serving package
- [ ] **2** Write `get_asgi_app()`
- [ ] **3** Packaging — entry point, self-contained wheel, data files
- [ ] **4** Dependencies
- [ ] **5** Invariant tests (no-worker-imports, endpoint parity, and a CI job that proves they ran)
- [ ] **6** Host onboarding — pin, lock, env, secrets
- [ ] **7** Validate locally against the real host router
- [ ] **8** Validate on an internal tenant
- [ ] **9** Flip the flag — **image before flag**

Placeholders used below:

| | |
|---|---|
| `<app>` | the app's routing name, e.g. `my-app` — the entry-point name, the k8s Service name, and the leading Host label. All three must match. |
| `<worker_pkg>` | the existing Python package holding the Temporal worker, e.g. `my_app` |
| `<serving dirs>` | before the carve-out, the directories holding request-handling code — handlers, routes, API modules, and whatever they import. **Exclude `main.py`**: it bootstraps worker and server together and is replaced, not migrated. If you cannot tell, start from the app's handler/server entry and follow its imports. |

## What changes, and what does not

| | |
|---|---|
| **Changes** | The app's serving code is packaged separately and imported by the host. The app's server Service becomes an `ExternalName` alias to the host. |
| **Unchanged** | Heracles stays the proxy for every handler call. Hostnames and endpoints are identical. No client knows the app is consolidated. |
| **Unchanged** | The Temporal **worker** — its deployment, image, task queue and scaling are untouched. Consolidation is serving-only. |
| **Unchanged** | The app's own release flow and CI. An app release never blocks on host CI. |

A host roll is triggered by a change to the app's **server** subtree (tracked as
a monotonic `server_revision` derived from that subtree plus the resolved SDK
entries), not by every app release. Worker-only releases do not roll the host.
App-level tests keep running with `routeToCommonAPIServer: false` — the app's own
pod and image — so the consolidated path is never on a connector's critical path.

The flag **removes the app's server Deployment**. Verified on a tenant: a
host-routed app's Service is an `ExternalName` to the host and its namespace
holds workers only — no `<app>-server` Deployment exists, even with
`splitDeploymentEnabled: true` and `serverReplicaCount: 1` in its applied
values. Two consequences that matter more than they look:

* **A targeted release to one tenant cannot change that tenant's serving
  surface.** The app image it ships runs workers; serving comes from the
  package the *host* pins. The release succeeds and the served behaviour is
  unchanged. To get serving code to one tenant you must either unroute that
  tenant (`routeToCommonAPIServer: false`) or roll the host.
* **The rollback lever is only as real as the app's image.** Flipping the flag
  back re-renders the Deployment, and that image must still be able to serve.
  Keep the app able to serve standalone — see below.

### What you can delete afterwards — and what you must not

The instinct once `server/` is serving traffic is to go delete the app's "old
server code". On an SDK-based connector there usually **isn't any**: the app
never had a hand-written server. `application_sdk` provides the HTTP process and
wraps the app's handler class, so the only thing that looked like a server was
that class — and it is worker-side infrastructure, not serving code.

Before deleting a handler, grep for these three worker-side consumers. Each one
breaks silently, at workflow time, not at import time:

| Consumer | Where | What breaks if you delete |
|---|---|---|
| The SDK **preflight gate** | `AppWorker`, every run, `App.preflight_gate_mode` | Every workflow loses its readiness check |
| The three **SDR workflows** | `sdr:test_auth` / `sdr:preflight_check` / `sdr:fetch_metadata`, registered whenever a handler is passed and `enable_sdr` is true (**the default**) | The agent/SDR path — how the connector form works when the source is inside the customer's network |
| In-workflow **preflight activities** | crawler / miner tasks calling `preflight_verdict` | The run-time block on a `REQUIRED` failure |

So after migrating, the app keeps its handler and the serving package has its own
copy of the preflight logic. That duplication is structural, not laziness: the
two speak different contract types (`application_sdk`'s `PreflightCheck` versus
`server_sdk`'s), which is why the port was needed in the first place. Collapsing
them is an SDK-level change, not a deletion.

What the parity suite in Phase 5.2 is for is exactly this: the two copies drift,
and nothing else notices. Make sure it actually runs (Phase 5.3) — a parity
suite that `importorskip`s itself reports agreement it never measured.

## When NOT to use this

- **Partner apps** — they ship images, not code, so there is no package to
  install. They stay on KEDA HTTP scale-to-zero.
- **Worker-only apps** — nothing to consolidate.
- **Apps whose serving path needs a Dapr sidecar at request time.** The host runs
  plain uvicorn with **no daprd**. See Phase 0.4 — this is a blocker, not a
  nuisance, and it fails at request time rather than at boot.

---

## Phase 0 — Audit first (blocking)

Run all seven. Each one that fails is a fix *before* any code moves. Most
migrations that go wrong went wrong here.

### 0.1 Identity: process-global reads in serving code

**This is the single most common cause of a broken port.** Once several apps
share a process, a process-global value cannot identify any one of them. The host
sets `ATLAN_APPLICATION_NAME` to **its own name** (`common-api-server`).

```bash
grep -rn --include='*.py' \
  -e 'ATLAN_APPLICATION_NAME' -e 'APPLICATION_NAME' -e 'DEPLOYMENT_NAME' \
  -e 'CONTRACT_GENERATED_DIR' -e 'ATLAN_APP_TASK_QUEUE' -e 'TASK_QUEUE' \
  -e 'os.environ' -e 'os.getenv' \
  <serving dirs>
```

Any hit in serving code is a defect. What it costs if missed:

- **Task queue** is derived as `atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}`.
  Under the host that names a queue **no worker polls** — starting a workflow
  reports success and then sits there forever. Silent.
- **App qualified name**, registry identity and own-app routing comparisons all
  shift with it.

**Fix — pin the name, do not read it.** Declare a module constant and have
settings force it, warning when the environment disagrees:

```python
APP_NAME = "my-app"   # the entry-point name, the Service name, the Host label

@model_validator(mode="after")
def pin_application_name(self):
    """The host sets ATLAN_APPLICATION_NAME to its OWN name. Everything derived
    from it — task queue, qualified name, routing — would follow."""
    if self.ATLAN_APPLICATION_NAME != APP_NAME:
        if self.ATLAN_APPLICATION_NAME:
            logger.warning(
                "ATLAN_APPLICATION_NAME=%r ignored: %s pins its application name",
                self.ATLAN_APPLICATION_NAME, APP_NAME,
            )
        self.ATLAN_APPLICATION_NAME = APP_NAME
    return self
```

Declare it **before** any validator that derives a queue or qualified name from
it, so the derivation uses the pinned value.

> Verify against production: the derived queue must equal what the app's worker
> pod actually polls. `kubectl exec <worker-pod> -- env | grep TASK_QUEUE`.

### 0.1b Interpreter-wide side effects — `.pth` files and import-time env writes

A grep of serving code will **not** find this, and it is the most dangerous
version of the identity problem.

A `.pth` file installed into site-packages executes **at interpreter startup**,
in every process that touches the venv, before anything else. Some apps use one
as a pre-SDK seam — e.g. bridging a chart env var into the name the SDK reads:

```python
# startup.pth  — runs for the WHOLE interpreter
import sys; sys.path.insert(0, "/app"); import app.startup
# app/startup.py then does os.environ.setdefault("ATLAN_TASK_QUEUE", ...)
```

Standalone that is fine: one interpreter, one app, it owns the process. On the
host it runs **once for every app in the process**, and whichever app's `.pth`
loads first wins — so a queue, path or flag set for one app silently applies to
all of them. `sys.path.insert(0, "/app")` also shadows the host's own modules.

```bash
find . -name '*.pth' -not -path './.venv/*'
grep -rn --include='*.py' -e 'os.environ\[' -e 'os.environ.setdefault' -e 'sys.path.insert' <serving dirs>
```

**Fix:** a `.pth` must never ship in the serving wheel. Whatever it sets has to
become an explicit argument on the `get_asgi_app()` path instead — a task queue
passed to the SDK factory (Phase 2.2), a path resolved package-relative
(Phase 0.2). Keep the `.pth` in the app's own image if the worker still needs
it; just make sure `[tool.hatch.build...]` does not carry it into the server
package.

Import-time `os.environ[...] = ...` in serving code has the same first-writer-wins
problem without the `.pth`. `setdefault` of a *host-wide* default is the one
acceptable form (see the observability sink in Phase 2) — setting anything
app-specific is not.

### 0.2 CWD-relative paths

The host's working directory is the host's, not the app's. Any path resolved
against the CWD silently points at nothing — assets 404, templates come back
empty, and no error is logged.

```bash
grep -rn --include='*.py' -e 'Path(__file__)' -e '"app/' -e "'app/" \
  -e 'frontend_assets_path' -e 'templates' -e 'static' <serving dirs>
```

If the app ships PKL/generated contracts, the generated directory is resolved
the same way and has the same problem — `ATLAN_CONTRACT_GENERATED_DIR` is
process-global, so under the host it cannot mean two apps' directories at once.
Resolve it per app, package-relative, via a helper rather than the environment.

`application_sdk`'s `create_app_handler_service(frontend_assets_path=...)`
defaults to `"app/generated/frontend/static"` — **CWD-relative**. Always pass an
absolute, package-relative path.

Also check `Path(__file__).parents[N]` arithmetic: moving a module changes N.

### 0.3 Worker coupling

The host installs **only** the serving package. Anything the serving code imports
from the worker package fails — at import time if module-level, at request time
if deferred (harder to find, see Phase 5.1).

```bash
grep -rn --include='*.py' -e 'from <worker_pkg>' -e 'import <worker_pkg>' <serving dirs>
```

### 0.4 Dapr

The host pod runs only uvicorn: **no daprd, no `/app/components`** — but
`DAPR_HTTP_PORT` **is** set in its environment. So a Dapr client will construct
happily and then fail at request time against a port nothing listens on. A
presence check on that env var is not a valid Dapr detector.

```bash
grep -rn --include='*.py' -e 'dapr' -e 'DaprClient' -e 'create_store_from_binding' \
  -e 'get_infrastructure' -e 'set_infrastructure' <serving dirs>
```

If serving code needs object storage, see Phase 2.5.

Note that the `dapr:` block in the app's `atlan.yaml` (`objectstore: true`,
`secretstore: true`, …) describes the app's **own** deployment. It buys nothing
on the host — those components are not rendered there, and every hosted app
today declares them while running without daprd.

If serving code needs Dapr **pub/sub** (the sidecar calling *into* the app), that
is a genuine blocker — those routes cannot work on the host. Keep them on the
app's own deployment and guard the mount (Phase 2.6).

### 0.5 Root-level mounts

A catch-all mount at a sub-app's root swallows method-mismatch handling (405
becomes 404). If the app mounts anything at `""` or `"/"`, check it still returns
405 for a wrong method after the port.

### 0.6 Shared-process conformance (read this even if nothing else)

Your app no longer owns its process. Three app-local bugs become shared outages:

- **Blocking IO in a serving path.** One synchronous call in one handler stalls
  every other app's requests in that process. Standalone this is your own latency
  bug; consolidated it is everyone's outage. Audit `requests`, `time.sleep`,
  blocking DB drivers, and any CPU-heavy work in async handlers — offload with
  `asyncio.to_thread` / `run_in_executor`.
- **Memory.** One process, so there is no per-app limit. A leak in your app OOMs
  the pod and takes every co-tenant app down, and the kill names the pod, not the
  app. Bound what you cache and what you read into memory per request.
- **Import-time cost.** The host's mount phase is bounded by a timeout, and every
  app pays into it. Keep `get_asgi_app()`'s imports deferred (Phase 2) and do no
  network or disk work at module scope.

Also: **every response should carry the serving app's version**, not just
`/manifest`. Drift between the serving code and the app's worker is expected and
allowed; making it *identifiable* is what keeps it safe.

---

## Phase 1 — Carve out the serving package

Target layout — three installable packages:

```
core/    <app>_core      shared domain: settings, contracts, models
server/  <app>_server    the serving surface. THIS is what the host installs
app/     <app>           the Temporal worker. Unchanged behaviour
```

`core/` exists so the worker and the serving surface share domain code without
the host installing the worker. If the app's serving surface shares almost
nothing with the worker, two packages (`server/` + worker) are fine — do not
invent a `core/` with nothing in it.

**Move by import closure, not by intuition.** Compute what the serving entry
point actually reaches at module level, and move that:

```python
"""Module-level import closure from an entry module. Run before moving anything.

Module-level ONLY: a deferred (function-local) import does not run at mount
time, so it cannot break get_asgi_app(). `ast.walk` is wrong here — it counts
function-local imports too and overstates the closure enormously.
"""
import ast, pathlib, collections

ROOTS = {"<worker_pkg>": pathlib.Path("<worker_pkg>")}   # add core/server once they exist

def path_of(mod):
    top, *rest = mod.split(".")
    if top not in ROOTS: return None
    p = ROOTS[top].joinpath(*rest)
    if p.with_suffix(".py").is_file(): return p.with_suffix(".py")
    if (p / "__init__.py").is_file(): return p / "__init__.py"
    return None

def toplevel_imports(path, mod):
    out = []
    def walk(body):
        for n in body:
            if isinstance(n, ast.If):
                if "TYPE_CHECKING" in ast.unparse(n.test):
                    continue                      # never executes
                walk(n.body); walk(n.orelse)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                walk(n.body)                      # Temporal's
                                                  # imports_passed_through() DOES run
            elif isinstance(n, ast.Try):
                walk(n.body); walk(n.orelse)
                for h in n.handlers: walk(h.body)
            elif isinstance(n, ast.Import):
                out.extend(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:                       # relative
                    base = mod.rsplit(".", n.level)[0] + ("." + base if base else "")
                out.append(base)
                out.extend(f"{base}.{a.name}" for a in n.names)
    walk(ast.parse(path.read_text()).body)
    return out

seen, q = set(), ["<serving entry module>"]
while q:
    mod = q.pop()
    if mod in seen: continue
    seen.add(mod)
    p = path_of(mod)
    if p: q.extend(toplevel_imports(p, mod))

print(len(seen), "modules reachable at module level")
print("\n".join(sorted(m for m in seen if path_of(m))))
```

That list is what moves. Anything outside it that the serving code reaches is
reached lazily — find those with Phase 5.1, not this.

Two failure modes seen in real migrations:

- **Package-level rename.** Rewriting `pkg.sub` → `newpkg.sub` also rewrites
  every `pkg.sub.*` child, including children that did **not** move. Only rewrite
  a reference when the **rewritten target actually exists on disk**, and assert
  the package root itself is never in the mapping.
- **Orphans.** Moving `pkg/sub/__init__.py` but leaving `pkg/sub/child.py` makes
  the child unreachable under either name. After moving, assert every remaining
  file's parent package still exists.

**Verify:**

```bash
python -c "import <app>_server, <app>_core; print('ok')"
```

---

## Phase 2 — Write `get_asgi_app()`

This is the whole contract with the host. It is called **once at mount time** and
must return a ready ASGI app.

```python
# server/<app>_server/__init__.py
from __future__ import annotations
import os, pathlib
from typing import Any

# Assert before any application_sdk import: the SDK's observability store sink
# defaults ON and uploads through a Dapr binding, raising in a background task on
# every flush. setdefault, so explicit host config still wins.
#
# Set BOTH names. _STORE_SINK is the primary; _DAPR_SINK is only consulted when
# the primary is unset (constants.py), so setting the DAPR one alone goes inert
# the moment anything sets the primary -- including to its default "true".
os.environ.setdefault("ATLAN_ENABLE_OBSERVABILITY_STORE_SINK", "false")
os.environ.setdefault("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "false")

_ASSETS = pathlib.Path(__file__).resolve().parent / "frontend"


def get_asgi_app() -> Any:
    """Build the ASGI app the host mounts for this app.

    Imports are deferred into the call: the host imports this module for EVERY
    app at discovery, before deciding what to mount, and the mount phase is
    bounded by a timeout.
    """
    from <app>_core.constants import APP_NAME
    from <app>_server._assembly import build_app

    app = build_app(frontend_assets_path=str(_ASSETS))

    # MANDATORY. The host compares this against the entry-point name and ejects
    # the app with 503 missing_app_name if it is unset or mismatched.
    app.state.app_name = APP_NAME

    # Only if the app is served under a path prefix (e.g. Kong strips /myapp).
    # The host runs one uvicorn for every app so it cannot use --root-path; it
    # reads this off the app and applies it per request.
    app.root_path = "/myapp"

    return app
```

**Verify before moving on:**

```bash
python -c "
from <app>_server import get_asgi_app
a = get_asgi_app()
print('app_name =', a.state.app_name)
print('root_path =', a.root_path or '(none)')
print('endpoints =', len(a.openapi()['paths']))
"
```

`app_name` must equal the entry-point name; `endpoints` must match the
standalone server (Phase 5.2).

### 2.1 `app.state.app_name` is not optional

Apps built on `server_sdk.build_asgi_app` get it for free. Apps on
`application_sdk` **do not** — set it explicitly or the host refuses to mount.

### 2.2 Pass identity explicitly to the SDK

For `application_sdk` apps, call the factory with identity passed in, never
inferred:

> `create_app_handler_service` is **deprecated** in favour of
> `server_sdk.build_asgi_app`, and your app will see a B001 conformance warning
> naming it. It still works and is still the right call for an app that has not
> moved its serving surface onto `atlan-application-sdk-server` — which is most
> of them today. Moving is not a rename: `build_asgi_app` registers the handler
> routes only, so an app relying on the wider route set has real work to do
> first. Keep using the factory until then.

```python
from application_sdk.handler.service import create_app_handler_service

app = create_app_handler_service(
    handler=my_handler,
    app_name=APP_NAME,                      # never the env
    task_queue=settings.ATLAN_APP_TASK_QUEUE,  # derived from the PINNED name
    frontend_assets_path=str(_ASSETS),      # absolute, not CWD-relative
    storage=<store>,                        # see 2.5
)
```

### 2.3 Custom FastAPI routes are fully supported

The app is mounted as a whole ASGI sub-application: its routes, middleware,
lifespan and exception handlers carry across as-is. Nothing needs splitting out.

**But if the app injects routes by patching an SDK class**, that patch must still
run on the `get_asgi_app()` path. A patch applied by the SDK's own boot path
(`main.py`, `run_dev_combined`) does **not** run here — the host never imports
`main.py`. Either install the patch inside `get_asgi_app()` before building, or
better, include the routers explicitly:

```python
app = create_app_handler_service(...)
for router in _routers():
    app.include_router(router)
```

### 2.4 Startup work: the host runs lifespans, not `main()`

The host **does** enter every hosted app's lifespan. It does **not** run the
app's `main.py`. Anything `main()` does before uvicorn binds — connecting
Temporal, running migrations, seeding, wiring storage — must move into the
lifespan:

```python
import contextlib

def wrap_lifespan(app: Any) -> None:
    """Run our startup ahead of the app's own lifespan — ahead of, because the
    app's lifespan may read what startup creates (e.g. a workflow client)."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scoped_app: Any):
        await run_startup(scoped_app)
        async with inner(scoped_app):
            yield

    app.router.lifespan_context = lifespan
```

Keep `main()`'s fatal/non-fatal split exactly: what was fatal there stays fatal
here (the host ejects that app and serves the rest), what was logged-and-survived
stays logged-and-survived.

If migrations or seed data live in the worker package, **move them to the serving
package** — they are server-side startup concerns and the host does not install
the worker.

### 2.5 Object storage without Dapr

If serving code reads or writes object storage, build the store from the chart's
own env and install it into the SDK infrastructure context — then every existing
`application_sdk.storage` call site keeps working untouched.

The chart provides, per cloud:

| cloud | env |
|---|---|
| AWS | `S3_BUCKET`, `S3_REGION` (ambient IRSA/instance identity — pass **no** key material) |
| Azure | `AZURE_STORAGE_CONTAINER`, `AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_ACCESS_KEY` |
| GCP | `GCP_BUCKET`, `GCP_REGION`, `GCP_HMAC_ACCESS_KEY`, `GCP_HMAC_SECRET` |

GCP uses the **S3-compatible endpoint** (`https://storage.googleapis.com`) with
the HMAC keys — obstore's native GCS path cannot use them.

```python
from application_sdk.storage._obstore_config import make_s3_store, make_azure_store
from application_sdk.infrastructure import get_infrastructure, set_infrastructure
from application_sdk.infrastructure.context import InfrastructureContext

def bootstrap_storage() -> None:
    if get_infrastructure() is not None:
        return                       # standalone already wired its own
    store = _build_from_env()
    if store is None:
        raise RuntimeError("No object store configured: set S3_BUCKET, ...")
    set_infrastructure(InfrastructureContext(storage=store, _dapr_client=None))
```

**Make a missing configuration fatal.** A local-filesystem fallback is worse: the
app comes up healthy and writes state to pod-local disk no other replica can
read. Lost data found later beats a pod that refuses to start — so refuse.

### 2.6 Guard anything the host genuinely cannot run

```python
try:
    from <worker_pkg>.events.routes import router
except ModuleNotFoundError:
    logger.info("Worker package absent (consolidated host): event routes not mounted.")
else:
    app.include_router(router)
```

---

## Phase 3 — Packaging

The host installs the serving package **from a git rev with
`#subdirectory=server`**. The wheel must therefore be self-contained.

### 3.1 Register the entry point

```toml
[project.entry-points."atlan.app_server"]
my-app = "<app>_server:get_asgi_app"
```

The entry-point **name is the routing key**. It must equal the k8s Service name
and the leading label of the Host header. A mismatch with `app.state.app_name` is
an ejected app.

### 3.2 `core/` must not be a runtime dependency

An unpublished sibling directory can never be resolved by the host. Make it
dev-only and **vendor the code into release wheels**:

```toml
[dependency-groups]
dev = ["<app>-core"]

[tool.uv.sources]
<app>-core = { path = "../core", editable = true }
```

```python
# server/hatch_build.py
class VendorCoreHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return          # editable must NOT vendor: the copy would shadow
                            # the sibling checkout. Only a hook can tell the
                            # two build targets apart.
        build_data.setdefault("force_include", {})["../core/<app>_core"] = "<app>_core"
```

Because the wheel carries core's *code*, the server package must declare **core's
runtime dependencies** too — a vendored copy brings none of its own.

### 3.3 Data files must follow their loaders

Templates, generated contracts, static assets, migration scripts — if serving
code reads it, it must be in the wheel:

```toml
[tool.hatch.build.targets.wheel.force-include]
"../docs" = "<app>_server/docs"
```

**Never force-include a path that is only sometimes present.** Gitignored build
output (a JS bundle) exists after the image's Node stage and nowhere else; a
static entry fails every build that does not run Node — CI, and any `uv sync`
from git. Add it conditionally in the build hook instead:

```python
built = repo / "app" / "frontend" / "output"
if built.is_dir():
    build_data.setdefault("force_include", {})[str(built)] = "<app>_server/frontend"
```

> ⚠️ **Open fleet issue.** A wheel built by the host from a git rev has **no
> Node**, so a frontend built only in the app's image never reaches the host.
> If the app serves a UI, resolve distribution before flipping the flag —
> publishing a built wheel from the app's CI is the likely answer.

### 3.4 Verify the wheel is self-contained

```bash
uv build --wheel server -o dist
python - <<'PY'
import zipfile, collections
n = zipfile.ZipFile("dist/<wheel>").namelist()
print(collections.Counter(x.split("/")[0] for x in n))
PY
```

Expect the server package, the vendored core, every data directory, and
`entry_points.txt`.
### 3.5 Keep the app able to serve standalone

The flag removes the app's server Deployment, so the only way back is to
re-render it — and that image must still be able to serve. The fleet is mixed
for as long as consolidation is progressive, so **the app's own image should
serve from the same `server/` package the host installs**, not from a second
copy that drifts.

Install the serving package into the app's image as an extra, and mount it from
`main.py` in standalone SERVER mode:

```toml
# app pyproject.toml
[project.optional-dependencies]
serving = ["atlan-<app>-server"]

[tool.uv.sources]
atlan-<app>-server = { path = "server", editable = true }
```

```dockerfile
# The serving extra is installed because the fleet is mixed during progressive
# consolidation: tenants not yet routed to common-api-server still run this
# image in standalone SERVER mode.
COPY --chown=appuser:appuser server ./server
RUN uv sync --locked --no-install-project --extra serving
```

Two traps:

* **The extra may pull a private dep.** The serving SDK itself no longer is one:
  `atlan-application-sdk-server` lives in the PUBLIC `atlanhq/application-sdk`
  (`#subdirectory=packages/server`), so it needs no credential. Keep the token
  plumbing only if the app pulls some *other* private atlanhq dep — and if it does,
  pass it through per-process `GIT_CONFIG_*` env from a BuildKit secret, never into
  a layer.
* **Do not put the serving package in the app's default dependency set.** The
  worker image must not carry the serving surface, and a plain dependency also
  makes every dep-resolving CI job need that credential (Phase 5.3).

**Verify** by running the app's own image in SERVER mode and hitting one real
route — not just `/health`, which answers before any app code is mounted.


---

## Phase 4 — Dependencies

- **Declare nothing you do not import.** An unused private git dependency drags a
  credentialed clone into every CI job that syncs the package. Check with
  `grep -rn 'import <dep>' core server` before declaring it.
- **Carry your drivers.** The serving package runs migrations and serves DB-backed
  routes, so the DB driver (e.g. `psycopg2-binary`) and migration runner (e.g.
  `alembic`) belong here — not only in the app's extras.
- **`requires-python`** must be satisfiable alongside every other hosted app and
  the host itself. PEP 695 generics (`class Foo[T]`) require ≥3.12.
- **Narrow extras**, but measure rather than assume. Check what an extra actually
  pulls, and check it against the host's existing lock — a heavy package already
  present via another app costs nothing.

### 4.1 Bumping the serving SDK — it is a lockstep, not a per-app change

Every serving package pins `atlan-application-sdk-server` by git rev as a PEP 508
direct reference, and the host resolves all of them **in one resolution**. Two apps
on different revs is a hard `uv` error, not a warning — the host simply stops
locking, and it fails for whoever bumps next rather than for whoever drifted.

Note the import path is still `server_sdk`; only the *distribution* was renamed
when it moved out of the standalone repo into `application-sdk/packages/server`.
That is why a re-point is a one-line dependency change per app and nothing else.

Worse than a rev mismatch: a *name* mismatch. Both distributions ship the same
top-level `server_sdk` module, so a host with one app on the old
`atlan-server-sdk` and another on `atlan-application-sdk-server` installs both and
silently keeps whichever resolved last. uv cannot catch that one. Move every
hosted app together.

So a bump is a coordinated change across every hosted app:

1. Pick the new rev. Bump it in **every** hosted app's `server/pyproject.toml`.
2. Each file pins it **twice** — in `dependencies` and again in the `[workflow]`
   extra. Change both; a half-bump resolves locally and breaks on the host.
3. Re-lock the host (`UV_NO_CONFIG=1 uv lock --default-index https://pypi.org/simple`).
4. Keep the app's own sibling pins on that rev too, where the app declares one.

**Read the revs from the exact revisions the host pins, not your checkouts.**

```bash
# right: the branch/SHA the host actually consumes
git -C <repo> show <origin/ref>:server/pyproject.toml | grep "application-sdk.git@"
```

A stale local clone shows a mismatch that does not exist. This has already
caused one near-miss: a two-week-old working copy made a correctly-aligned fleet
look broken, and the "fix" would have downgraded a healthy pin. Assert the
occurrence count before rewriting anything — if the count is not what you
expect, your checkout is wrong, not the fleet.

### 4.2 New apps drag new PyPI packages through the cooldown gate

Pinning a connector pulls its driver stack into the host lock (`pymysql`,
`psycopg`, `snowflake-connector-python`, …). Any of those published in the last
7 days trips the release-age rule and CI's `dep-cooldown`. Cap them —
never bypass:

```toml
constraint-dependencies = [
    "pymysql<1.2.1",                    # 1.2.1-1.2.3 all shipped 2026-09-17
    "snowflake-connector-python<4.7.4", # shipped 2026-09-16
]
```

Record the date and the reason in the comment, so the cap can be lifted rather
than inherited forever.

---

## Phase 5 — The invariant tests

Add both. They are what stops a future change silently re-breaking the port.

### 5.1 No worker imports — the deferred-import trap

Module-level analysis is **not enough**. The import that reaches a module is
often deferred inside a FastAPI dependency, and the module it loads then imports
the worker at *its* module level — invisible until a request touches that path,
and possibly only on one registry/config backend.

Sweep **every** module in the carved packages, in a subprocess with the worker
blocked at the import hook (the worker is importable in the app's own repo, so a
same-process check proves nothing):

```python
class _NoWorker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "<worker_pkg>" or fullname.startswith("<worker_pkg>."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None

sys.meta_path.insert(0, _NoWorker())
# then importlib.import_module() every module under core/ and server/
```

**Verify the test by breaking it**: reintroduce one worker import and confirm it
fails, naming the module. A test you have not seen fail proves nothing.

### 5.2 Endpoint parity

Assert the hosted app exposes exactly what the standalone server does. The
strongest baseline is the **live** standalone server's own `/openapi.json`:

```bash
kubectl exec -n <app>-app <server-pod> -- /app/.venv/bin/python -c \
  "import urllib.request,json;print('\n'.join(sorted(json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json'))['paths'])))"
```

Diff as sets against `get_asgi_app().openapi()["paths"]`.

### 5.3 Make the suites actually run — the silent-pass trap

Both suites above live under `server/`, and `server/` is deliberately **not** in
the app's dependency graph — it must never reach the worker image — so the app's
normal test job never collects them. A parity suite also has to guard its worker imports, because the
consolidated image installs only the serving package:

```python
worker_handler = pytest.importorskip("<worker_pkg>.handler")
```

Put those two facts together and the suite skips itself and reports a pass. That
is not hypothetical — it is how a connector's serving preflight drifted a whole
refactor behind its worker without one red build.

Give `server/` its own CI job, and make the job **prove the suite ran** before
running it:

```yaml
- name: Authorize private atlanhq git deps
  # actions/checkout scopes its token to this repo's own .git via includeIf,
  # so uv's separate clone cache cannot use it.
  env:
    GIT_TOKEN: ${{ secrets.ORG_PAT_GITHUB }}
  run: |
    git config --global url."https://x-access-token:${GIT_TOKEN}@github.com/atlanhq/".insteadOf "https://github.com/atlanhq/"

- name: Install the app, then the serving package on top
  run: |
    uv sync --all-groups
    uv pip install -e ./server

- name: Prove both sides are importable
  run: |
    uv run --no-sync python -c "
    import importlib
    for mod in ('<worker_pkg>.<preflight module>', '<app>_server.<preflight module>'):
        importlib.import_module(mod)"

- name: Run server tests
  run: uv run --no-sync python -m pytest server/tests -q
```

Do **not** close this gap by adding the serving package to the app's dependency
groups instead. Two things break, and CI is where you find out: the Dockerfile's
`uv sync` starts building the serving package into the *worker* image, and every
job that resolves dependencies suddenly needs a credential for the private SDK
repo that it never needed before.

---

## Phase 6 — Host onboarding

In `common-app-server`:

1. **Pin the app** in `pyproject.toml`:
   ```toml
   "<app>-server @ git+https://github.com/atlanhq/<repo>.git@<immutable-sha>#subdirectory=server",
   ```
2. **Re-lock.** ⚠️ If your machine has a managed `~/.config/uv/uv.toml` (Endor
   firewall), a plain `uv lock` rewrites **every** package's source to the
   credentialed proxy — which CI cannot authenticate to. Always:
   ```bash
   UV_NO_CONFIG=1 uv lock --default-index https://pypi.org/simple
   ```
   Then confirm the diff is only your app, and that sources still say
   `pypi.org`.
3. **Raise `requires-python`** if the app needs it.
4. **Host env.** The app's config must exist in the host pod. App-prefixed vars
   (`ATLAN_MYAPP_*`) cannot collide, so they can be injected plainly.
5. **Host secrets.** ⚠️ Kubernetes secrets are **namespace-scoped**. Secrets the
   app references live in `<app>-app`; the host runs in `common-api-server-app`.
   Any secret not already there must be provisioned into the host's namespace by
   a chart change. Check before you flip:
   ```bash
   kubectl get secret <name> -n common-api-server-app
   ```

---

## Phase 7 — Validate locally

Resolve all hosted apps together and drive the real host router. This catches
resolution conflicts and cross-app interference before any cluster.

```python
import asyncio, sys
sys.path.insert(0, "<path to common-app-server checkout>")
from common_api_server.main import build_common_app

async def request(app, path, host):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "scheme": "http",
             "headers": [(b"host", host.encode())],
             "client": ("1.2.3.4", 1), "server": ("h", 80)}
    status, body = None, b""
    async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
    async def send(m):
        nonlocal status, body
        if m["type"] == "http.response.start": status = m["status"]
        elif m["type"] == "http.response.body": body += m.get("body", b"")
    await app(scope, receive, send)
    return status, body

async def main():
    router = build_common_app()
    started, msgs = asyncio.Event(), []
    async def receive():
        if not started.is_set():
            started.set(); return {"type": "lifespan.startup"}
        await asyncio.sleep(3600)
    task = asyncio.create_task(
        router({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, msgs.append))
    while not msgs:
        await asyncio.sleep(0.1)
    print("lifespan:", msgs[0])          # must be lifespan.startup.complete

    host = "<app>.<app>-app.svc.cluster.local"
    for path in ("/server/health", "/api/v1/..."):
        print(path, *await request(router, path, host))
    task.cancel()

asyncio.run(main())
```

Expect `Hosting N of N app server(s)`. Anything less means an app was ejected —
the log names it and why.

⚠️ **Make the harness faithful.** If the worker package is importable (e.g. you
ran from the app's repo, putting CWD on `sys.path`), the harness masks exactly
the bugs Phase 5.1 exists to catch. Verify:
`python -c "import importlib.util as u; print(bool(u.find_spec('<worker_pkg>')))"`
→ must be `False`.

Also exercise the app's **real config backend**. A registry/config type that
differs from production (e.g. `sql` locally vs `atlan` in tenants) takes a
different code path and will pass while production 500s.

---

## Phase 8 — Validate on an internal tenant

Do **not** patch the live `common-api-server` Deployment — it serves other apps
on that tenant. Run a throwaway pod instead:

- namespace: the **app's own** namespace, where its secrets already exist
- image: a consolidated image built with your pin (build-only, on a non-channel
  branch, `publish=false` — no marketplace release)
- env: copy the app server Deployment's env **spec** (secret *references*, never
  values), then override `ATLAN_APPLICATION_NAME=common-api-server` so the pod
  reproduces the host condition and exercises the identity pin
- command: the host's uvicorn entry point

Then verify, in order:

```
Hosting N of N app server(s): ...
Object store: S3 (<tenant bucket>)      # if applicable
Application startup complete
```

and probe **every** parameterless GET endpoint through the host by Host header,
diffing status **and body shape** against the live standalone server. Status
codes alone hide a route that returns 200 with empty data.

Finally, confirm the log is clean:

```bash
kubectl logs <pod> | grep -c "No module named '<worker_pkg>'"   # must be 0
```

Delete the pod when done and confirm the tenant is as you found it.

---

## Phase 9 — Flip the flag

**Order matters — image before flag.** The host must already be serving the app
before the flag routes traffic to it, or callers get `app_not_hosted` 503s.

1. Land the host pin; let the consolidated image build and roll to the tenant.
   **This step ships on its own.** Pinning only mounts the app — no traffic moves
   until the flag flips — so land it, watch it, and let it settle before going
   near the flag. Doing both at once means a failure has two possible causes.
2. Confirm on the tenant's real host pod. `Hosting N of N` in the logs is
   necessary and not sufficient; an app can mount and still be broken. Check:
   - `/server/ready` reports `N/N` with an **empty `failed` list**;
   - each app answers **on its own Host label with its own identity** — fetch
     `/openapi.json` per app and assert `info.title` matches the app that
     Host selected. This is the cross-talk check, and it is the one that proves
     routing rather than merely liveness;
   - the app's real surface responds: for a connector, `/workflows/v1/check`
     against an unreachable source must return **200 with a `not_ready`
     preflight, never a 500**;
   - an unknown hostname still gets `503 app_not_hosted`, not the pod's health.
3. Only then, in the app's `atlan.yaml`:
   ```yaml
   deploy:
     routeToCommonAPIServer: true
   ```

This aliases the app's Service to `common-api-server.common-api-server-app.svc.cluster.local`
and removes the app's server Deployment. The worker is untouched — including its
handler, which still backs the preflight gate and the SDR workflows (see "What
you can delete afterwards").

### Rollback

- **One bad app:** set `routeToCommonAPIServer: false` — back on its own pod,
  per-app and per-tenant, no host release needed. This is the fast lever, and it
  only works if that app's image can still serve (Phase 3.5). The Deployment is
  re-rendered on the next reconcile, so this is not instantaneous.
- **Bad app code:** lower that app's pin in the host and roll forward. Only that
  app's code moves; every other app keeps its pin.
- **Bad host:** revert the host image.

---

## Failure catalogue

| Symptom | Cause | Fix |
|---|---|---|
| `503 missing_app_name` | `app.state.app_name` unset | Phase 2.1 |
| `503` + `entry point 'x' vs app_name 'y'` | Entry-point name ≠ declared name | Phase 3.1 |
| `503 factory_error` | `get_asgi_app()` raised | Read the host log; usually a missing dep or a CWD path |
| `503 lifespan_error` | Startup raised | Phase 2.4; check DB/Temporal/storage reachability from the host pod |
| `app_not_hosted` 503 | Flag flipped before the image served the app | Phase 9 ordering |
| Routes 500, `No module named '<worker_pkg>'` | Deferred worker import on a serving path | Phase 5.1 |
| Workflow starts, never runs | Task queue derived from the host's name | Phase 0.1 |
| Assets/templates 404, no error | CWD-relative path | Phase 0.2 |
| `StorageBindingNotFoundError` | Dapr object store on the host | Phase 2.5 |
| Background upload errors on every flush | SDK observability store sink defaults on | Phase 2, `ATLAN_ENABLE_OBSERVABILITY_STORE_SINK=false` (set the `_DAPR_SINK` name too; it is only the fallback) |
| 405 becomes 404 | Catch-all root mount | Phase 0.5 |
| One app's queue/flag applied to another | `.pth` or import-time env write shipped in the wheel | Phase 0.1b |
| Host resolve fails on `requires-python` | App floor above the host's | Phase 4 |
| CI cannot clone a dependency | Unused/private dep declared | Phase 4 |

## Host behaviour worth knowing

- **Degraded mount.** One app failing to mount is ejected; the rest keep serving.
  The pod goes *ready degraded* — readiness is "routing table built and ≥1 app
  mounted", with `apps_mounted` / `app_mount_failed` exported. So a broken app is
  a **silent per-app outage** unless someone alerts on that gap.
- **`X-Original-Host` wins over `Host`** (the KEDA interceptor path).
- **Lifespans are forwarded** to every mounted app.
- **SDK lockstep:** apps built on `atlan-application-sdk-server` must pin the
  *same* rev; a mismatch is a hard `uv` error, and a mix of the old and new
  distribution names is worse — both ship `server_sdk`, so the host silently keeps
  whichever installed last. An app that does not import `server_sdk` should not
  declare it.
