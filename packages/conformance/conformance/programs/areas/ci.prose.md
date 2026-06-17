---
kind: responsibility
name: ci-area
description: >
  Maintains the current C-series violation-set.  Registered in the
  cross-area structure but remediation prescription is deferred to a later
  phase — detection runs, fixes do not.
---

### Maintains

The current set of unsuppressed C-series (CI / GitHub Actions) conformance
findings in the working tree, as reported by `suite.runner --series C`.

#### violations-ci

The fingerprint-set of all unsuppressed FAILING C-series results.  Extends to
include WARNING results in strict mode.

Postcondition (deferred — not yet enforced by the remediation loop):

> C-series remediation is not implemented in this phase.  All C-series
> findings route to the residue report for manual triage.  To implement,
> author the CI prescription in this file and update `remediate-finding` to
> dispatch on `area == "ci"`.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any file under `.github/` changes.

### Execution

```prose
# Detection only — route all findings to residue.
let violations = call detect-violations
  scope: scope
  series: "C"
  target: if mode == "strict" then "failing+warning" else "failing"

for each finding in violations:
  add finding to residue with note "C-series remediation deferred; add prescription to ci.prose.md"
```
