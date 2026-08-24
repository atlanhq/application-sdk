---
schema: 2
id: APP-TESTS-002
level: L2
category: test-coverage
globs: []
severity: HIGH
suppressible: false
---
# The right test at the right tier

The conformance suite checks that test suites EXIST per tier. This rule
owns whether THIS diff's change is covered at the RIGHT tier. Never write
a generic "add tests" comment — name the tier and the unexercised path.

Tier formula for connector apps:

- **Unit** — pure logic, offline, I/O mocked: transform/mapping output,
  SQL/statement builders, chunking math, error classification.
  Example: a mapping change MUST change the golden fixture for that
  entity in the same PR; an error-classifier change MUST simulate the
  failure and assert BOTH verdicts (retryable, degradable).
- **Integration** — real component, fake source: real DuckDB/parquet/
  filesystem, recorded HTTP at the client seam, workflow logic in the
  test environment, and config through the REAL resolution path.
  Example: a new config flag with on/off branches needs one integration
  test per branch through real resolution — unit tests that mock the
  config reader prove nothing about the path production takes (env
  layering has silently overridden explicit inputs before).
- **E2E / app-harness** — real source through the SDK-scaffolded DAG
  harness: reserved for changes to crawl orchestration, preflight
  checks, or publish artifacts. Example: a new preflight check needs the
  harness to prove the check and the real operation agree on one oracle.

Delta coupling:

- A change to product behavior or downstream output with NO test change
  in the same PR is a finding — state which tier is missing and which
  execution path goes unexercised.
- Branch coverage beats line coverage: an on/off path counts as covered
  only when BOTH branches execute somewhere real.
- Boundary shapes over scale: assert 0 rows, 1 row, the chunk boundary
  N/N+1, the empty page carrying a next-page token. Fixtures must
  resemble real source shapes — a fixture that dodges a known failure
  shape (all-null columns, mixed dtypes) is vacuous at any tier.

Safe paths (do not flag): a pure refactor whose behavior is pinned by
existing tests (the PR says so); docs/comment-only changes; a change an
existing test already fails without (point to that test). Do not demand
E2E for changes the unit/integration tiers fully exercise — E2E is
credential-gated and expensive.
