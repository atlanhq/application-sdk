---
kind: function
name: orthogonal-gate
description: >
  Runs the orthogonal gate after a source-code fix.  This is the gate the
  remediation layer is NOT allowed to edit — running it detects fixes that merely
  silence the linter while breaking observable behaviour.  Dispatches to the
  appropriate gate implementation based on the rule's `orthogonal_gate` field.
---

### Parameters

- `scope` (string, required) — repository root path.
- `finding` (object, required) — the finding being gated, as returned by
  `detect-violations`.  Used to read `finding.orthogonal_gate` for dispatch.

### Returns

- `passed` (boolean) — true if the gate exits 0.
- `exit_code` (integer) — raw exit code from the gate runner.
- `summary` (string) — last ~20 lines of combined stdout/stderr, for
  diagnostic context in the residue report.

### Implementation

Dispatch on the rule's `orthogonal_gate` value (read from
`get_rule(finding.rule_id).orthogonal_gate` or from the `orthogonal_gate` field
of the finding as returned by `detect-violations`):

- `"tests"` (or null/default) → run the repository's standard test command:

  ```sh
  uv run coverage run -m pytest --import-mode=importlib --capture=no --log-cli-level=INFO tests/ -v 2>&1
  ```

  Capture exit code and the last 20 lines of combined output.  Return `passed =
  (exit_code == 0)`.

- `"pkl-eval"` → delegate to `pkl-eval-gate` with `scope = scope`.  Return its
  `passed`, `exit_code`, and `summary` directly.

- `"none"` → skip execution entirely; return `passed = true`, `exit_code = 0`,
  and `summary = "orthogonal_gate=none — fix cannot affect Python or contract behaviour, skipped"`.
  Used by rules whose only remediable fixes are deterministic re-syncs of
  managed non-Python files (e.g. C001/C002/C003's fixes) — running the test
  suite after a `.github/` or `.gitignore` change has no signal to offer.

- **any other value** → fail closed: emit `passed = false`, `exit_code = -1`,
  and `summary = "Unknown orthogonal_gate value: <value> — revert the edit and
  route to residue.  The rule definition contains a typo; do not proceed."`.
  Never fall through to the tests branch on an unrecognised gate name.

Do not attempt to interpret or fix test/eval failures — that is a separate facet's
job.  Simply report the result so the loop can decide to revert the triggering
edit.

Note: this function is skipped for `suppress` outcomes (when `remediate-finding`
emits an inline `# conformance: ignore` directive rather than changing logic).
No tests should break from a comment-only change; skipping the gate here is
safe and saves test-suite execution time on every WARNING suppression.
