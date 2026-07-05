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

      # finding.forces_external_influence is the structural, rule-level
      # guarantee (e.g. C001, always true); result.external_influence is
      # remediate-finding's own per-invocation report. ORing both means a
      # rule known ahead of time to always need human sign-off gets it even
      # if a single invocation's result omits the flag.
      if result.outcome == "suppress"
        or result.classification == "judgment"
        or result.external_influence
        or finding.forces_external_influence:
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

# Group residue by root cause before emitting. The cluster key is rule_id:
# every finding of a rule shares one prescription and one rule rationale, so
# grouping co-locates them — the reviewer reads that shared context once
# instead of per row, and sees sibling sites side by side.
#
# Grouping does NOT collapse the group into one decision. Residue is where
# the *judgment* findings land, and same rule does not mean same disposition:
# an exception swallow may be correct as best-effort at one site and a latent
# bug at another; a stacktrace may be safe to log at one site and leak
# sensitive data at another. Each item stays an independent decision. What
# grouping buys is that an inconsistent call across similar sites becomes
# visible — so the reviewer can apply the same fix where it fits and
# consciously justify the exceptions, rather than silently diverge (or
# force-align sites that legitimately differ).
#
# The partition itself is purely mechanical (a group-by on an existing
# field): no model judgment, nothing to game, stable across runs.
#
# This is REPORT-only grouping. It deliberately does not touch how fixes are
# decided: each finding is still remediated on its own merits, in its own
# small context, and gated on its own (see the per-finding loop above). A
# cluster-*fix* pre-pass — one agent deciding a single edit for many sites at
# once — is intentionally NOT pursued here, and this grouping is not a step
# toward one. Per-site independence is a feature, not a limitation: it keeps
# fix-vs-suppress a per-site call (the right answer at one E013 site can be
# the opposite of another's), keeps each context small, and keeps model
# errors uncorrelated so recheck catches them one finding at a time. One
# large context deciding all N sites would instead risk a single
# wrong-but-fingerprint-clearing edit passing for the whole group — a
# correlated false-negative the gate can't catch. Do not read "grouped by
# root cause" as license to merge the decisions.
emit residue as structured report, grouped by root cause
  for each cluster in group(residue by rule_id), ordered by rule_id:
    - class header: rule_id, area, count of items in this cluster
    - the shared prescription and rule rationale, stated once (from the area
      prescription) — a starting point for each site, not a verdict for the group
    - for each item in cluster, ordered by file then line:
      - file, line, fingerprint
      - proposed edit (if any)
      - classification and outcome
      - reason this item is in residue (judgment / suppression / recheck-failed
        / not-remediable) — items in one cluster may differ here, and each is
        decided on its own merits
```

### Notes

The `fingerprints(findings)` helper computes the frozenset of
`partialFingerprints["atlanConformance/v1"]` values across a findings list —
a set comparison (not order-sensitive) that reliably detects oscillation.

`escalate` in strict mode raises the condition to the top-level
`conformance-remediation` responsibility, which can decide to surface it to a
human or reduce the scope and retry.
