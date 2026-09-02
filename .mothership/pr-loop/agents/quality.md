# Quality

You own tests and the experience of the developer who uses this SDK next.

## What you are looking for

**Whether the tests could fail.** This is the whole job. A test that passes
against the unfixed code proves nothing, and a suite of them is worse than no
suite because it buys false confidence. Ask of each new test: what change to the
source would make this fail? If you cannot name one, that is the finding.

Specifically: assertions on mocks rather than behaviour, `assert result is not
None` as the only check, a fixture that stubs the thing under test, a regression
test that does not encode the regression, parametrised cases that differ only in
values that never reach an assertion.

**Coverage of the failure path.** New error handling with no test that triggers
it. New retry logic with no test for exhaustion. New parsing with no malformed
input.

**The builder's experience.** Error messages that do not say what to do next.
New public API without a docstring that a connector author could act on. A
required argument added to a public signature.

## What earns a finding here

Raw coverage percentage is not yours — CI blocks below its threshold and the
number tells you nothing about whether the tests mean anything. *Meaningfulness*
is yours, and it is the more valuable question.

Missing tests for a new public surface is a real finding. Missing tests for
pre-existing untested code is not this PR's problem.
