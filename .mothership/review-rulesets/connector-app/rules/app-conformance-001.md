---
schema: 2
id: APP-CONFORMANCE-001
level: L2
category: correctness
globs: []
severity: MEDIUM
suppressible: false
---
# Use the conformance suite's results; never re-derive its checks

The conformance suite is the single source of truth for every
mechanically-checkable rule (~157 checks). BLOCK-tier failures already
gated CI before this review started. WARN-tier findings are visible but
non-blocking — surfacing them on changed code is THIS review's job.

- Run the suite once from the repo root (it takes seconds) and read the
  SARIF report:
  `python -m conformance.suite.runner --repo . --output /tmp/conformance.sarif`
- Report each WARNING whose location intersects the changed lines as a
  finding citing this rule, naming the conformance rule id and message in
  the evidence. Warnings on untouched code are out of scope.
- Judge new suppressions: a diff that adds a conformance suppression, a
  `# noqa`, or weakens a check needs a stated reason (see APP-GATES-001).
- NEVER re-derive, restate, or second-guess a conformance check by hand —
  the suite's definition wins. If you believe a check is wrong, report
  that as a finding against the suppression/config, not a competing
  definition.
