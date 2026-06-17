---
kind: responsibility
name: prescriptions-area
description: >
  Maintains the current P-series violation-set and drives SUGGEST-ONLY
  remediation: for each finding the model drafts a proposed fix, but the
  proposal is recorded for human review and never auto-applied — because
  P-series rules currently have no orthogonal gate that can validate a fix.
---

### Maintains

The current set of unsuppressed P-series (prescription) conformance findings
in the working tree, as reported by `suite.runner --series P`, each paired
with a model-drafted **proposed** fix for human review.

#### violations-prescriptions

The fingerprint-set of all unsuppressed FAILING P-series results.  Extends to
include WARNING results in strict mode.

Postcondition (suggest-only — the loop proposes but does not apply):

> Every P-series finding routes to the residue report with a drafted fix
> attached.  The working tree is left unchanged by this area; a human reviews
> each proposal and applies it (or rejects it) manually.  The deterministic
> `suite.runner --series P` exit code is therefore unchanged by this area —
> only humans clear P-series findings.

**Why suggest-only, not auto-applied (not an oversight):** P001
`UnboundedContractFields` is suppress-only, and its only fix that clears the
detector is adding `Annotated[..., MaxItems(N)]` or an inline suppression.
`MaxItems` is a **declarative marker — not runtime-enforced** — so (a)
`recheck-narrowest` is satisfied by *any* bound, including an absurd one, and
(b) the orthogonal test gate is structurally blind: no behaviour changes with
the bound, so no test can catch a hollow fix.  Per design §6.1, a rule whose
gaming move no gate can catch must **not** be auto-applied — that would
normalise exactly the gaming the gate exists to prevent.  The safe form is
**propose, don't apply**: the model drafts a concrete diff, a human is the gate.
When a gate that validates the bound exists (a runtime-enforced `MaxItems`, or
a payload-size behavioural check), this area can graduate to the full
`detect-fix-recheck` loop.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any `*.py` file under `scope` changes.

### Execution

```prose
# Suggest-only: detect, draft a fix per finding, route to residue WITHOUT
# applying.  No gate can validate a P-series fix, so the human is the gate —
# this area never mutates the working tree (contrast detect-fix-recheck, which
# applies and keeps edits that pass their gates).
let violations = call detect-violations
  scope: scope
  series: "P"
  target: if mode == "strict" then "failing+warning" else "failing"

for each finding in violations:
  let proposal = call remediate-finding
    finding: finding
    mode: mode

  # The proposal is recorded, never applied.  classification is always
  # "judgment" for P-series, so it lands in the human-review residue.
  add { finding, proposal } to residue with note "P-series suggest-only: proposed fix drafted for human review; NOT applied (no orthogonal gate validates a MaxItems bound or suppression)"
```
