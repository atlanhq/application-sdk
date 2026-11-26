# ADR-0023: Consolidated API Host for Hosted Apps' Handlers

## Status
**Accepted** — supersedes in part [ADR-0001](0001-per-app-handlers.md). Records the
architecture that shipped in application-sdk PR #4058; the mechanics live in
[The api distribution](../standards/api-distribution.md).

## Context

[ADR-0001](0001-per-app-handlers.md) chose one handler service per app and rejected a
shared "uber-handler" for five reasons: blast radius, coupled deployments, dependency
conflicts, every app's credentials in one process, and no per-app scaling.

Since then the cost side of that trade-off grew. Every hosted app runs a handler
service of its own, mostly idle, to answer a handful of short requests (auth,
check, metadata, config, manifest) for the UI and Heracles. Each one carries the full
worker SDK (Temporal, Dapr, the object store) though a handler uses none of it.

The consolidated API host serves those requests for many apps from one process. It
installs `atlan-application-sdk-api`, the listed handler-side subset of
`application_sdk`, plus one small generated package per app. That reverses ADR-0001
for the apps it serves, so the decision and its mitigations are recorded here.

## Decision

**Hosted apps' handler routes are served by one consolidated API host.** ADR-0001
still holds for everything outside that scope.

### Scope

- **In:** apps running on Atlan's hosted platform that opt in. An app opts in with a
  `[tool.atlan-app-api]` block in its `pyproject.toml` (see the
  `api-server-consolidation-migration` skill); once its `routeToCommonAPIServer` flag
  is set, every Heracles handler call for it lands on the
  host.
- **Out:**
    - Apps that have not opted in. They keep their own handler service, as ADR-0001
      describes.
    - Workers. Per-app workers ([ADR-0002](0002-per-app-workers.md)) and separate
      handler and worker deployments ([ADR-0009](0009-separate-handler-worker-deployments.md))
      are unchanged. The worker still registers the same route modules, so an app
      answers alike on either surface.
    - Self-Deployed Runtime. Nothing in an SDR runtime on customer infrastructure
      moves onto the host. The one cross-over is the task queue the host stamps into a
      served manifest; Heracles re-stamps agent-side nodes with the SDR queue (see
      [Cross-repo contracts](../standards/cross-repo-contracts.md)).
- **Code ownership is still per app.** Each app writes its own `Handler` subclass in
  its own repo, released on its own version. Only the process that serves it is
  shared.

### How ADR-0001's objections are answered

| ADR-0001 objection | Position on the host |
| --- | --- |
| **Security: all credentials in one process** | Accepted as a trade-off, bounded by a rule: only **trusted first-party** apps are hosted. There is no secret isolation between hosted apps: every app's environment shares one `os.environ`. The host does not authenticate callers; a NetworkPolicy (owned by App Runtime) limits who can reach the pod to Heracles and the ingress. The SDK's own routes pass response-body text through `redact_secrets`, so a credential in exception text is not echoed back; an app's own routers (`Handler.routers()`) are mounted as written and get no such pass (see below). Config keys use the mounted app's name, never request input. |
| **Blast radius: one app's bug affects all** | Reduced, not removed. Each app gets its own `run_in_thread` / `submit_in_thread` pool (`api-<app>-`), so one app's hung blocking calls cannot take another app's thread slots ([ADR-0010](0010-async-first-blocking-code.md), FND-2973 addendum). Uploads land only under the app's own `persistent-artifacts/apps/<app>/uploads/`. The event loop, FastAPI's thread pool and the host's request concurrency limit stay shared: an app that blocks the event loop or leaks memory still affects every hosted app. |
| **Dependency conflicts** | One resolved environment, checked before it ships. All hosted apps share one `atlan-application-sdk-api` version and one version of every third-party package. Each app declares only its handler's own dependencies and api extras. The host pins each app by release tag plus commit SHA. Its lock fails in CI when the apps cannot be resolved together, and every staged app is probed in the locked environment before a pin moves. An app whose dependencies conflict cannot move to the new pin until the conflict is fixed. |
| **Coupled deployments** | Accepted. An app's handler change reaches the host only through a host pin bump (Renovate opens the PR), which rebuilds and redeploys the one host that serves every app. The SDK's release workflow does not push to the host. |
| **No per-app scaling** | Accepted. The host scales as one unit. Handler traffic is short, synchronous and low-volume, so per-app scaling bought little. |

### Rules that keep hosted handlers safe to co-locate

- Handler code imports only the api distribution's listed modules; `check_api_surface.py`
  and `gen_app_api.py check` enforce this in CI.
- Handler code does not log. It reports through its return value or a typed `AppError`;
  the shared routes log each outcome with the request id (conformance P060).
- A handler reads its app's name from `self.context.app_name`, never from
  `APPLICATION_NAME` / `ATLAN_APPLICATION_NAME`. On the host those name the host itself.
- An app's own routers (`Handler.routers()`) are served on the host too, mounted with
  `include_router` after the SDK's routes. The SDK does not redact their responses, so
  an app route sanitizes its own output: it never returns exception text, a DSN or a
  credential verbatim, and answers a failure with a fixed
  message.

## Options Considered

### Option 1: Keep one handler service per app (ADR-0001)

**Rejected for hosted apps.** It keeps the strongest isolation. The cost is an idle
service per app carrying the full worker SDK.

### Option 2: Consolidated host with per-app packages (Chosen)

One process, many apps, each mounted from its own generated package. The cost is the
shared trust boundary and shared capacity described above. In exchange: one image and
one environment for every opted-in app, with no worker runtime in it (the api-only
core install is measured at 62 MB).

### Option 3: Consolidated host with per-app subprocesses

**Not chosen.** Running each app's handler in its own process would restore secret
and memory isolation. It would also bring back most of the per-app baseline cost the
host exists to remove. It stays open if an untrusted or third-party app ever needs
hosting; the trusted-first-party rule exists until then.

## Consequences

**Positive:**
- One handler process and image for every opted-in app, not one per app.
- Handler code is smaller and checked: no worker runtime, no logging, an import
  surface enforced in CI.

**Negative:**
- Every hosted app can read every other hosted app's environment. Hosting a
  third-party or untrusted app requires a new decision.
- A handler that blocks the event loop, leaks memory or crashes the process affects
  every hosted app.
- All hosted apps move together on one dependency set. One app's dependency conflict
  blocks it from moving to a new host pin.
- An app has two possible handler surfaces (host or its own service), and the routes
  must answer alike on both.

## References

- [The api distribution](../standards/api-distribution.md): build, routes, trust model,
  versions and release.
- [ADR-0001](0001-per-app-handlers.md): the decision this supersedes in part.
- [ADR-0010](0010-async-first-blocking-code.md): per-app thread pools on the host.
- `.claude/skills/api-server-consolidation-migration/SKILL.md`: onboarding an app.
