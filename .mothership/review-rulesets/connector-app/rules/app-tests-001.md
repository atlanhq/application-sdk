---
schema: 2
id: APP-TESTS-001
level: L2
category: test-coverage
globs: []
severity: HIGH
suppressible: false
---
# Tests are not gamed to fit the code

The senior-reviewer question for EVERY modified test in the diff: would
the old test have failed on the new code — and is the new expectation
justified as CORRECT, or merely as MATCHING what the code now does?
Read the test diff before the code diff; a test bent to the code makes
the whole PR self-certifying.

Gaming shapes to flag (each is a finding on its own):

- An expected value updated to the new actual output with no stated
  reason the new output is right — the assertion diff mirrors the code
  diff. Justification must cite a source of truth (spec, vendor doc,
  fixture provenance), never the code itself.
- Golden files / snapshots re-recorded wholesale ("regenerate fixtures")
  without the new content itself being reviewed for correctness.
- Weakened assertions: exact → approx/contains/any-order/is-not-None,
  widened tolerances, fewer assertions than before.
- Mocks moved inward past the changed logic, so the change is never
  executed; mock return values widened until assertions are tautological.
- A new test that derives its expected value BY CALLING the code under
  test — it asserts the implementation, bugs included.
- Deleted edge-case tests or fixtures; new skip/xfail without a linked
  reason; a narrowed parametrize matrix.
- Conditional assertions (`if result: assert ...`) that silently pass on
  empty results.
- A regression test that does not fail without the fix is not a
  regression test.
