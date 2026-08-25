---
schema: 2
id: APP-TESTS-002
level: L2
category: test-coverage
globs: []
severity: HIGH
suppressible: false
---
# Unit tier: changed logic means changed unit tests

Unit tests own pure logic, offline, with I/O mocked only at the seam:
transform/mapping output, SQL and statement builders, chunking math,
error classification, qualified-name construction.

- Delta coupling: a change to product behavior or downstream output with
  no unit-test change in the same PR is a finding — name the function
  and the unexercised path. A mapping change MUST change that entity's
  golden fixture in the same PR.
- An error-classification change MUST simulate the failure and assert
  BOTH verdicts (retryable, degradable) — not just that no exception
  escapes.
- Boundary shapes over row counts: 0 rows, 1 row, the chunk boundary
  N/N+1, the empty page that still carries a next-page token.
- Fixture honesty: fixtures resemble real source shapes. A fixture that
  dodges a known failure shape (all-null columns, mixed dtypes, unicode
  identifiers) is vacuous regardless of assertions.
- Safe paths: a pure refactor whose behavior is pinned by existing tests
  (the PR says so); a change an existing test already fails without
  (point to it).
