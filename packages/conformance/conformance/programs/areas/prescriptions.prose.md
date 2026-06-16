---
kind: responsibility
name: prescriptions-area
description: >
  Maintains the current P-series violation-set.  Registered in the cross-area
  structure but remediation is intentionally DEFERRED — detection runs, fixes
  do not — because P-series rules currently have no orthogonal gate that can
  validate a model-proposed fix.
---

### Maintains

The current set of unsuppressed P-series (prescription) conformance findings
in the working tree, as reported by `suite.runner --series P`.

#### violations-prescriptions

The fingerprint-set of all unsuppressed FAILING P-series results.  Extends to
include WARNING results in strict mode.

Postcondition (deferred — not yet enforced by the remediation loop):

> P-series remediation is not implemented in this phase.  All P-series
> findings route to the residue report for manual triage.

**Why deferred (not an oversight):** P001 `UnboundedContractFields` is
suppress-only, and its only fix that clears the detector is adding
`Annotated[..., MaxItems(N)]` or an inline suppression.  `MaxItems` is a
**declarative marker — not runtime-enforced** — so (a) `recheck-narrowest` is
satisfied by *any* bound, including an absurd one, and (b) the orthogonal test
gate is structurally blind: no behaviour changes with the bound, so no test
can catch a hollow fix.  Per design §6.1, a rule whose gaming move no gate can
catch must **not** be put in the auto-fix loop — auto-applying a
model-selected bound or suppression on a BLOCK rule would normalise exactly
the gaming the gate exists to prevent.  P-series stays detection-only until a
gate exists that validates the bound (e.g. a runtime-enforced `MaxItems`, or a
payload-size behavioural check).

To implement later: add a P-series prescription to this file and a matching
`area == "prescriptions"` dispatch in `remediate-finding`, paired with a gate
that actually bites.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any `*.py` file under `scope` changes.

### Execution

```prose
# Detection only — route all findings to residue (see "Why deferred" above).
let violations = call detect-violations
  scope: scope
  series: "P"
  target: if mode == "strict" then "failing+warning" else "failing"

for each finding in violations:
  add finding to residue with note "P-series remediation deferred: suppress-only rule with no orthogonal gate (MaxItems is declarative); needs human review and a gate that validates the bound"
```
