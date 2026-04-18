# v3 Holistic & Structural Review Rules

Big-picture review rules for PRs against application-sdk v3. Focus on design coherence, file health, dependency direction, and whether the PR is fixing the right thing -- not line-level style issues.

---

## Guiding Principle

Before reviewing individual lines, answer:

1. **Is this fixing a symptom or a cause?**
2. **Does this change make the codebase easier or harder to change next time?**
3. **Would this change need to be re-done when the root cause is addressed?**

If the answer to (3) is yes, the PR needs a different approach -- not polish.

---

## STRUCT-001: Symptom vs Cause Analysis (Critical)

Before approving any fix, determine whether it addresses the root cause or patches a symptom.

**Red flags:**
- PR description says "workaround" or "temporary fix" without a linked issue
- Adding a special case for one caller instead of fixing the underlying API
- Patching a v2 function when callers should migrate to v3 replacement
- Adding retry/fallback around code that should not fail in the first place
- Catching and suppressing an exception instead of preventing the condition

**Review action:** Classify each finding as:
- `PATCH` — fix is correct in place
- `MIGRATE` — v3 has a better API, move callers
- `REFACTOR` — wrong location, needs decomposition
- `DESIGN_CHANGE` — needs human decision

---

## STRUCT-002: File Health (Critical)

### Size Thresholds

| Metric | Threshold | Action |
|--------|-----------|--------|
| File length | > 500 lines | Flag: should this change go in a separate module? |
| Class length | > 300 lines | Flag: multiple responsibilities likely |
| Function length | > 75 lines | Flag: extract helpers |
| Parameters | > 7 params | Flag: consider config object |
| Nesting depth | > 3 levels | Flag: early returns, guard clauses |

### Known Large Files

| File | Lines | Status |
|------|-------|--------|
| `app/base.py` | ~1739 | Needs decomposition (tracked) |
| `handler/service.py` | ~1578 | Needs decomposition (tracked) |
| `main.py` | ~1147 | Needs decomposition (tracked) |

**Rule:** Any PR increasing line count of a file already over 500 lines must justify why new code belongs there. "It was already there" is not justification.

---

## STRUCT-003: Dumping Ground Detection (Critical)

Files with generic names (`utils.py`, `helpers.py`, `common.py`) attract unrelated code.

**Symptoms:**
- Generic filename
- Functions from multiple unrelated domains
- Imported by modules in 3+ different packages
- No cohesive theme beyond "stuff we needed somewhere"

**Review action:**
- PR adds function to dumping ground → ask where it actually belongs
- PR modifies dumping ground function → note debt, check if good time to move
- Never approve creating new `utils.py` or `helpers.py`

---

## STRUCT-004: Design Coherence (Critical)

A healthy codebase has one clear way to do each thing.

**Violations:**
- Two different patterns for the same operation
- Mixed abstraction levels in the same function
- New helper duplicating existing SDK functionality
- Inconsistent error handling within same module
- New config mechanism when one already exists

**The "Two Ways" test:** After this PR merges, will a new developer find two patterns for the same operation? If yes, the PR must either use the canonical pattern, migrate the old one, or include a TODO with issue reference.

---

## STRUCT-005: Technical Debt Accounting (Important)

**Flag:**
- TODO/FIXME without issue reference
- HACK/WORKAROUND without linked issue and removal plan
- Commented-out code (delete it; git has history)
- Dead code never called
- `noqa` / `type: ignore` / `pragma: no cover` without justification

**Positive signals:**
- PR removes more code than it adds
- PR deletes a deprecated shim
- PR consolidates two implementations
- PR moves code from dumping ground to domain module

---

## STRUCT-006: Dependency Direction (Critical)

v3 enforces strict dependency direction:

```
app  -->  execution  -->  infrastructure
 |                            ^
 +------- handler -------->---+
```

**Rules:**
- `app/` may import from `execution/`, `infrastructure/`, `contracts/`, `handler/`
- `execution/` may import from `infrastructure/`, `contracts/`
- `infrastructure/` imports only `contracts/` (and stdlib/third-party)
- `handler/` may import from `infrastructure/`, `contracts/`
- `contracts/` imports only stdlib and pydantic
- Nothing imports from `app/` except entry points and tests

**Violations:**
- `infrastructure/` importing from `app/` or `execution/` (reverse)
- `execution/` importing from `app/` (reverse)
- `contracts/` importing from any SDK module
- Circular imports even via deferred imports

---

## STRUCT-007: v2 Symbol Detection in Connector PRs (Critical)

When reviewing connector migration PRs, check for v2 symbols that should have been replaced. These v2 class names have zero matches in `application_sdk/` itself — they only appear in unmigrated connector code:

| If PR still uses... | Correct v3 replacement |
|---------------------|------------------------|
| `ObjectStore` | `application_sdk.storage` (`CloudStore`, `create_local_store`) |
| `WorkflowInterface` subclass | `application_sdk.app.App` with `@task` |
| `ActivitiesInterface` subclass | `@task` methods on `App` subclass |
| `HandlerInterface` subclass | `application_sdk.handler.Handler` |
| Direct `DaprClient` usage | `application_sdk.infrastructure` protocols |
| Direct `temporalio` imports | `application_sdk.execution` public API |
| `BaseSQLMetadataExtractionActivities` | `application_sdk.templates.SqlMetadataExtractor` |
| Dict-based credential access | `application_sdk.handler` typed contracts |

**Review action:**
- v2 symbol found in a connector PR → classify as MIGRATE, block until replaced
- v2 symbol found in SDK source itself → this is a blocker bug; create a ticket immediately

---

## Review Workflow

1. **Understand intent** — read PR description and linked issues
2. **Check v3 replacement** (STRUCT-007) — before looking at implementation
3. **Assess file health** (STRUCT-002, STRUCT-003) — size, dumping grounds
4. **Evaluate design coherence** (STRUCT-004) — "two ways" test
5. **Check dependencies** (STRUCT-006) — import direction
6. **Classify findings** (STRUCT-001) — PATCH / MIGRATE / REFACTOR / DESIGN_CHANGE
7. **Summarize debt** (STRUCT-005) — net impact
