# Check-spec examples

Worked examples of check specifications for `/audit-consumers`. The first two are audit-only
examples (no `fix:` block). The last three demonstrate the optional `fix:` block added for
`--raise-prs` mode.

---

## Example A: deprecated symbol removal (audit-only)

```markdown
# Check spec — ObjectStore.upload (legacy sync method removed in v4.0)

## Context
application_sdk v4.0 removes `ObjectStore.upload()` (sync). All callers must migrate to
`ObjectStore.upload_file()` (async). Audit before the removal PR merges.

## target_release
v4.0.0

## Anchors
- pattern: `from application_sdk\.storage`
- pattern: `ObjectStore`

## Checks

### check: R1 — Direct call to .upload()
- impact: definite-break
- require_anchor: true
- patterns:
  - `\.upload\s*\(`
- recommendation: replace `.upload(key, path)` with `await .upload_file(key, path)`
```

---

## Example B: exception type change (audit-only)

```markdown
# Check spec — BaseSQLClient error-type changes (PR #1234)

## Context
PR #1234 replaces ValueError/ClientError raises in BaseSQLClient with typed AppError subclasses.
Consumers catching by old type will silently stop receiving the error.

## target_release
application_sdk#1234

## Anchors
- pattern: `BaseSQLClient`
- pattern: `AsyncBaseSQLClient`
- pattern: `from application_sdk\.clients\.sql`

## Checks

### check: R1 — Narrow catch of old exception types
- impact: definite-break
- require_anchor: true
- patterns:
  - `except\s+(ValueError|ClientError|CommonError)\b`
- recommendation: update to catch the new typed error (e.g. `AuthError`, `InternalError`) or `AppError`

### check: R2 — Message-string inspection
- impact: silent-break
- require_anchor: false
- patterns:
  - `"DB_CONFIG is not configured"`
  - `SQL_CLIENT_AUTH_ERROR`
  - `CREDENTIALS_PARSE_ERROR`
- recommendation: switch to isinstance check against the new typed error class

### check: R3 — Broad except Exception near SDK calls
- impact: review
- require_anchor: true
- patterns:
  - `except\s+(Exception|BaseException)\b`
- recommendation: review whether the handler re-raises as a legacy type or inspects the message
```

---

## Example C: simple literal replacement with `fix:` block

Demonstrates the `replace:` variant (no regex backrefs needed) and a single `add_import`.

```markdown
# Check spec — ObjectStore.upload (with auto-fix)

## Context
application_sdk v4.0 removes `ObjectStore.upload()` (sync). Auto-fix to replace call sites.

## target_release
v4.0.0

## Anchors
- pattern: `from application_sdk\.storage`
- pattern: `ObjectStore`

## Checks

### check: R1 — Direct call to .upload()
- impact: definite-break
- require_anchor: true
- patterns:
  - `\.upload\s*\(`
- recommendation: replace `.upload(key, path)` with `await .upload_file(key, path)`
- fix:
  - pattern_index: 0
    replace: `.upload_file(`
    # Literal replacement: swaps the method name in the matched span.
    # The `await` keyword must already be present or is the caller's responsibility;
    # if the site lacks `await`, the LLM fallback adds it via recommendation:.
```

---

## Example D: regex replacement with backrefs + import, mixed-mode check

Demonstrates `replace_template:` (backrefs into groups) and `add_imports:` for a check that
has two patterns — the first is deterministic, the second falls back to LLM.

```markdown
# Check spec — BaseSQLClient error-type changes (with auto-fix on R1)

## Context
PR #1234 replaces ValueError/ClientError raises in BaseSQLClient with typed AppError
subclasses. R1 (narrow catch) can be auto-fixed deterministically. R2 (string match) and
R3 (broad catch) are too contextual and use LLM fallback.

## target_release
application_sdk#1234

## Anchors
- pattern: `BaseSQLClient`
- pattern: `AsyncBaseSQLClient`
- pattern: `from application_sdk\.clients\.sql`

## Checks

### check: R1 — Narrow catch of old exception types
- impact: definite-break
- require_anchor: true
- patterns:
  - `except\s+(ValueError|ClientError|CommonError)\b`
- recommendation: catch AppError (or a specific typed subclass) instead
- fix:
  - pattern_index: 0
    replace_regex: `except AppError`
    # Replaces the entire matched span with `except AppError`.
    # The import is added once per file if any R1 hit applies.
  add_import: `from application_sdk.errors import AppError`

### check: R2 — Message-string inspection
- impact: silent-break
- require_anchor: false
- patterns:
  - `"DB_CONFIG is not configured"`
  - `SQL_CLIENT_AUTH_ERROR`
  - `CREDENTIALS_PARSE_ERROR`
- recommendation: switch to isinstance check against the new typed error class
# No fix: block — LLM fallback generates a contextual isinstance rewrite

### check: R3 — Broad except Exception near SDK calls
- impact: review
- require_anchor: true
- patterns:
  - `except\s+(Exception|BaseException)\b`
- recommendation: narrow the catch to the specific SDK error type being inspected
# No fix: block — context-dependent; LLM fallback or human handles
```

---

## Example E: multi-import fix

Demonstrates `add_imports:` (list form) when a fix needs more than one new import.

```markdown
# Check spec — EventStore typed events migration

## Context
EventStore.emit() now requires a typed EventPayload subclass instead of a plain dict.
Consumers passing raw dicts will receive a TypeError at runtime.

## target_release
v3.9.0

## Anchors
- pattern: `from application_sdk\.events`
- pattern: `EventStore`

## Checks

### check: R1 — Raw dict passed to emit()
- impact: definite-break
- require_anchor: true
- patterns:
  - `\.emit\s*\(\s*\{`
- recommendation: wrap the dict in a typed EventPayload subclass; see SDK migration guide
# No fix: block — the replacement dict structure is consumer-specific; LLM fallback

### check: R2 — Direct use of deprecated emit_raw()
- impact: definite-break
- require_anchor: true
- patterns:
  - `\.emit_raw\s*\(`
- recommendation: replace emit_raw() with emit() + a typed EventPayload
- fix:
  - pattern_index: 0
    replace_regex: `.emit(`
  add_imports:
    - `from application_sdk.events import EventPayload`
    - `from application_sdk.events import EventStore`
    # Both imports added once per file where at least one R2 hit applies.
```
