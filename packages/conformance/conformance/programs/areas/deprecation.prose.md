---
kind: responsibility
name: deprecation-area
description: >
  Maintains the current B-series violation-set and drives remediation of
  deprecation findings.  B001 (app: stop consuming a deprecated SDK symbol) and
  B002 (sdk: fix a malformed deprecation notice) are guided fixes; B003 (overdue
  removal) and B004 (unmarked claim) are detect-only and route to residue.
---

### Maintains

The current set of unsuppressed B-series (backwards-compatibility / deprecation)
conformance findings in the working tree, classified by disposition and
remediability.

#### violations-deprecation

The fingerprint-set of all unsuppressed FAILING B-series results in the current
working tree, as reported by `suite.runner --series B`.

B-series rules are WARN-tier, so in **default** mode this facet is typically
empty (warnings do not fail the gate).  In **strict** mode the fingerprint-set
includes unsuppressed WARNING results, which is where B-series remediation
actually runs.

The active scope decides which rules can appear: on a consumer app only B001
(scope `app`) surfaces; on the SDK only B002/B003/B004 (scope `sdk`).  The runner
auto-detects scope, so each repo only ever sees its own half.

This facet's fingerprint moves when any B-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series B` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for B-series in the SARIF output is 0 (every
> B-series WARNING was cleared by a real fix or a justified suppression).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
In the Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "B"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "deprecation"`._

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

**Suppress outcome (strict mode only, WARNING-tier findings)**: the model may
propose an inline `# conformance: ignore[Bxxx] <8–40 word justification>` when
the site is a legitimate exception (e.g. a B001 usage in a compatibility shim
that intentionally bridges old and new APIs).  Route every suppression to residue
for human audit.
