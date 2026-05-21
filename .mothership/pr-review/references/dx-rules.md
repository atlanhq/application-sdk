# v3 Developer Experience (DX) Review Rules

Review from the perspective of a developer USING the SDK to build connectors.
Every change is evaluated for how it affects the developer's ability to discover, learn, use, debug, and upgrade.

---

## 1. API Ergonomics

### Intuitive Parameter Names (Important)

Public API parameters must be self-describing. A connector developer reading a call site should understand what each argument does without checking the docstring.

```python
# BAD -- cryptic abbreviations
@task(t=300, r=3, hb=60)
async def fetch(self, input: FetchInput) -> FetchOutput: ...

# GOOD -- readable, consistent with existing SDK patterns
@task(timeout_seconds=300, retry_max_attempts=3, heartbeat_timeout_seconds=60)
async def fetch(self, input: FetchInput) -> FetchOutput: ...
```

Flag:
- Single-letter or ambiguous parameter names on public APIs
- Boolean parameters without a verb prefix (`active` vs `is_active`, `skip` vs `should_skip`)
- Parameter names that diverge from established SDK conventions (e.g. `timeout` instead of `timeout_seconds`)

### Maximum 5 Required Parameters (Important)

Public constructors and functions should have at most 5 required (no-default) parameters. Beyond that, group into a config/options dataclass or use keyword-only arguments with defaults.

Flag:
- Public API methods or constructors with more than 5 required parameters
- `**kwargs` on public APIs without a typed alternative (hides the contract)

### Consistency with Existing Patterns (Critical)

New APIs must follow established SDK idioms. Connector developers build muscle memory around these patterns:

- `@task` decorator for units of work
- Single `Input` subclass in, single `Output` subclass out
- `App` subclassing with `run()` or `@entrypoint` as the orchestrator
- `self.logger`, `self.now()`, `self.uuid()` for determinism
- `self.task_context.run_in_thread()` for blocking ops

Flag:
- New orchestration methods that bypass `run()` / `@entrypoint`
- New decorator that duplicates `@task` behavior under a different name
- Returning raw `dict` or `tuple` instead of typed `Output` subclass
- Using `datetime.now()` or `uuid.uuid4()` instead of `self.now()` / `self.uuid()`

### Public surface should expose one recommended variant (Important)

New public factories, builders, and helpers should default to ONE
recommended variant per concept. Multiple parallel variants (sync vs
async, v8 vs v9, legacy vs new) on a single helper return point
fragment the API surface and force every connector developer to
re-derive "which one should I use?"

**Flag any new code that:**
- Adds a factory / builder / helper that returns one of several
  client/object variants based on a parameter, where the SDK's
  recommended path is a specific variant. The factory should default
  to (or only return) that variant; other variants should be reached
  via a separate documented escape hatch with a deprecation/future-
  removal note.
- Returns `object`, `Any`, `Union[A, B]`, or a `# type: ignore`-d
  type from a public factory or accessor. The return type MUST be
  the concrete typed class connectors will actually consume.
- Adds a kwarg to a public function whose values map to qualitatively
  different return shapes — splitting the function into two narrowly-
  typed alternatives is almost always clearer.

**Heuristic:** if the PR description has to explain "we support all
four variants because…", the surface is too broad — push back with
"which variant should the typical connector use? Make that the only
default; relegate the rest behind a separate, narrowly-documented
helper."

**Severity:** Important (DX). Not blocking, but should be flagged
loudly with a suggested narrower signature.

---

## 2. Error Messages and Debugging

### Actionable Error Messages (Critical)

Every user-facing exception must tell the developer: (1) what went wrong, (2) why it matters, and (3) how to fix it. Use the most specific categorical leaf class from `application_sdk.errors`.

```python
# BAD -- opaque, wrong base class
raise AppError("Invalid configuration")

# GOOD -- typed leaf, what + why + fix, optional suggested_action
from application_sdk.errors import AuthError
raise AuthError(
    message="Credential not found for connection 'snowflake-prod'. "
            "Ensure the credential is provisioned in the Atlan UI under "
            "Settings > Credentials before running this workflow.",
    auth_method="credential_guid",
)
```

Flag:
- Exception messages with fewer than 10 words and no guidance
- Raw Python exceptions surfaced to connector developers without wrapping
- Error messages that reference internal SDK implementation details instead of developer actions
- New code using bare `AppError` instead of a categorical leaf
- New code using legacy `error_code=` / `AAF-{COMP}-{ID:03d}` format (back-compat only — do not use in new code)

### Categorical Leaf Selection Required (Critical)

New SDK exceptions must use the most specific leaf from `application_sdk.errors`:

| Situation | Leaf |
|-----------|------|
| Credentials missing/expired/invalid | `AuthError` |
| Authenticated but not authorized | `AppPermissionDeniedError` |
| External service temporarily down | `DependencyUnavailableError` |
| Caller input malformed | `InvalidInputError` |
| Entity does not exist | `NotFoundError` |
| Entity already exists (idempotent-create) | `AlreadyExistsError` |
| State conflict requires explicit fix | `PreconditionError` |
| Source returned 429 / quota | `RateLimitedError` |
| Bounded wait elapsed | `AppTimeoutError` |
| Local resource limit hit | `ResourceExhaustedError` |
| Returned data corrupt | `DataIntegrityError` |
| Feature not yet built | `UnimplementedError` |
| Unexpected invariant violation / bug | `InternalError` |

Each leaf sets a default `audience` (`USER | PLATFORM | APP_OWNER`) and `retryable` flag that consumers use for routing and retry decisions. The audience enum is closed three-valued — there is no `UNKNOWN` escape hatch; if the locus is unclear, use `APP_OWNER` (the team that wrote the code investigates and reclassifies).

### Debuggable Log Output (Important)

When something fails, the developer should be able to trace the problem from logs alone. Structured log fields are indexed in Loki/Grafana.

Flag:
- Error/warning log statements missing structured context fields
- Missing `exc_info=True` when logging caught exceptions
- Log messages that expose internal SDK paths instead of connector-relevant context

---

## 3. Migration and Upgrade Path

### Deprecation Warnings Required (Critical)

Any removed or relocated v2 API must have a deprecation shim at the old import path with `warnings.warn()` including:
1. What is deprecated
2. What to use instead (full import path)
3. When it will be removed (target: v3.1.0)

```python
# application_sdk/test_utils/integration/__init__.py — real example in the SDK
import warnings
warnings.warn(
    "application_sdk.test_utils.integration is deprecated and will be removed "
    "in v3.1.0. Use application_sdk.testing.integration instead.",
    DeprecationWarning,
    stacklevel=2,
)
from application_sdk.testing.integration import *  # noqa: E402,F401,F403
```

Flag:
- Removed import path without a deprecation shim
- Deprecation warning missing the replacement import path
- Deprecation warning missing the removal version
- `stacklevel` not set (must be 2)

### Clear Replacement Paths (Important)

Deprecation messages must give a direct copy-pasteable import.

Flag:
- Deprecation messages that say "use the new X" without the exact import
- Multiple possible replacements without guidance on which to choose

### Backward Compatibility of Contracts (Critical)

Existing connector code must not break. Contract evolution rules (ADR-0006):
- Add fields with defaults only
- Never remove or rename fields
- Never change field types

Flag:
- Removed fields on public contracts
- Renamed fields without keeping old name as deprecated alias
- Changed field types
- New required fields (no default) on existing contracts

### Removing a public kwarg requires a deprecation cycle (Important)

Public function/method kwargs are part of the contract. Silently
dropping or ignoring a previously-supported kwarg breaks callers
without warning. A removal must keep the parameter in the signature
for at least one release with a `DeprecationWarning`.

**Flag any change that:**
- Removes a kwarg from a public method or function signature that was
  present in the prior release.
- Renames a kwarg without keeping the old name as a deprecated alias
  (`old_name=DeprecationAlias(new_name)` or accept-and-warn).
- Silently ignores a kwarg's value (still accepting it for back-compat
  but discarding it) without emitting `warnings.warn(...,
  DeprecationWarning, stacklevel=2)`.

**Expected pattern:**

```python
def configure(self, *, new_name: str, old_name: str | None = None) -> None:
    if old_name is not None:
        warnings.warn(
            "`old_name` is deprecated, use `new_name`. Removal in v3.X.",
            DeprecationWarning,
            stacklevel=2,
        )
        new_name = old_name
    ...
```

**Severity:** Important (DX + back-compat). Same class as removing a
contract field — broadens the consumer-blast-radius check.

---

## 4. Discoverability and Imports

### Module __init__.py Exports (Critical)

Each public module must have an `__init__.py` with explicit `__all__`.

Key import entry points for connector developers:
```python
from application_sdk.app import App, task, entrypoint, Input, Output
from application_sdk.testing import MockStateStore, MockSecretStore, MockPubSub
from application_sdk.handler import Handler
from application_sdk.storage import CloudStore, create_local_store
```

Flag:
- New public class/function not added to its module's `__all__`
- Public symbols importable only via deep private paths
- `__init__.py` that re-exports private/internal symbols

### Logical Import Depth (Important)

Connector developers should reach any public API within 3 levels: `application_sdk.{module}.{symbol}`.

Flag:
- Public APIs requiring 4+ levels of import nesting
- Imports traversing `_`-prefixed (private) directories for public functionality

### Private-module imports from app code are forbidden (Critical)

Anything under a `_`-prefixed module (`_temporal/`, `_dapr/`,
`_redis/`, etc.) is private to the SDK. Connector developers and
app code MUST import only through the public-protocol surface
(`application_sdk.execution`, `application_sdk.infrastructure`,
etc.). Direct imports from private modules couple consumers to
implementation details and prevent the SDK from refactoring them.

**Flag any new import in the diff that:**
- Matches `from application_sdk\.[a-z_]+\._[a-z_]+(\.[a-z_]+)*` —
  i.e., reaches into a `_`-prefixed package or module from outside.
- Imports `temporalio.*`, `dapr.*`, `redis.*` directly anywhere
  outside the corresponding `_temporal/`, `_dapr/`, `_redis/`
  directories.
- Imports a symbol whose name starts with `_` from a non-`_`
  package (private symbol leaking through a public module).

**Diff-grep heuristic:**

```bash
git diff origin/main...HEAD -- '*.py' | \
  grep -E '^\+from application_sdk\.[a-z_]+\._[a-z_]+'
git diff origin/main...HEAD -- '*.py' | \
  grep -E '^\+(from|import) (temporalio|dapr|redis)\b' | \
  grep -vE '_temporal|_dapr|_redis'
```

Both should be empty. Any hit is a Critical finding.

**Severity:** Critical (ARCH). Concrete detection method for
ADR-0005.

---

## 5. Documentation Contract

### Google-Style Docstrings (Important)

All public API methods must have Google-style docstrings. At minimum a one-line summary. For core APIs: summary, Args, Returns, Raises, Example.

Flag:
- Public API method without any docstring
- Docstring using reStructuredText or NumPy style instead of Google style
- Missing `Raises` section when the method raises documented exceptions
- Missing `Example` on core APIs that developers subclass/call directly

---

## 6. Backwards Compatibility

### Existing Connector Code Must Not Break (Critical)

The highest-priority DX rule. Any change that would cause an existing connector's import or runtime behavior to break is critical unless accompanied by a deprecation shim.

Check for:
- Moved classes/functions without re-export at old path
- Changed method signatures (new required params, removed params, changed return type)
- Changed default values that alter behavior
- Removed public attributes from `App`, `Handler`, or contract classes

### Testing Utilities Stability (Important)

`application_sdk.testing` is part of the public API. Changes to testing utilities follow the same backward compatibility rules.

Flag:
- Removed or renamed mock class without deprecation shim
- Changed fixture signature without backward-compatible default
- New mock that doesn't follow `Mock{ServiceName}` naming convention

---

## 7. Conventional Commits and Releases

### Conventional Commits prefix must match diff scope (Critical)

The prefix on the PR title becomes the merge-squash commit message
and feeds `release-please`. A mismatched prefix ships the wrong
version number — major-bumping for nothing, minor-bumping for CI-only
content, or burying a real break behind `fix:`.

**For every PR, check the prefix against the diff content:**

| Prefix | Required evidence in the diff (else flag mismatch) |
|---|---|
| `feat!:` / `BREAKING CHANGE:` footer | At least one developer-facing public-API break: removed/renamed export in `__init__.py`'s `__all__`, removed/renamed public class or function, removed/renamed/retyped field on a public Input/Output contract, removed deprecation shim. If none → flag "drop the `!` or document the specific break in a `BREAKING CHANGE:` footer." |
| `feat:` | At least one new public capability — new export, new public method, new env-var-controlled behavior, new entry point. If the diff is purely under `.github/`, build config, or otherwise non-shipping → flag "this is not a feature; pick `ci:` / `build:` / `chore:`." |
| `fix:` | A code change that resolves a defect (logic change, missing case, race condition). Pure refactor with no behavior change → flag "use `refactor:`." |
| `ci:` / `build:` | Only files under `.github/`, `.pre-commit-config.yaml`, release/build config, Dockerfiles. Application code under `application_sdk/` touched → flag "the diff includes app code; pick a stronger prefix." |
| `chore:` / `docs:` | No application-code or contract change. |

**Detection:** for `feat!:` claims, diff the `__init__.py` files under
`application_sdk/` and check for removed `__all__` entries / missing
deprecation shims. For `feat:` / `fix:` on CI-only diffs, check
`git diff --name-only origin/main...HEAD` — if every path matches
`^\.github/|^\.pre-commit|^pyproject\.toml$|^uv\.lock$`, the prefix
should be `ci:` or `build:`.

**Severity:** Critical when the wrong prefix would cause a
major-version bump (`feat!:` on non-breaking diff). Important
otherwise. Always actionable — the PR title can be edited before
merge.

---

## 8. Environment Variables

### Env-var lifecycle: prefix, removal warnings, doc updates (Important)

`application_sdk` env vars are part of the deployment contract:
operators set them in Helm values, App Foundation reads them from
ConfigMaps. Renaming or removing one without a fallback breaks
deploys silently. Adding one without `ATLAN_` prefix breaks the
naming convention operators rely on for grep/audit.

**Flag any new code that:**
- Adds an `os.getenv(...)` / `os.environ[...]` / `Settings` field
  reading an env var that is NOT prefixed with `ATLAN_`. Exceptions:
  standard ecosystem vars (`OTEL_*`, `DAPR_*`, `KUBERNETES_*`,
  `HOME`, `PATH`).
- Renames an existing env var (changes the string literal) without
  appending the old name to `_REMOVED_ENV_VARS` in
  `application_sdk/constants.py` (so users get a startup warning
  pointing at the new name).
- Removes an env var (deletes the read call) without the same
  `_REMOVED_ENV_VARS` entry.
- Adds a new env var without an accompanying line in
  `docs/standards/env-vars.md` documenting purpose, default, and
  who sets it.

**Detection heuristic:** for each new `os.getenv(` / `os.environ[`
in the diff, check (a) the string literal starts with `ATLAN_` (or
is on the allowed-ecosystem list); (b) if a same-file removal of an
old env-var literal is also present, `_REMOVED_ENV_VARS` was
updated; (c) `docs/standards/env-vars.md` has a touch in the diff.

**Severity:** Important. Silent rename/removal breaks production
rollouts; the `_REMOVED_ENV_VARS` mechanism exists precisely so the
user sees a warning instead of running with stale config.
