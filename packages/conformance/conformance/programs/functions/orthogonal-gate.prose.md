---
kind: function
name: orthogonal-gate
description: >
  Runs the test suite as the orthogonal gate after a source-code fix.  This is
  the gate the remediation layer is NOT allowed to edit — running it detects
  fixes that merely silence the linter while breaking observable behaviour.
---

### Parameters

- `scope` (string, required) — repository root path.

### Returns

- `passed` (boolean) — true if the test suite exits 0.
- `exit_code` (integer) — raw exit code from the test runner.
- `summary` (string) — last ~20 lines of combined stdout/stderr, for
  diagnostic context in the residue report.

### Implementation

Run the repository's standard test command from `scope`:

```sh
uv run poe test 2>&1
```

Capture exit code and the last 20 lines of combined output.  Return `passed =
(exit_code == 0)`.

Do not attempt to interpret or fix test failures — that is a separate facet's
job.  Simply report the result so the loop can decide to revert the triggering
edit.

Note: this function is skipped for `suppress` outcomes (when `remediate-finding`
emits an inline `# conformance: ignore` directive rather than changing logic).
No tests should break from a comment-only change; skipping the gate here is
safe and saves test-suite execution time on every WARNING suppression.
