# v3 Readiness Checklist

Use this page to decide whether an application-sdk-based app is ready to ship on v3.

It complements three existing resources:

- [`upgrade-guide-v3.md`](../upgrade-guide-v3.md) — the canonical v2 → v3 how-to with before/after code.
- [`whats-new-v3.md`](../whats-new-v3.md) — the conceptual rationale for the changes.
- [`tools/migrate_v3/check_migration.py`](../../tools/migrate_v3/check_migration.py) — the static checker that enforces most of §1 automatically.

An app is **v3-ready** when every item below is either ✅ (done) or ➖ (not applicable to this app). A single ❌ blocks the v3 cutover.

---

## 1 — App shape

Each item corresponds to a `FAIL`/`WARN` rule in `tools/migrate_v3/check_migration.py`. Run the checker (see §5) and fix every `FAIL` before continuing.

- [ ] **SDK pinned to `>=3.0.0,<4.0.0`** — `pyproject.toml` must constrain to the v3 major (optionally with extras):
  ```toml
  [project]
  dependencies = [
      "atlan-application-sdk[workflows]>=3.0.0,<4.0.0",
  ]
  ```
  `[tool.uv.sources]` overrides **are allowed** (e.g. to resolve from a git ref or an internal mirror) — the constraint is on the resolved major version, not the source. Verify with `uv tree | grep atlan-application-sdk` and confirm the resolved version is `3.x`.
- [ ] **No deprecated imports** — no `application_sdk.{application,worker,workflows,activities,handlers,services,interceptors,test_utils}` or `application_sdk.clients.{atlan,temporal,workflow}` anywhere (including tests). Rewrite with `python -m tools.migrate_v3.rewrite_imports`.
- [ ] **App subclass** — exactly one class inherits from `App` (or a template: `SqlMetadataExtractor`, `IncrementalSqlMetadataExtractor`, `SqlQueryExtractor`, `BaseMetadataExtractor`). Class-level `name: ClassVar[str]` is set.
- [ ] **`@task` only** — no `@workflow.defn`, `@activity.defn`, `@auto_heartbeater` decorators. No `workflow.execute_activity_method()` calls. No `from temporalio import workflow / activity` in app code.
- [ ] **Typed contracts** — every `@task` parameter and return value is a subclass of `application_sdk.app.Input` / `Output`. No `Dict[str, Any]` / `dict[str, Any]` at contract boundaries. No `allow_unbounded_fields=True`.
- [ ] **Handler signatures** — if a `Handler` subclass exists, `test_auth`, `preflight_check`, and `fetch_metadata` use typed `Input` contracts — not `*args`/`**kwargs`.
- [ ] **Async clients** — no sync `get_client()`. Use `create_async_atlan_client()` with an `AtlanApiToken` credential.
- [ ] **Entry point** — dev uses `run_dev_combined(...)`; containers invoke the `application-sdk --mode combined` CLI (usually via `ATLAN_APP_MODULE`). No `BaseApplication(...)`.
- [ ] **No direct infrastructure** — no direct `DaprClient(...)` or `AsyncDaprClient(...)` construction, no `self._state`, and no raw `loguru`/`logging.getLogger()` in app code. Go through `self.context.*`, the `application_sdk.infrastructure` exports, and `application_sdk.observability.logger_adaptor.get_logger()`.

## 2 — Contract & config

> **In the consuming app repo:** The following checks apply to app repos that use the SDK, not to the SDK repo itself.

The SDK generates workflow/credential/manifest/input artifacts from a single `contract/app.pkl`. See [`.claude/skills/contract/SKILL.md`](../../.claude/skills/contract/SKILL.md).

> **Output directory is `app/generated/`**, importable as `app.generated`. The SDK reads it via `ATLAN_CONTRACT_GENERATED_DIR` (default defined in [`application_sdk/constants.py`](../../application_sdk/constants.py) as `app/generated`). If an app writes generated artifacts elsewhere, either set `ATLAN_CONTRACT_GENERATED_DIR` to match or (preferred) point `poe generate` at `app/generated/` so the runtime finds them without extra config.

- [ ] **`contract/app.pkl`** exists and is the source of truth for the app's metadata, credentials, and workflow form.
- [ ] **Generated artifacts are committed** — `app/generated/{name}.json`, `atlan-connectors-{name}.json`, `manifest.json` and `app/generated/_input.py` are present.
- [ ] **`poe generate` task wired** in `pyproject.toml` and produces no diff on a clean checkout (i.e. generated files are not stale).
- [ ] **`PklProject.deps.json`** lockfile committed.
- [ ] **No stale overrides** — `app/templates/`, `get_configmap()` / `get_manifest()` handler overrides are removed; the SDK auto-serves generated artifacts.

## 3 — Dockerfile & deployment

- [ ] **Base image is `app-runtime-base:3`** — the Dockerfile must use the v3 major tag:
  ```dockerfile
  FROM registry.atlan.com/public/app-runtime-base:3
  ```
  No `*-latest` tags, no dev-branch tags (e.g. `refactor-v3-latest`), no other `app-runtime-base` image. The `:3` major tag tracks the latest v3 patch, matching the `>=3.0.0,<4.0.0` SDK constraint.
- [ ] **Non-root `appuser`** runs the app process.
- [ ] **No secrets in build layers** — credentials resolved at runtime via Dapr / `SecretStore`.
- [ ] **No `ENTRYPOINT` / `CMD` override** — the app Dockerfile inherits the base image's entrypoint (`/usr/local/bin/entrypoint.sh`, which launches `python -m application_sdk.main` and co-runs `daprd` with graceful-shutdown handling). The app only needs `ENV ATLAN_APP_MODULE=<module>:<AppClass>`; the runtime mode (`worker`/`handler`/`combined`) is supplied by Helm via `ATLAN_APP_MODE`.

  ❌ Do not do this — it bypasses daprd co-launch and graceful shutdown:
  ```dockerfile
  CMD ["python", "main.py"]
  ENTRYPOINT ["uv", "run", "application-sdk"]
  ```

  ✅ Correct — inherit everything from the base image:
  ```dockerfile
  FROM registry.atlan.com/public/app-runtime-base:3
  ENV ATLAN_APP_MODULE=app.connector:OpenAPIConnector
  # (no CMD or ENTRYPOINT)
  ```

## 4 — Tests that must pass

The SDK ships test doubles in `application_sdk.testing.mocks` (`MockSecretStore`, `MockStateStore`, `MockPubSub`, …) so unit tests run without Dapr or Temporal.

### Unit tests (must exist; must all pass)

- [ ] **App instantiation** — construct the `App` subclass with `MockSecretStore({...})` + `MockStateStore()` and assert `context.app_name` / `context.run_id` are populated.
- [ ] **Each `@task` method** — at least one happy-path test per `@task`, invoked with a typed `Input` and asserting the typed `Output`. Use `self.task_context.run_in_thread()` only where the real implementation does.
- [ ] **Contract serialization** — round-trip every `Input`/`Output` through `model_dump()` + `model_validate()` to catch unbounded fields and ensure Temporal-safe payloads (<2 MB).
- [ ] **Handler methods** — if the app exposes a `Handler`, cover `test_auth`, `preflight_check`, and `fetch_metadata` with typed inputs and mocked credentials.
- [ ] **Credential registration** — import the app's credentials module and assert `CredentialTypeRegistry().get_class("<type>")` is not None.
- [ ] **Deterministic `run()`** — verify the orchestrator is free of non-determinism (no `datetime.now`, `uuid4`, `random`, direct I/O). This is enforced by Temporal at runtime; a unit test that imports the module under Temporal's sandbox (`WorkflowEnvironment`) catches regressions.

### Integration / E2E tests (must cover every supported scenario)

E2E coverage is **not** limited to the happy path. Every user-observable scenario the app supports must have a dedicated end-to-end test that drives it through `run_dev_combined` (or a full container) and asserts the final artifact / HTTP response — not just intermediate function returns. A scenario is "supported" if it appears in the workflow form, the handler's `test_auth`/`preflight_check` surface, the Dockerfile, or any branch of `run()`.

- [ ] **Boot probe** — start the app via `run_dev_combined` in-process (or as a subprocess) and confirm:
  - `GET /health` returns 200
  - `GET /workflows/v1/manifest` returns the committed `app/generated/manifest.json` verbatim
  - `GET /workflows/v1/configmap/{name}` returns the committed workflow config
- [ ] **Golden contract drift** — re-run `poe generate` in CI and `git diff --exit-code app/generated/ app/generated/_input.py` must be clean.
- [ ] **Scenario matrix** — one E2E test per supported scenario. Each drives `POST /workflows/v1/start` with a realistic payload and asserts the final outcome (NDJSON artifacts, uploaded object-store keys, publish-app state, response codes). Cover at minimum:
  - Happy path for every `import_type` / extraction mode the app exposes (e.g. URL vs CLOUD, full vs incremental, agent vs GUID credentials).
  - Each credential / auth flow the `Handler.test_auth` accepts (success and failure).
  - Each entry in `preflight_check` (success and each failure mode the UI can render).
  - Every explicit branch in `run()` — e.g. `connection_usage=CREATE` vs `REUSE`, `load_to_atlan=True` vs `False`, empty-result short-circuit, retryable vs non-retryable task failures.
  - Error / edge cases the app is expected to handle: invalid spec, empty source, network timeout, missing credential, downstream publish-app failure. Each must produce a deterministic, user-facing outcome (workflow fails cleanly, error propagates, logs include correlation ID).
  - Multi-tenant isolation (if applicable): two tenants running concurrently never see each other's artifacts or state.
- [ ] **Scenario coverage matrix in the repo** — `tests/e2e/README.md` (or equivalent) lists each scenario above and the test that covers it. Reviewers use this table to confirm coverage is complete; missing rows block v3-readiness.
- [ ] **Replay test** — at least one workflow uses `TestWorkflowEnvironment` + a pinned history JSON to prove determinism across SDK upgrades.

## 5 — SDK-triggered validation

App owners and CI run these commands against the app repo. All must exit 0 for the app to be v3-ready.

**One-click runner:** the SDK ships a GitHub Action at [`.github/workflows/v3-readiness-check.yaml`](../../.github/workflows/v3-readiness-check.yaml). Trigger it from the application-sdk repo's Actions tab with two inputs — `app_repo` (e.g. `atlanhq/atlan-openapi-app` or a full GitHub URL) and `branch` — and it runs §5.1 — §5.2 plus the §1 SDK pin and §3 Dockerfile base-image checks against the target ref, writes a summary, and uploads the `check_migration` report as an artifact. Private app repos require an `APP_REPO_READ_TOKEN` repo secret with `contents: read` on the target.

### 5.1 Static check (mandatory)

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <path-to-app-src>
```

- **Exit 0** — all `FAIL` rules pass. `WARN` rules may remain but should be reviewed.
- **Exit 1** — one or more `FAIL`s. Read the report; fix before re-running.
- **Exit 2** — usage/argument error.

Wire this into CI (GitHub Actions job, pre-commit hook, or `poe check-v3`) so regressions are blocked at PR time.

### 5.2 Contract drift check (mandatory)

```bash
uv run poe generate
git diff --exit-code app/generated/ app/generated/_input.py
```

Non-zero exit = generated artifacts are stale relative to `contract/app.pkl`. Regenerate and commit.

### 5.3 Boot probe (recommended)

```bash
# From the app repo root, in a scratch environment:
uv run python run_dev.py &
APP_PID=$!
trap "kill $APP_PID" EXIT

# Poll /health for up to 30s
until curl -sf http://127.0.0.1:8000/health; do sleep 1; done

# Fetch manifest + configmaps and diff against committed artifacts
curl -sf http://127.0.0.1:8000/workflows/v1/manifest | diff - app/generated/manifest.json
```

Mismatches mean the SDK serves something different from what the repo committed — a readiness failure.

### 5.4 Test suite (mandatory)

```bash
uv run pytest tests/unit tests/integration
```

Unit tests must pass without Dapr or Temporal running. Integration tests may spin up the dev Temporal + Dapr stack via `uv run poe start-deps`.

---

## PR sign-off block

Paste this into the description of the PR that declares an app v3-ready. Reviewers check each box.

```markdown
## v3-readiness sign-off (docs/standards/v3-readiness.md)

### App shape (§1)
- [ ] `pyproject.toml` pins `atlan-application-sdk[...]>=3.0.0,<4.0.0`; `uv tree` confirms the resolved version is `3.x` (any source allowed — `[tool.uv.sources]` OK)
- [ ] `python -m tools.migrate_v3.check_migration` reports zero FAILs
- [ ] One `App` subclass, `@task`-only, typed Input/Output at every boundary

### Contract (§2)
- [ ] `contract/app.pkl` + generated artifacts committed
- [ ] `poe generate` produces no diff on a clean checkout

### Deployment (§3)
- [ ] Dockerfile `FROM registry.atlan.com/public/app-runtime-base:3` (v3 major tag)
- [ ] No `CMD`/`ENTRYPOINT` override; `ENV ATLAN_APP_MODULE` set; mode comes from `ATLAN_APP_MODE` at runtime

### Tests (§4)
- [ ] Unit tests cover App instantiation, every `@task`, and contract round-trips
- [ ] E2E scenario matrix documented (`tests/e2e/README.md`) and **every listed scenario has a passing E2E test** — happy path + each branch of `run()` + each `Handler` flow + error/edge cases + multi-tenant isolation
- [ ] Boot probe verifies `/health`, `/workflows/v1/manifest`, and `/workflows/v1/configmap/{name}` match committed artifacts
- [ ] Replay test pinned for every workflow that runs in production

### SDK-triggered checks (§5)
- [ ] `check_migration` exit 0 (CI job)
- [ ] Contract drift check exit 0 (CI job)
- [ ] `pytest tests/unit tests/integration` exit 0 (CI job)
```
