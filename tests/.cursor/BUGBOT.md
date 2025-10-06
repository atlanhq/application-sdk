# Testing Code Review Guidelines - Test Implementation Standards

## Context-Specific Patterns

This directory contains all test implementations for the Application SDK. Tests must be reliable, maintainable, and provide confidence in code correctness without being brittle.

### Phase 1: Critical Testing Safety Issues

**Test Reliability Requirements:**

- All tests must be deterministic - same code always produces same test results
- No tests should depend on external services (real databases, APIs, network)
- Tests must not have hidden dependencies on execution order
- No shared mutable state between test cases
- Tests must clean up any resources they create

**Test Data Safety:**

- No real customer data or production data in tests
- Test databases must be isolated and disposable
- Mock sensitive operations (email sending, payment processing)
- No hardcoded secrets or credentials in test code
- Test data must be anonymized and safe for version control

---

## Testing-Specific Anti-Patterns

**Always Reject:**

- Tests that call real external services
- Tests with non-deterministic behavior
- Tests that depend on execution order
- Shared mutable state between tests
- Tests without proper cleanup
- Overly complex test setup
- Tests that test implementation details instead of behavior
- Missing test documentation

---

## Educational Context for Test Reviews

When reviewing test code, emphasize:

1. **Reliability Impact**: "Flaky tests undermine confidence in the entire test suite. Tests that sometimes pass and sometimes fail train developers to ignore test failures, defeating the purpose of testing."

2. **Maintainability Impact**: "Tests are code that needs to be maintained. Overly complex test setup or brittle mocking makes tests harder to update when requirements change, slowing down development."

3. **Coverage vs Quality**: "High test coverage with poor test quality provides false confidence. Tests should verify behavior, not just exercise code paths."

4. **Feedback Speed**: "Fast, reliable tests enable rapid development cycles. Tests that take too long to run or require complex setup discourage developers from running them frequently."

5. **Documentation Value**: "Well-written tests serve as executable documentation of system behavior. They should clearly show how components are intended to work and what edge cases are handled."

6. **Mock Accuracy Impact**: "Incorrect mocking creates false confidence. Tests that mock non-existent methods or wrong signatures can pass while the real code fails. Always verify that mocks match actual implementations."

---