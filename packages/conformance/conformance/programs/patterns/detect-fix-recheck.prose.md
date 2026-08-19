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
- `rule_ids` — optional list of exact rule IDs to restrict the loop to, e.g.
  `["L004"]`.  Forwarded verbatim to every `detect-violations` call in the loop —
  including the end-of-round re-detect, so the loop's own convergence check and
  its oscillation fingerprint set stay scoped to the same rules it is fixing.  A
  loop scoped to one rule must not be declared un-converged because a different
  rule in the same series is still failing.  Omitted ⇒ every rule in `series`.
- `classification_override` — optional string.  When set (currently only
  `"unverifiable"`, by the P- and S-series areas), every result's
  `classification` is reported as this value regardless of what
  `remediate-finding` returns, and the finding is **always** added to residue.
  Exists so an area whose gates are structurally blind cannot emit a result that
  reads as gate-verified.  Never use it to *upgrade* a classification.
- `require_cited_evidence` — boolean, default `false`.  When `true`, a `fix`
  outcome is only accepted if `remediate-finding` returned a non-empty citation
  for the value it chose (a schema field, a documented upstream limit, a
  secret-store path).  A fix with no citation is reverted and residued as
  `"no cited evidence for the chosen value"` — for a blind-gate area an
  uncited value is a guess, and a guess that passes a blind gate is precisely
  the failure mode the gate cannot catch.
- `deliver_as_draft` — boolean, default `false`.  Stamped onto **every surviving
  result and every residue item** this loop emits, as `deliver_as_draft = true`,
  and surfaced as its own column in the residue report.  The consumer is the
  delivering caller — the remediation playbook's delivery stage passes
  `--draft` and requests a named reviewer whenever any delivered result carries
  the flag; the `/remediate` skill's residue phase prints it so a human
  applying proposals sees the requirement.  The loop itself never opens PRs, so
  carrying the flag on its outputs *is* the implementation at this layer: if it
  were dropped here, the guarantee "S-series ships as draft" would silently
  become unenforceable downstream.

### Delegation

```prose
let violations = call detect-violations
  scope: scope
  series: series
  rule_ids: rule_ids
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

      # Blind-gate areas (P, S) must not accept an uncited value.  Checked
      # BEFORE the edit is applied: an uncited fix is never written to the tree
      # at all, so there is nothing to revert and no window in which a guessed
      # value exists on disk.
      if require_cited_evidence and result.outcome == "fix" and not result.evidence:
        add finding to residue with note "no cited evidence for the chosen value — not applied"
        continue

      apply result.edit  # single-file text edit to finding.file, or (e.g. C002/C003) a multi-file command like `bootstrap`

      # touched_files defaults to [finding.file] for a normal single-file
      # textual edit. A multi-file fix (currently only C002/C003's `bootstrap`
      # invocation) sets it explicitly to every path it actually wrote, so a
      # revert below undoes the whole fix instead of leaving every file but
      # finding.file mutated in the tree.
      let touched = result.touched_files or [finding.file]

      let recheck = call recheck-narrowest
        scope: scope
        file: finding.file
        rule_id: finding.rule_id
        fingerprint: finding.fingerprint

      # Only run orthogonal gate for source-logic fixes; suppress = comment only.
      if result.outcome == "fix":
        let ortho = call orthogonal-gate scope: scope, finding: finding, touched_files: touched
        if not ortho.passed:
          revert result.edit from touched
          add finding to residue with note "orthogonal gate failed after fix"
          continue

      if not recheck.clear:
        revert result.edit from touched
        add finding to residue with note "recheck failed: finding still present after edit"
        continue

      # classification_override, when set by a blind-gate area, replaces the
      # model-reported classification on the surviving result. Applied here —
      # after both gates, before residue routing — so the value that reaches the
      # residue report and every downstream consumer is the area's, not the
      # model's. A result from a structurally-unverifiable area can therefore
      # never present itself as "mechanical".
      if classification_override:
        let result.classification = classification_override

      # finding.forces_external_influence is the structural, rule-level
      # guarantee (e.g. C001, always true); result.external_influence is
      # remediate-finding's own per-invocation report. ORing both means a
      # rule known ahead of time to always need human sign-off gets it even
      # if a single invocation's result omits the flag.
      # "unverifiable" is included for the same structural reason: its gates
      # passed but proved nothing, so it always needs a human to look.
      if result.outcome == "suppress"
        or result.classification == "judgment"
        or result.classification == "unverifiable"
        or result.external_influence
        or finding.forces_external_influence:
        add finding + result to residue for human review

  let next_violations = call detect-violations
    scope: scope
    series: series
    rule_ids: rule_ids
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
