---
name: connector-review
description: Review the current connector change before commit against applicable L2-L4 rules.
---

# Connector review

Review only the current diff. Rule files are the authority; do not restate them.

1. Scope: use `git diff HEAD --name-only` and `git diff --cached --name-only`.
   If both are empty, use `git diff origin/main...HEAD --name-only`.
2. Run `scripts/fetch-review-rules.sh`. Read matching L2/L4 rules from
   `.mothership/.cache/review-rulesets/` and L3 rules from
   `.mothership/review-rulesets/connector-app/`. If the shared cache is unavailable,
   state that L2/L4 were not reviewed.
3. For each selected rule, report `checked`, `not_applicable`, or a finding. Then do
   one independent correctness pass over the changed code.
4. L1 conformance runs in CI on every pull-request update. Do not run L1 locally or
   write a review marker. CI enforcement remains repository-specific.
5. Report findings with evidence and a concrete fix. This local review is guidance;
   it never blocks a commit through a self-written PASS marker.
