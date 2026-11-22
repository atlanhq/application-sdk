---
id: FP15
applies-to: [tests]
---
# A changed test still proves the behaviour

Principle: When a PR weakens or removes an expectation an existing test already made, the PR says
why the new expectation is the correct behaviour, not only that it matches what the code now does.
Why: A test edited to agree with the code proves nothing. The PR certifies itself, and the
regression the old test guarded against ships unnoticed.
Ask:
- Did an existing assertion get looser, change its expected value or disappear, including by a new skip, fewer parameter cases, a re-recorded golden file or a mock that now hides the changed code?
- If so, does the PR, a comment or the test give a reason from outside the code (a spec, the source's documentation, where a fixture came from)?
Don't flag: added tests, renames, refactors, moved or reformatted fixtures, tests rewritten with
the same or stronger expectations, and any change the PR explains. Conformance reports tests with
no assertion or one that cannot fail (`T005`, `T006`, `T007`) and whole files skipped (`T009`).
Severity: Medium by default. High only when a test that covers code changed in this PR was deleted
or skipped.
