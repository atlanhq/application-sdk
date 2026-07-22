---
kind: function
name: pkl-eval-gate
description: >
  Runs `pkl eval` on the app's contract directory as the orthogonal gate after a
  contract source fix.  Verifies that the migrated contract still compiles and that
  the committed `app/generated/` + `atlan.yaml` artifacts regenerate cleanly.  This
  gate is the contract-toolkit equivalent of the test-suite orthogonal gate — the
  remediator may not hand-edit the generated artifacts it is judged against.
---

### Parameters

- `scope` (string, required) — repository root path.

### Returns

- `passed` (boolean) — true if `pkl eval` exits 0 and the regenerated artifacts
  match the content produced by the migration edit.
- `exit_code` (integer) — raw exit code from `pkl eval`.
- `summary` (string) — last ~20 lines of combined stdout/stderr, for diagnostic
  context in the residue report.
- `pkl_absent` (boolean) — true if the `pkl` CLI was not found on `PATH`.  When
  true, `passed` is always false and the gate reason is `cannot-verify`.

### Implementation

First, check that `pkl` is available:

```sh
which pkl 2>/dev/null || echo "pkl-absent"
```

If `pkl` is absent (or the poe wrapper `uv run poe generate` is unavailable), set
`pkl_absent = true`, `passed = false`, and return immediately.  **Never claim
success when the gate cannot run — always revert + residue on `pkl_absent`.**

Otherwise, run the evaluation from `scope`:

```sh
cd <scope>
pkl eval -m . contract/app.pkl 2>&1
```

Capture exit code and the last 20 lines of combined output.

- exit 0 → `passed = true`.  The regenerated artifacts (`app/generated/**`,
  `atlan.yaml`) are now part of the fix and should be staged alongside the
  `contract/app.pkl` change.
- exit ≠ 0 → `passed = false`.  Revert both the `contract/app.pkl` edit **and**
  any regenerated artifacts touched since the edit began.

Note: this function is skipped for `suppress` outcomes (inline `// conformance:
ignore` directives are comment-only changes; `pkl eval` should already pass).
