---
kind: pattern
name: detect-fix-recheck
description: >
  The bounded gated remediation loop.  Area-agnostic — prescriptions live in
  the area responsibilities.  Expands into the calling responsibility at
  compile time.

  Loop invariant: every edit is verified by two deterministic gates before
  it survives (recheck-narrowest confirms the finding is gone; orthogonal-gate
  confirms existing tests still pass).  Neither gate may be edited by the
  remediator — the §6.1 "no self-judging changes" discipline is structural, not
  a policy promise.
---

### Parameters

- `scope` — repository root.
- `series` — comma-separated series letters to run.
- `mode` — `"default"` or `"strict"`.
- `max_attempts` — maximum loop iterations before freeze-and-escalate
  (default: 5).

### Delegation

```prose
let violations = call detect-violations
  scope: scope
  series: series
  target: if mode == "strict" then "failing+warning" else "failing"

let attempts = 0
let residue = []
let pre_fingerprints = fingerprints(violations)

loop until violations is empty or attempts >= max_attempts:

  # Batch findings per file — avoids thrashing one file in repeated passes
  # when interacting rules would cycle (design §6.2 batch-boundary discipline).
  for each file, file_findings in group(violations by file):

    for each finding in file_findings:

      let result = call remediate-finding
        finding: finding
        mode: mode

      if result.not_remediable:
        add finding to residue with note "not remediable in this phase"
        continue

      apply result.edit to finding.file

      let recheck = call recheck-narrowest
        scope: scope
        file: finding.file
        rule_id: finding.rule_id
        fingerprint: finding.fingerprint

      # Only run orthogonal gate for source-logic fixes; suppress = comment only.
      if result.outcome == "fix":
        let ortho = call orthogonal-gate scope: scope
        if not ortho.passed:
          revert result.edit from finding.file
          add finding to residue with note "orthogonal gate failed after fix"
          continue

      if not recheck.clear:
        revert result.edit from finding.file
        add finding to residue with note "recheck failed: finding still present after edit"
        continue

      if result.outcome == "suppress"
        or result.classification == "judgment"
        or result.external_influence:
        add finding + result to residue for human review

  let next_violations = call detect-violations
    scope: scope
    series: series
    target: if mode == "strict" then "failing+warning" else "failing"

  # Oscillation detection: same fingerprint-set across rounds = loop is stuck.
  if fingerprints(next_violations) == pre_fingerprints and next_violations is not empty:
    escalate "oscillation detected — same violation set after full pass; freezing"

  let pre_fingerprints = fingerprints(next_violations)
  let violations = next_violations
  let attempts = attempts + 1

if violations is not empty:
  escalate "max attempts reached with %d violations remaining" % len(violations)

emit residue as structured report
  for each item in residue:
    - rule_id, file, line, fingerprint
    - proposed edit (if any)
    - classification and outcome
    - reason the item is in residue (judgment / suppression / recheck-failed / not-remediable)
```

### Notes

The `fingerprints(findings)` helper computes the frozenset of
`partialFingerprints["atlanConformance/v1"]` values across a findings list —
a set comparison (not order-sensitive) that reliably detects oscillation.

`escalate` in strict mode raises the condition to the top-level
`conformance-remediation` responsibility, which can decide to surface it to a
human or reduce the scope and retry.
