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
- `touched_files` (list of strings, optional) — every path the applied fix
  actually wrote, as computed by the caller (`detect-fix-recheck`'s
  `result.touched_files or [finding.file]`).  Used by the `"skip"` gate to
  parse-check the full multi-file write, not just `finding.file`.  Falls
  back to `[finding.file]` if not passed.

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

- `"skip"` → skip the Python/contract test suite (it has no signal to offer for
  a `.github/`/`.gitignore`-only change) but still run a minimal
  **parseability check** over every touched non-Python file, because neither
  this gate nor `recheck-narrowest` otherwise verifies the file is still
  well-formed outside the one line the fix targeted — and for a rule whose
  fix is *not* unconditionally escalated to residue (C002/C003; unlike C001,
  which always routes to human sign-off via `external_influence`), a
  syntax-breaking rewrite would otherwise auto-accept with nothing catching
  it before GitHub Actions itself fails to parse the file post-merge.

  For each path in the `touched_files` parameter (or `[finding.file]` if not
  passed):
  - `.yaml`/`.yml` → parse with a YAML loader (e.g.
    `python -c "import sys,yaml; yaml.safe_load(open(sys.argv[1]))" <path>`).
  - `.json` → parse with a JSON loader.
  - any other extension (e.g. `.gitignore`, `.py` scripts) → no parse check;
    nothing to validate structurally.

  If every touched file with a checkable extension parses cleanly, return
  `passed = true`, `exit_code = 0`, `summary = "orthogonal_gate=skip —
  fix cannot affect Python or contract behaviour; N touched file(s)
  parse-checked"`. If any fails to parse, return `passed = false`,
  `exit_code = 1`, and `summary` naming the file and the parser error, so the
  loop reverts the fix and routes it to residue instead of auto-accepting a
  broken file.

  Named `"skip"` rather than a bare `"none"` string so it can't be confused
  with the field's own `None` default, which means the opposite thing (run
  the standard test suite).

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
