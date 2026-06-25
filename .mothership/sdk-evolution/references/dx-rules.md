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

---

## 2. Error Messages and Debugging

### Actionable Error Messages (Critical)

Every user-facing exception must tell the developer: (1) what went wrong, (2) why it matters, and (3) how to fix it.

```python
# BAD -- opaque
raise AppError("Invalid configuration")

# GOOD -- what + why + fix
raise AppError(
    "Credential not found for connection 'snowflake-prod'. "
    "Ensure the credential is provisioned in the Atlan UI under "
    "Settings > Credentials before running this workflow.",
    error_code=CREDENTIAL_NOT_FOUND,
)
```

Flag:
- Exception messages with fewer than 10 words and no guidance
- Raw Python exceptions surfaced to connector developers without wrapping
- Error messages that reference internal SDK implementation details instead of developer actions

### AAF Error Codes Required (Critical)

All SDK exceptions raised to connector code must carry a structured `ErrorCode` following the `AAF-{COMP}-{ID:03d}` format defined in `application_sdk/errors.py`.

Current components: APP, STR, CTR, HDL, EXE, INF, CRD, DSC, EVT.

Flag:
- New exception classes without an `error_code` parameter
- New error scenarios that reuse a generic code when a specific one would be more useful
- Error codes that break the naming convention

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
