---
kind: function
name: remediate-finding
description: >
  Proposes a source edit (or a justified inline suppression) for a single
  conformance finding.  The model is the worker here — it reads the finding's
  hint, classifies the fix, and emits an edit.  The deterministic re-check
  gate (recheck-narrowest) decides whether the edit worked.
---

### Parameters

- `finding` (object, required) — a finding as returned by `detect-violations`:
  `rule_id`, `area`, `file`, `line`, `column`, `message`, `hint`,
  `autofixable`, `disposition`, `fingerprint`.
- `mode` (string, required) — `"default"` or `"strict"`.  The `suppress`
  outcome is only available for WARNING-tier findings when mode is `"strict"`.

### Returns

- `outcome` — `"fix"` (source logic change) or `"suppress"` (inline ignore
  directive, strict mode only).
- `edit` — a description of the change to apply, including file path, the
  exact lines to change or insert, and the replacement text.
- `classification` — `"mechanical"` (deterministic, no judgment needed) or
  `"judgment"` (model made a non-trivial call; route to residue for human
  audit).
- `external_influence` — boolean; true if the model consulted any content
  outside the source file itself that could be attacker-influenced.  Always
  false for error-handling in this phase; wired for future dependency/CVE use.
- `not_remediable` — boolean; true when the area has no authored prescription
  yet (returns to residue without an edit attempt).

### Write-scope constraint

This function may **only** propose edits to Python source files under the
repository root — never to `tests/`, `.github/`, `conformance/`, or any CI /
gate configuration.  This is the §6.1 "no self-judging changes" discipline:
the remediator may not touch the gate it is judged against.

### Dispatch by area

Route on `finding.area` to the matching area prescription below.

---

#### Area: error-handling (E-series) — PHASE 1

Consult the finding's `hint` and `message`, then look at the actual source
lines around `finding.line` in `finding.file` before proposing a fix.

**Mechanical rules** (`autofixable = true`) — produce a `"fix"` outcome with
`classification = "mechanical"`:

- **E005 ExceptBlockMissingExcInfo** — add `exc_info=True` to the log call
  inside the except block.  The edit is always a single keyword argument
  addition.  Example: `logger.warning("msg")` → `logger.warning("msg",
  exc_info=True)`.

- **E016 MissingExceptionChaining** — add `from exc` (or `from e`, matching
  the existing except clause variable) to the bare `raise X(...)` inside the
  except block.  Example: `raise ValueError(msg)` → `raise ValueError(msg)
  from exc`.

**Judgment rules** (`autofixable = false`) — produce a `"fix"` outcome with
`classification = "judgment"`; always route to residue:

- **E002 TypedExceptPass** — the `except SomeError: pass` swallows the
  exception silently.  Propose replacing `pass` with a log call:
  `logger.warning("Ignoring %s: %s", type(e).__name__, e, exc_info=True)`,
  where `e` is the except clause variable (or `exc` if bare).  If the
  surrounding context suggests a best-effort probe (e.g. feature detection at
  import time), note this in the residue as a suppression candidate.

- **E001 BareExceptPass** — same treatment as E002 but bare `except:`.
  Propose adding a typed `Exception` clause and a log call.

- **E013 LegacyAtlanErrorRaise** — the code raises a deprecated `AtlanError`
  subclass.  Consult the `/typed-failures` prescription: propose replacing
  with the appropriate `AppError` subclass from
  `application_sdk.common.error_codes`.  Choose the subclass by matching the
  raise site's semantic category (connection, permission, not-found, etc.) to
  the `AppError` hierarchy.  Classification is always `"judgment"` — the
  mapping requires understanding the call-site intent.

- **E006 BareExceptWithBody** — bare `except:` with a non-empty body.
  Propose narrowing to `except Exception as exc:` and adding
  `exc_info=True` to any existing log calls in the body.

- **All other E-series rules (E003, E004, E007–E012, E014–E018)** — produce
  `classification = "judgment"` and a best-effort fix guided by the `hint` and
  `message`.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose a suppression instead of a fix if it judges the pattern a legitimate
exception for this specific site (e.g. an E004 broad-except at a genuine
top-level worker loop boundary).  The suppression edit is an inline directive
inserted as a comment on the line above the violation:

```
# conformance: ignore[E004] <concise justification, 8–40 words>
```

The justification must describe _why_ the pattern is acceptable here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit regardless.

---

#### Area: optimizations (O-series) — PHASE 1

Consult the finding's `hint` and `message`, then look at the actual source
lines around `finding.line` in `finding.file` before proposing a fix.

**Judgment rules** (`autofixable = false`) — produce a `"fix"` outcome with
`classification = "judgment"`; always route to residue:

- **O001 StdlibJsonOverOrjson** — the site calls `json.dumps(...)` or
  `json.loads(...)` on the stdlib module.  `orjson` is **not** a drop-in, so
  this is never mechanical:
  - `json.loads(s)` → `orjson.loads(s)` is usually direct (orjson accepts
    `str` or `bytes`).
  - `json.dumps(obj)` → `orjson.dumps(obj)` returns **`bytes`, not `str`**.
    Inspect the call site: if the result is written to a text sink, passed
    where a `str` is required, or concatenated with `str`, append `.decode()`.
    If it feeds a bytes sink (file opened `"wb"`, a socket, a hash), leave as
    bytes.
  - Translate keyword arguments: `indent=2` → `option=orjson.OPT_INDENT_2`;
    `sort_keys=True` → `option=orjson.OPT_SORT_KEYS` (OR-combine multiple
    options); a `default=` callable stays as the `default` keyword (orjson
    supports it).  Drop kwargs orjson cannot express and note them in residue.
  - Ensure `import orjson` is present at module top (it is a core SDK
    dependency); add it if missing.

  The orthogonal gate **bites** here: a `bytes`/`str` regression on any
  covered path fails the behavioural tests, so a careless swap is caught by
  `orthogonal-gate` before the edit survives.  Classification is always
  `"judgment"` (the decode/kwargs call requires reading the call site), so the
  edit is also routed to residue for human confirmation.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and the site legitimately needs stdlib `json` (e.g.
interop with a library that requires a `str` and the bytes-decode round-trip
is wasteful, or a `json.JSONEncoder` subclass), the model may propose an
inline `# conformance: ignore[O001] <justification>` instead of a fix.  Route
every suppression to residue for human audit.

---

#### Area: prescriptions (P-series) — PHASE 1 (suggest-only)

Drafts a **proposed** fix for human review.  The `prescriptions-area` records
the proposal in residue and **never applies it** (see prescriptions.prose.md):
P001's only detector-clearing fix is a `MaxItems` bound or an inline
suppression, and `MaxItems` is **not runtime-enforced**, so `recheck-narrowest`
accepts any bound and the orthogonal test gate is structurally blind — no gate
can validate the proposal (design §6.1).  The safe form is therefore
propose-don't-apply: this function only *drafts* the change; a human is the gate.

- **P001 UnboundedContractFields** — the contract opts out of payload safety
  via the `allow_unbounded_fields=True` class keyword.  Read the contract's
  fields around `finding.line`, then draft, in order of preference:

  1. **The real fix (preferred)** — remove `allow_unbounded_fields=True` and
     bound each field the payload-safety validator would reject: wrap an
     unbounded `list[T]` as `Annotated[list[T], MaxItems(N)]` and an unbounded
     `dict[K, V]` as `Annotated[dict[K, V], MaxItems(N)]`, choosing `N` from
     the field's realistic cardinality and **stating that assumption** in the
     proposal (e.g. ~10000 ≈ ~1MB JSON, well under Temporal's 2MB limit).  A
     scalar-only contract needs only the opt-out removed.  Add
     `from typing import Annotated` and
     `from application_sdk.contracts.types import MaxItems` if missing.
     Return `outcome = "fix"`.

  2. **Fallback** — if a field is genuinely unbounded with no sensible cap,
     draft an inline `# conformance: ignore[P001] <concise justification>` on
     the declaration line, where the justification explains *why* unbounded
     fields are unavoidable here (not merely that the rule is suppressed).
     Return `outcome = "suppress"`.

  `classification` is **always `"judgment"`** for P001 — both the bound value
  and the bound-vs-suppress decision require human-level judgement, and there
  is no gate to validate them.  The proposal is recorded in residue for human
  review; the area does not apply it.

This area graduates to the full `detect-fix-recheck` (apply-and-keep) loop only
once a gate exists that validates the bound (a runtime-enforced `MaxItems`, or
a payload-size behavioural check).

---

#### Area: logging (L-series) — DEFERRED

No prescription authored for this phase.  Return `not_remediable = true`.
The finding is added to the residue report as "detected but not yet
remediable; add a logging prescription to remediation/programs/areas/logging.prose.md".

---

#### Area: ci (C-series) — DEFERRED

No prescription authored for this phase.  Return `not_remediable = true`.
Same residue note as logging.
