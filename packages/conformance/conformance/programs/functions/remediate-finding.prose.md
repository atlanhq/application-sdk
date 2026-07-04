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
- `touched_files` — optional list of repo-relative paths the edit actually
  wrote. Defaults to `[finding.file]` when omitted, which covers every
  single-file textual edit (the overwhelming majority of fixes). Set
  explicitly for any fix that writes more than one file — currently only
  C002/C003's `bootstrap` invocation (see the write-scope note below for how
  it is populated deterministically). `detect-fix-recheck` reverts exactly
  this file set if the fix fails its gates, so a multi-file fix that is
  later reverted doesn't leave unrelated files mutated in the tree.

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

For C002 findings (and C003's absent-`.gitignore` case) only, invoking
`atlan-application-sdk-conformance bootstrap` is also permitted, despite it
writing under `.github/` and `.gitignore`. This is not a carve-out of the
no-self-judging discipline: the model never authors or chooses the written
content — `bootstrap` renders the same deterministic template the C002
checker itself renders for comparison, so there is nothing for the model to
judge or game.

`bootstrap`'s actual write footprint is wider than `.github/`/`.gitignore` and
must be accounted for whenever it is invoked as part of a C002 (or C003
absent-file) fix. Set `touched_files` to every path `bootstrap` printed with
a `scaffolded:`, `installed:`, or `updated:` prefix in this invocation's own
stdout — this is deterministic (read directly off the CLI's own output),
never model-judged, and it is what lets `detect-fix-recheck` revert the
*entire* fix, not just `finding.file`, if the gates below reject it. A path
printed with an `ok (...)` prefix was left unchanged and is not part of
`touched_files`.

- It **always overwrites** `.claude/skills/remediate/SKILL.md` in consumer
  app repos — the very document driving this remediation loop — on every
  invocation (captured in `touched_files` like any other managed file). This
  is the same deterministic-re-sync argument as above (the model does not
  author SKILL.md's content, `bootstrap` renders it), but it is called out
  explicitly here so a reviewer auditing a C002 fix isn't surprised to see
  SKILL.md touched by a change that was nominally about a CI workflow file.
  **Exception: inside the `atlan-application-sdk-conformance` repo itself**
  (detected by the presence of `packages/conformance/` in the scan root — no
  consumer app repo has this directory, since they consume the package via
  pip rather than as an in-tree checkout), `bootstrap` skips this write
  entirely: `.claude/skills/remediate/SKILL.md` there is hand-maintained
  prose (this very file's sibling), not generated template output, so
  overwriting it would destroy human-authored content rather than re-sync a
  deterministic template. This guard is enforced in code
  (`_cmd_bootstrap` in `cli.py`), not just documented here.
- It **write-if-absent scaffolds** `contract_schema.lock.json` (a B-series
  entrypoint-contract ledger baseline) whenever that file does not already
  exist — unrelated to the C-series finding being fixed. Whether this
  invocation created it is determined the same deterministic way: if
  `contract_schema.lock.json` appears in this invocation's `scaffolded:`
  lines (as opposed to not appearing at all, which means it already
  existed), add a residue entry noting a new B-series baseline was
  established and needs human review — it was not produced to satisfy any
  C-series finding and must not be silently folded into the C002 fix's own
  outcome.

For C001 findings only, editing the `@<ref>` suffix of a single `uses:` line
in a `.github/workflows/*.yml`/`*.yaml` or `.github/actions/**/action.yml`/
`action.yaml` file is also permitted — and **only** that suffix: the action
owner/repo/path and every other line must be byte-for-byte unchanged. Unlike
C002, the replacement
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
