---
schema: 2
id: APP-TESTS-001
level: L2
category: test-coverage
globs: []
severity: HIGH
suppressible: false
---
# Changed behavior ships with honest tests

The conformance suite catches structurally empty tests (no assertions,
empty bodies, tautological literals, unconditional skips). This rule owns
semantic honesty — tests that LOOK real but prove nothing:

- Every behavior change carries a test that FAILS without the change; a
  bug fix without a regression test is incomplete.
- A test that mocks the system under test, or asserts only that a mock
  was called, proves nothing — assert the failure case and the output
  shape.
- MUST NOT modify or delete an existing test to make new code pass
  without an explicit stated reason in the PR — that is how guarantees
  silently disappear.
- Indirect cases: a widened mock return that makes an existing assertion
  tautological; a deleted edge-case fixture; a new skip whose reason
  doesn't hold.
