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

- **O001 OrjsonOverStdlibJson** — the site calls `json.dumps(...)` or
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

#### Area: logging (L-series) — PHASE 2

Consult the finding's `hint` and `message`, then read the actual source lines
around `finding.line` in `finding.file` before proposing a fix.

**Mechanical rules** (`autofixable = true`, `classification = "mechanical"`):

- **L004 ExceptBlockMissingExcInfoLog** — add `exc_info=True` as a keyword
  argument to the log call inside the except block.
  `logger.warning("msg")` → `logger.warning("msg", exc_info=True)`.
  If the call already has keyword arguments, append after them.

- **L007 LoggerCriticalUsage** — rename `.critical(` to `.error(`.  If the
  call site is inside an except block and has no `exc_info` kwarg, also add
  `exc_info=True`.  Outside an except block, rename only.
  `logger.critical("msg")` → `logger.error("msg")`.

- **L015 DictConfigDisableExistingLoggers** — in the dict literal argument
  to `logging.config.dictConfig()`, set `"disable_existing_loggers": False`.
  If the key is absent, add `"disable_existing_loggers": False` as an entry.
  If it is present as `True`, change the value to `False`.

- **L017 LoggerExceptionUsage** — rename `.exception(` to `.error(` and add
  `exc_info=True` if not already present.
  `logger.exception("msg")` → `logger.error("msg", exc_info=True)`.
  If `exc_info=False` is already present, leave it (the caller intentionally
  suppressed the traceback); rename the method only.

- **L020 DeprecatedLoggingWarn** — simple rename: `.warn(` → `.warning(`.
  Also handles the module-level form: `logging.warn(` → `logging.warning(`.

**Judgment rules** (`classification = "judgment"`; route to residue):

- **L001 FStringInLogMessage** — rewrite the f-string as %-style.  Move each
  `{expr}` to a positional argument after the format string, replacing it with
  `%s` (use `%r` for repr, `%d` for clearly integer expressions).  For a
  single interpolation this is nearly mechanical; for complex nested
  expressions (conditional, method call, attribute chain) examine context and
  use `%s` with a clear string representation.  Never introduce `.format()`
  or concatenation as the replacement.

- **L002 NonCanonicalLoggerFactory** — swap to the canonical SDK adapter.
  Steps:
  1. Add `from application_sdk.observability.logger_adaptor import get_logger`
     to the import block if not present.
  2. Replace the non-canonical acquisition:
     - `logging.getLogger(name)` → `get_logger(name)`.
     - `structlog.get_logger(...)` → `get_logger(__name__)`.
     - `from loguru import logger` (direct import) → remove the import line;
       add `logger = get_logger(__name__)` at module level.
  3. Remove the now-unused `import logging` / `import structlog` line if no
     other usages remain in the file.
  Classification is always `"judgment"` — the import change affects the whole
  file and requires verifying that no other symbols from the removed import
  are still in use.

- **L005 PrintInProductionCode** — replace `print(...)` with a logger call.
  Choose level from context:
  - Output that describes an error or exception → `logger.error(...)`.
  - Output that looks diagnostic / verbose → `logger.debug(...)`.
  - Default / informational → `logger.info(...)`.
  Rewrite any f-string or concatenation in the print argument to %-style in
  the logger call (applying the L001/L011 transform).  Ensure `get_logger` is
  imported and a module-level `logger` is present; add them if missing.

- **L011 StringConcatenationInLog** — rewrite string concatenation as
  %-style.  Identify alternating segments: literal strings become the static
  parts of the format string; non-literal expressions each become a `%s`
  positional arg.  `logger.info("User " + name + " connected")` →
  `logger.info("User %s connected", name)`.  For `str(expr)` wrappers, drop
  the `str()` call and use `%s` (Python's `%` will call `str()` implicitly).

- **L013 StdlibArbitraryKwargs** — move non-allowlist kwargs into the message
  body.  Allowlist: `{exc_info, extra, stack_info, stacklevel}`.  For each
  non-allowlist kwarg `key=value`: append `key=%s` to the format string and
  move `value` to a positional argument after the existing args.  If the
  message is not already %-style, first rewrite it (applying L001/L011
  transform) before appending context.
  `logger.info("Connected", host=host, port=port)` →
  `logger.info("Connected host=%s port=%s", host, port)`.

- **L021 MissingLoggingLintRules** — add the missing rule IDs to
  `[tool.ruff.lint]` in `pyproject.toml`.  Prefer extending `extend-select`
  (not `select`) to avoid clobbering existing selections.  The missing rule
  IDs are listed in the finding message.  Add them as additional strings in
  the `extend-select` list; create the key if absent.  Example addition for
  G001, G003, G004, T201, LOG009:
  ```toml
  [tool.ruff.lint]
  extend-select = ["G001", "G003", "G004", "T201", "LOG009"]
  ```
  If a category prefix already covers some rules (e.g. `"G"` covers all
  G-rules), add only the genuinely missing individual IDs.

**All other L-series rules** (L003, L006, L008, L009, L010, L012, L014,
L016, L018, L019) — `autofixable = false`; produce `classification =
"judgment"` and a best-effort fix guided by the `hint` and `message` in the
finding.  L010 (CredentialInLogOutput) is a security finding; always route to
residue and never auto-apply.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose a suppression instead of a fix if it judges the pattern a legitimate
exception for this specific site (e.g. an L005 `print()` inside a
`__main__` guard that the checker could not statically detect as exempt, or an
L006 loop that is provably bounded to ≤10 items via a literal collection).
The suppression edit is an inline directive on the line above the violation:

```
# conformance: ignore[LXXX] <concise justification, 8–40 words>
```

The justification must describe *why* the pattern is acceptable here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit regardless.

---

#### Area: ci (C-series) — DEFERRED

No prescription authored for this phase.  Return `not_remediable = true`.
Same residue note as logging.

---

#### Area: deprecation (B-series) — PHASE 1

Consult the finding's `hint` and `message` — for the B-series the message
carries the SDK's own migration guidance — then read the actual source lines
around `finding.line` in `finding.file` before proposing anything.

**Guided fixes** (`classification = "judgment"`; the loop applies and gates them
with `recheck-narrowest` + the test orthogonal gate, then routes to residue for
human audit):

- **B001 DeprecatedSdkSymbolUsage** (app source) — the app imports, subclasses,
  or calls a symbol the SDK has deprecated.  Apply the migration named in the
  finding message — **never a blind name swap**: the replacement usually changes
  the call shape (signature, return type, import path).  Examples:
  - `upload_to_atlan(input)` → `App.upload(UploadInput(local_path=...,
    tier=StorageTier.RETAINED))` — different argument and return types; read the
    call site and adapt both.
  - `from application_sdk.discovery import DiscoveryError` →
    `from application_sdk.errors import InvalidInputError`, and update every
    use of the old name in the file.
  - `class X(BaseMetadataExtractor)` → migrate to `application_sdk.templates.SqlApp`
    per the notice; this is a structural change — draft it and let the test gate
    decide.
  Because the migration is non-trivial, `classification` is always `"judgment"`.
  The orthogonal test gate is what makes applying it safe: if the migration
  breaks behaviour, the gate reverts and routes to residue.

- **B002 MalformedDeprecationNotice** (SDK source) — the notice is missing a
  migration target and/or a removal version.  Edit the notice string in place to
  add what the finding says is missing:
  - missing migration target → add `use <replacement>` naming the real successor
    (read the surrounding code / docstring to find it);
  - missing removal version → add `will be removed in v<N>`, choosing the next
    major unless the surrounding context names a version, and **state that
    assumption** in the edit.
  `classification` is `"judgment"` (the wording and target need a human-level
  call); the recheck gate confirms the notice now parses as well-formed.

**Detect-only — route to residue** (`not_remediable = true`):

- **B003 OverdueDeprecationRemoval** (SDK source) — the symbol was promised gone
  by a version the SDK has already reached.  Resolving it means *removing a public
  symbol* or *pushing out the removal version* — both are human decisions with
  fleet-wide blast radius, so never auto-edit.  Record in residue with the
  finding message (which names the overdue version and current version).

- **B004 UnmarkedDeprecationClaim** (SDK source) — a docstring claims deprecation
  with no marker.  Which marker to add (`@deprecated` decorator vs a
  `DeprecationWarning` in `__init__`/`__init_subclass__`) is a small design
  choice for the symbol's owner; record in residue with the suggestion the
  finding message already carries.

**Suppress outcome (strict mode only, WARNING-tier findings)**: as for the other
areas, the model may propose an inline `# conformance: ignore[Bxxx] <8–40 word
justification>` when the site is a legitimate exception (e.g. a B001 usage in a
compatibility shim that intentionally bridges old and new APIs).  Route every
suppression to residue for human audit.
