---
kind: responsibility
name: error-handling-area
description: >
  Maintains the current E-series violation-set and drives remediation of
  error-handling conformance findings.  The only fully-implemented area in
  phase 1.
---

### Maintains

The current set of unsuppressed E-series (error-handling) conformance findings
in the working tree, classified by disposition (FAILING / WARNING) and
remediability.

#### violations-error-handling

The fingerprint-set of all unsuppressed FAILING E-series results in the
current working tree, as reported by `suite.runner --series E`.

In strict mode the fingerprint-set extends to include unsuppressed WARNING
results as well.

This facet's fingerprint moves when any E-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series E` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
This is the Reactor-ready wake source — in the Claude Code skill path, the
skill caller re-invokes on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "E"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "error-handling"`._

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

- **All other E-series rules (E003, E004, E007–E012, E014, E015, E017, E018, E019, E020)** — produce
  `classification = "judgment"` and a best-effort fix guided by the `hint` and
  `message`.  (E019 is the return/append-value counterpart of E015: route the
  caught exception into a typed `AppError`/evidence field and keep the contract
  `message=` a stable, sanitised summary.  E020: replace the empty/None return on
  a checked HTTP-failure branch with a raised typed `AppError` so the failure
  propagates instead of publishing an empty success.)

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
