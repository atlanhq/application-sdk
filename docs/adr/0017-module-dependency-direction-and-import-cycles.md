# ADR-0017: Module Dependency Direction and Import-Cycle Policy

## Status

**Proposed** — `needs-design-review`. Raised by the Autonomous SDK Evolution
weekly run (THEME=ARCH, 2026-07-20) under BLDX-1564 (parent BLDX-1563). No code
rollout until the approach is ratified by the SDK maintainers.

## Context

The SDK's codified layering is `app/ → execution/ → infrastructure/` (never
reverse). This direction is asserted as a review criterion
(`docs/agents/review.md`) and in the SDK Evolution check-registry, but it has
never been captured as an ADR and nothing enforces it.

ADR-0005 (Infrastructure Abstraction) governs the **consumer** boundary — app
developers never import `temporalio`/`dapr` directly. This ADR is about the
**SDK's own internal** module structure, which ADR-0005 does not cover.

### What the code actually looks like today

`PLC0415` (imports-must-be-at-top-of-module) is a **deliberately enabled** ruff
rule. Its stated purpose in `pyproject.toml` is to *prevent
broken-import-at-runtime bugs*:

```toml
# pyproject.toml — [tool.ruff.lint] extend-select
"PLC0415", # imports must be at top of module — prevents broken-import-at-runtime bugs (e.g. PR #1543)
```

Measured across `application_sdk/` (production package, excludes `tests/` and
`tools/`, which are per-file-exempt):

| Metric | Count |
| --- | --- |
| `# noqa: PLC0415` function-local imports | **538** |
| Files carrying at least one | **108** |
| Annotated `circular:` | **158** |
| …of which are legitimate Temporal-sandbox imports (`imports_passed_through()`) | **7** |
| Net circular-workaround imports (design debt) | **~151** |

So the rule the team enabled to prevent runtime-import bugs is defeated 538
times by hand-written suppressions, ~151 of them because the module graph has
cycles. The suppressions pass CI cleanly, so static analysis never surfaces the
debt, and a reader must open each call site to learn the real dependency graph.

### Two root causes

**Cause 1 — eager `__init__.py` sibling aggregation.** Every package `__init__`
eagerly imports all of its sibling submodules to present a flat public API. For
example `application_sdk/storage/__init__.py`:

```python
from application_sdk.storage.binding import (...)
from application_sdk.storage.cloud import CloudStore
from application_sdk.storage.errors import (...)
from application_sdk.storage.factory import create_local_store, create_memory_store
from application_sdk.storage.ops import (...)
from application_sdk.storage.preflight import verify_object_store_access
```

Importing *any* symbol from `storage` therefore loads *every* storage submodule.
The moment one sibling (or a module in another package) needs a symbol that
transitively re-enters the package mid-initialisation, Python raises
`ImportError`/`AttributeError`, and the only local fix is to defer the import
into a function body. This is self-inflicted and accounts for the largest
buckets — by the `circular:` rationale strings:

| Rationale (dedup) | Count |
| --- | --- |
| `storage/__init__.py loads sibling modules` | 35 |
| `credentials/__init__.py loads sibling modules` | 31 |
| `execution/__init__.py loads _temporal which imports app.base` / `app.base imports execution` | 26 |
| `package __init__ loads sibling modules` | 12 |
| `infrastructure/__init__.py loads sibling modules` | 5 |

**Cause 2 — a true `app/ ↔ execution/` layering inversion.** Independent of
`__init__` aggregation, the two layers import each other at runtime:

```python
# application_sdk/execution/_temporal/activity_utils.py:41 — module top-level, unconditional
from application_sdk.app.registry import AppRegistry
```

```python
# application_sdk/app/base.py:162 — deferred back into execution because of the cycle
from application_sdk.execution.heartbeat import (  # noqa: PLC0415 — deferred: app.base is imported by execution (circular)
    run_in_thread,
)
```

`app/base.py` defers 8+ `execution.*` imports (heartbeat, retry, errors,
preflight_gate, the `execution` package) with the same rationale. This is a real
reverse arrow: `execution/` depends on concrete `app/` classes, contradicting the
codified `app/ → execution/` direction.

### Why it matters

- **The enabled guardrail is neutralised.** PLC0415 exists to catch
  broken-at-runtime imports; 538 blanket `noqa`s mean it protects almost
  nothing in the package it was meant to protect.
- **The dependency graph is invisible to tooling.** Deferred imports hide edges
  from ruff, import linters, and IDEs. The layering in the check-registry cannot
  be verified mechanically, so "never reverse" is aspirational.
- **Fragility and cost.** Every deferred import re-executes the `sys.modules`
  lookup on each call and can only fail at call time, not import time — the exact
  failure mode PLC0415 was turned on to prevent. New cycles are added silently
  because nothing objects.
- **Onboarding tax.** 158 near-identical `circular:` comments signal an accepted
  workaround, which trains contributors to reach for `noqa: PLC0415` instead of
  fixing structure — the debt compounds.

## Decision

Adopt a two-part module-structure policy and pay down the existing debt behind
it:

1. **Lazy public-API aggregation** — package `__init__` files expose their flat
   API through PEP 562 module `__getattr__` instead of eager top-level imports,
   so importing a package no longer force-loads every sibling.
2. **Dependency inversion at the `app/ ↔ execution/` seam** — `execution/`
   depends on a small neutral protocol module, not on concrete `app/` classes,
   restoring the `app/ → execution/` direction.
3. **A conformance guardrail** so the debt cannot silently regrow after it is
   paid down (rule + remediation ship together, per the conformance discipline).

This is a **proposal**; the sections below lay out the options so the maintainers
can ratify the target before any 108-file rollout.

## Options Considered

### Option A — Lazy `__init__` via PEP 562 `__getattr__` (recommended for Cause 1)

Replace eager re-exports with a lazy resolver. Public import paths
(`from application_sdk.storage import CloudStore`) stay identical for consumers.

```python
# application_sdk/storage/__init__.py  (sketch)
import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # static analysers + IDEs still see the real symbols
    from application_sdk.storage.cloud import CloudStore
    from application_sdk.storage.ops import download_file, upload_file
    # …

_EXPORTS = {
    "CloudStore": "application_sdk.storage.cloud",
    "download_file": "application_sdk.storage.ops",
    "upload_file": "application_sdk.storage.ops",
    # …one entry per __all__ symbol
}

def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)

def __dir__():
    return sorted(_EXPORTS)
```

Because the package no longer force-loads siblings, the ~48 self-inflicted
`__init__`-aggregation cycles (storage 35 + credentials 31 + infra 5 + generic
12, minus overlap) dissolve, and their deferred imports move back to module top
where PLC0415 can protect them again.

- **Pros:** Consumer import paths unchanged; largest bucket of cycles removed
  mechanically; `TYPE_CHECKING` block preserves IDE/pyright completion; faster
  cold import (siblings load on demand).
- **Cons:** `from package import *` and runtime `dir()` need the `__all__`/
  `__dir__` shims above; a typo in `_EXPORTS` fails at attribute access, not
  import — mitigated by a test that asserts every `__all__` entry resolves;
  slightly more `__init__` boilerplate (generatable from `__all__`).

### Option B — Dependency-inversion seam for `app/ ↔ execution/` (recommended for Cause 2)

`execution/` needs only a *registry-shaped* view of the app (list activities,
fetch task metadata). Define that as a Protocol in a neutral module both layers
may depend on, and have the app register itself against it.

```python
# application_sdk/execution/registry_protocol.py  (new, no app import)
from typing import Protocol, runtime_checkable

@runtime_checkable
class ActivityRegistry(Protocol):
    def activities(self) -> list[str]: ...
    def task_metadata(self, name: str) -> "TaskMetadataView": ...
```

`execution/_temporal/activity_utils.py` then imports the Protocol, not
`app.registry.AppRegistry`; `app/` provides the concrete implementation. The
reverse arrow disappears and `app/base.py` no longer needs its deferred
`execution.*` imports.

- **Pros:** Restores the codified direction; the seam is explicit and testable;
  matches ADR-0005's Protocol-based abstraction style.
- **Cons:** Larger, more invasive than A; touches the worker/activity hot path,
  so it needs its own review and careful test coverage. Best done as a second
  step after A shrinks the surrounding noise.

### Option C — Status quo (keep the `noqa`s)

Do nothing; continue adding `# noqa: PLC0415 — circular` as needed.

- **Pros:** Zero effort now.
- **Cons:** The enabled guardrail stays neutralised; the dependency graph stays
  invisible; debt keeps compounding; "never reverse" stays unenforceable. This is
  the trajectory that produced 538 suppressions.

## Recommendation

**A first, then B, gated by C's guardrail.**

1. Land this ADR (decision + target) — **this PR, docs-only, zero blast radius.**
2. Roll out **Option A** package-by-package (`storage`, `credentials`,
   `infrastructure`, then the rest), each PR converting one `__init__` and moving
   that package's freed deferred imports back to top-level, with a test asserting
   every `__all__` symbol resolves. One package per PR keeps each diff reviewable.
3. Do **Option B** as a focused follow-up on the `app/ ↔ execution/` seam once A
   has removed the surrounding aggregation cycles.
4. Add the **conformance guardrail** (Option C's enforcement): an `sdk`-scope
   rule that fails when a `# noqa: PLC0415` carries a `circular:` rationale in
   `application_sdk/` (i.e. a new cycle workaround), shipped with its remediation
   guidance in the same PR. This caps the debt at zero-new and makes the
   `app/ → execution/ → infrastructure/` direction mechanically enforceable
   instead of aspirational.

Sequencing A→B→guardrail means the tree is never left half-converted, and the
538 count only ever goes down.

## How to review

- **Is the direction right?** Confirm `app/ → execution/ → infrastructure/` is
  the intended contract and that Option B's Protocol seam is the shape you want
  (vs. e.g. moving `AppRegistry` into `execution/`).
- **Is PEP 562 acceptable for public packages?** The known cost is `import *` /
  `dir()` and fail-at-access; the sketch mitigates both. Flag any consumer that
  relies on eager side effects at package import (none found in this package).
- **Guardrail scope.** Should the new rule block *all* new `circular:` noqas in
  `application_sdk/`, or only reverse-direction ones? (Recommendation: all, since
  each is a cycle.)
- **Rollout size.** ~151 net deferred imports across ~108 files; confirm the
  one-package-per-PR cadence and whether Temporal-sandbox imports (the 7
  `imports_passed_through()` cases) stay as-is (they should — they are a genuine
  Temporal constraint, not debt).

## Consequences

- **Positive:** PLC0415 protects the package again; the dependency graph becomes
  static-analysable; the codified layering is enforced, not asserted; faster cold
  imports; contributors stop reaching for `noqa` as the default.
- **Negative / cost:** A multi-PR rollout (bounded, mechanical, one package per
  PR); a small amount of `__getattr__`/`__all__` boilerplate per package
  (generatable); Option B touches the activity hot path and needs careful tests.
- **Neutral:** Consumer import paths are unchanged throughout; the 7 legitimate
  Temporal-sandbox deferrals remain.

## References

- ADR-0005 (Infrastructure Abstraction) — consumer-facing boundary; complementary.
- `docs/agents/review.md` — dependency-direction review criterion.
- Evidence: `pyproject.toml` (PLC0415 enable), `application_sdk/storage/__init__.py`
  (eager aggregation), `application_sdk/execution/_temporal/activity_utils.py:41`
  and `application_sdk/app/base.py:162` (the `app/ ↔ execution/` cycle).
- [PEP 562 — Module `__getattr__` and `__dir__`](https://peps.python.org/pep-0562/).
