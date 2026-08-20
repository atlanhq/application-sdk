---
schema: 2
id: APP-TESTS-001
level: L2
category: test-coverage
globs: []
severity: HIGH
provenance: application-sdk test conventions; connector app review practice
suppressible: false
---
# Changed behavior ships with honest tests

- Every behavior change carries a test that FAILS without the change. A bug
  fix without a regression test is incomplete.
- Tests must be non-vacuous: a test that mocks the system under test, or
  asserts only that a mock was called, proves nothing. Assert the failure
  case and the output shape, not just "no exception".
- MUST NOT modify or delete an existing test to make new code pass without
  an explicit stated reason in the PR — that is how guarantees silently
  disappear.
- Unit tests mock external I/O (source APIs, object store, LLMs); they do
  not hit live services.
- Indirect cases: a widened mock return that makes an existing assertion
  tautological; a deleted edge-case fixture; `pytest.mark.skip` added
  without a linked reason.
