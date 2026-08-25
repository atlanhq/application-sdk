---
schema: 2
id: APP-TESTS-004
level: L2
category: test-coverage
globs: []
severity: MEDIUM
suppressible: true
---
# E2E / harness tier: real source, real pipeline

E2E runs the SDK-scaffolded DAG harness against a real (or recorded)
source with credentials. It is expensive and credential-gated — require
it precisely, not reflexively.

Require an E2E/harness change ONLY when the diff touches:

- Crawl orchestration (workflow DAG shape, entrypoint wiring, run
  sequencing);
- A preflight check or its paired operation — the harness must prove
  both use the same oracle against the same credential;
- Publish artifacts or their layout (what downstream diffing consumes);
- The setup/credential flow the platform walks (auth, connection
  construction).

For everything else, unit + integration coverage is sufficient — say so
instead of demanding E2E. When E2E applies but credentials are absent,
the accepted evidence is a recorded-fixture harness run plus a stated
plan; a skipped E2E with no reason is a finding.
