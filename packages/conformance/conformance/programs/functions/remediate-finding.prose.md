---
kind: function
name: remediate-finding
description: >
  Proposes a source edit (or a justified inline suppression) for a single
  conformance finding.  The model is the worker here — it reads the finding's
  hint, classifies the fix, and emits an edit.  The deterministic re-check
  gate (recheck-narrowest) decides whether the edit worked.
---

### Parameters

- `finding` (object, required) — a finding as returned by `detect-violations`:
  `rule_id`, `area`, `file`, `line`, `column`, `message`, `hint`,
  `autofixable`, `disposition`, `fingerprint`.
- `mode` (string, required) — `"default"` or `"strict"`.  The `suppress`
  outcome is only available for WARNING-tier findings when mode is `"strict"`.

### Returns

- `outcome` — `"fix"` (source logic change) or `"suppress"` (inline ignore
  directive, strict mode only).
- `edit` — a description of the change to apply, including file path, the
  exact lines to change or insert, and the replacement text.
- `classification` — `"mechanical"` (deterministic, no judgment needed) or
  `"judgment"` (model made a non-trivial call; route to residue for human
  audit).
- `external_influence` — boolean; true if the model consulted any content
  outside the source file itself that could be attacker-influenced.  Always
  false for error-handling in this phase.  True for C001 (the replacement SHA
  is resolved from a live GitHub lookup); also wired for future dependency/CVE
  use.
- `not_remediable` — boolean; true when the area has no authored prescription
  yet (returns to residue without an edit attempt).

### Write-scope constraint

This function may **only** propose edits to Python source files and the root
`Dockerfile` — never to `tests/`, `.github/`, `conformance/`, or any CI / gate
configuration.  This is the §6.1 "no self-judging changes" discipline: the
remediator may not touch the gate it is judged against.  The `Dockerfile`
exception is limited to I-series findings; no other area may propose Dockerfile
edits.

For K-series findings, edits to `contract/*.pkl` files are also permitted.
The `pkl-eval` orthogonal gate verifies that the edited contract still compiles
and regenerates its artifacts correctly.

For C002 findings only, invoking `atlan-application-sdk-conformance bootstrap`
is also permitted, despite it writing under `.github/` and `.gitignore`. This
is not a carve-out of the no-self-judging discipline: the model never authors
or chooses the written content — `bootstrap` renders the same deterministic
template the C002 checker itself renders for comparison, so there is nothing
for the model to judge or game.

For C001 findings only, editing the `@<ref>` suffix of a single `uses:` line
in a `.github/workflows/*.yml` or `.github/actions/**/action.yml` file is also
permitted — and **only** that suffix: the action owner/repo/path and every
other line must be byte-for-byte unchanged. Unlike C002, the replacement
content here *is* model-obtained (a commit SHA resolved from a live GitHub
lookup), which is why C001's prescription always sets
`external_influence = true` — the fix is verified by recheck like any other,
but is unconditionally routed to residue for human sign-off before it merges,
per the `detect-fix-recheck` loop's existing `external_influence` handling.

No other rule or area may write to `.github/`, `tests/`, or `conformance/`.

### Dispatch by area

Route on `finding.area` to the matching area file and follow its
**Fix Prescription** section for rule-by-rule guidance.  Only load the
relevant area file — this is the progressive-disclosure boundary.

| `finding.area` | Phase | Area file |
|---|---|---|
| `error-handling` | PHASE 1 | `areas/error-handling.prose.md` |
| `optimizations` | PHASE 1 | `areas/optimizations.prose.md` |
| `prescriptions` | PHASE 1 (suggest-only) | `areas/prescriptions.prose.md` |
| `logging` | PHASE 2 | `areas/logging.prose.md` |
| `dependency` | PHASE 1 | `areas/dependency.prose.md` |
| `dockerfile` | PHASE 1 (suggest-only) | `areas/dockerfile.prose.md` |
| `deprecation` | PHASE 1 | `areas/deprecation.prose.md` |
| `tests` | PHASE 2 (strict-only) | `areas/tests.prose.md` |
| `ci` | PHASE 1 (partial) | `areas/ci.prose.md` — C002 (and C003's absent-file case) mechanical via `bootstrap`; C001 mechanical SHA-pin, always routed to residue (`external_influence`); C003 missing-entry and drifted `tests.yaml`/`renovate.json` `not_remediable = true` |
| `contract-toolkit` | PHASE 1 (strict-only; WARN-tier) | `areas/contract-toolkit.prose.md` |
| `security` | PHASE 1 (suggest-only) | `areas/security.prose.md` |
