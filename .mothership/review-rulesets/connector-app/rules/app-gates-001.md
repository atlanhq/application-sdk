---
schema: 2
id: APP-GATES-001
level: L2
category: security
globs: []
severity: HIGH
suppressible: false
---
# Changes to review and CI gates review themselves

A PR must not quietly weaken the checks that will judge it or its
successors. When the diff touches any gate surface, scrutinize it first:

- `.mothership/reviewer.yaml` (triggers, branches, required checks),
  `.mothership/review-rulesets/**` (rule files, manifests, suppressions),
  conformance configuration, CI workflow definitions, or deploy-gate
  config in `atlan.yaml`.
- Weakening moves — a new suppression, a deleted or narrowed rule, a
  removed required check, a loosened trigger, a new lint/conformance
  exemption — MUST carry a stated reason in the PR. Absent that reason,
  the weakening itself is the finding.
- A new suppression entry must name its reason, owner, and expiry in the
  diff, not in a comment thread.
- Strengthening or additive changes are fine; note them and move on.
