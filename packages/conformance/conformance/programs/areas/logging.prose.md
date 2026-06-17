---
kind: responsibility
name: logging-area
description: >
  Maintains the current L-series violation-set.  Registered in the
  cross-area structure but remediation prescription is deferred to a later
  phase — detection runs, fixes do not.
---

### Maintains

The current set of unsuppressed L-series (logging) conformance findings in
the working tree, as reported by `suite.runner --series L`.

#### violations-logging

The fingerprint-set of all unsuppressed FAILING L-series results.  Extends to
include WARNING results in strict mode.

Postcondition (deferred — not yet enforced by the remediation loop):

> L-series remediation is not implemented in this phase.  All L-series
> findings route to the residue report for manual triage.  To implement,
> author the logging prescription in this file and update `remediate-finding`
> to dispatch on `area == "logging"`.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any `*.py` file under `scope` changes.

### Execution

```prose
# Detection only — route all findings to residue.
let violations = call detect-violations
  scope: scope
  series: "L"
  target: if mode == "strict" then "failing+warning" else "failing"

for each finding in violations:
  add finding to residue with note "L-series remediation deferred; add prescription to logging.prose.md"
```
