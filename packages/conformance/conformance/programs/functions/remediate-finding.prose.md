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
  `autofixable`, `disposition`, `fingerprint`, `forces_external_influence`,
  `canonical_reference`.  `autofixable` is the rule's classification —
  `true` means an *auto-fixable* rule the lane may apply a prescription for,
  `false` means a *migration* rule that is never applied by this function
  (see *Reference apps, impact analysis and verification* below).
  `canonical_reference` names the reference-app file that already has the
  compliant shape; it is mandatory reading before any edit.
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
- `evidence` — optional list of citation strings for any *chosen value* the fix
  contains: where the number, name, or path came from.  Each entry names a
  checkable source — a repo-relative `path:line`, a contract schema field
  (`contract/app.pkl: connection.max_items`), or a documented upstream limit
  with its identifier.  Empty/omitted for fixes that choose nothing (a rename,
  an added kwarg, a prescribed rewrite).  **Mandatory for the blind-gate areas
  under `apply_unverifiable`**: P-series must cite the source of a bound and
  S-series the secret-store path or env-var NAME being referenced (never a
  value), because `detect-fix-recheck`'s `require_cited_evidence` rejects a
  `fix` outcome whose `evidence` is empty *before the edit is applied* — for an
  area whose gates cannot validate the value, an uncited choice is a guess, and
  a guess that passes a blind gate is precisely what the gate cannot catch.
  Free-text rationale is NOT evidence; every entry must point at something a
  reviewer can open.
- `touched_files` — optional list of repo-relative paths the edit actually
  wrote. Defaults to `[finding.file]` when omitted, which covers every
  single-file textual edit (the overwhelming majority of fixes). Set
  explicitly for any fix that writes more than one file — C002/C003's
  `bootstrap` invocation, and K003/K004's `pkl` regeneration (see the
  write-scope note below and `areas/contract-toolkit.prose.md`'s K003/K004
  procedures for how each is populated deterministically). `detect-fix-recheck`
  reverts exactly this file set if the fix fails its gates, so a multi-file fix
  that is later reverted doesn't leave unrelated files mutated in the tree.
- `impact` — string, **mandatory for every `fix` outcome**.  The impact
  analysis performed before the edit was applied: which callers, importers,
  tests, contract/generated artifacts and config files were checked, which
  consequential edits were folded into this fix (and are therefore in
  `touched_files`), and which consequences fall outside the write scope and
  need a human (tests to adjust, a `.github/` file, an owner decision).  A
  reviewer must be able to read it and know the app is not left half-changed.
- `verification` — object, **mandatory for every `fix` outcome**, with four
  booleans: `finding_cleared` (rule-scoped re-detect no longer reports the
  fingerprint), `gate_passed` (the rule's orthogonal gate passed),
  `no_new_findings` (a whole-series re-detect on `touched_files` introduced no
  finding for any rule that was not there before) and `matches_reference` (the
  fixed site now has the shape of the file `finding.canonical_reference`
  names).  All four must be `true` for `outcome = "fix"`; otherwise revert and
  route to residue naming the check that failed.
- `migration_brief` — string, set together with `not_remediable = true` for
  every finding whose rule has `autofixable = false` (a *migration* rule).
  What the target state looks like in the reference app (file and symbol),
  which files in this app would have to change, the external skill or
  migration guide to run when the rule names one, and any owner decision the
  migration needs.  The loop carries it into residue verbatim so the
  connector's migration sub-issue starts from it rather than from nothing.

### Write-scope constraint

This function may **only** propose edits to Python source files and the root
`Dockerfile` — never to `tests/`, `.github/`, `conformance/`, or any CI / gate
configuration.  This is the §6.1 "no self-judging changes" discipline: the
remediator may not touch the gate it is judged against.  The `Dockerfile`
exception is limited to I-series findings; no other area may propose Dockerfile
edits.

For K-series findings, edits to `contract/*.pkl` files are also permitted.
The `pkl-eval` orthogonal gate verifies that the edited contract still compiles
and regenerates its artifacts correctly. K003/K004's own write footprint is
wider than `contract/*.pkl` — `pkl project resolve`/`pkl eval` also write
`contract/PklProject.deps.json` and every regenerated artifact under
`app/generated/`, `atlan.yaml`, or `app.yaml`; see `areas/contract-toolkit.prose.md`
for how `touched_files` accounts for that full set.

**K014 is the one K-series rule that may write `atlan.yaml` directly** — and only
after establishing, by running `pkl eval -m <tmpdir> contract/app.pkl` and
inspecting the output, that the contract does **not** emit `atlan.yaml`.  When the
contract does emit it, the file is a generated artifact and the general
never-hand-edit-a-generated-artifact rule applies unchanged: the edit goes in the
contract and `atlan.yaml` is rewritten by regeneration, not by this function.  See
`areas/contract-toolkit.prose.md`'s K014 procedure — the emission test is
mandatory, because neither the presence of `contract/app.pkl` nor the contract's
`amends` line reliably predicts the answer.

For C002 findings (and C003's absent-`.gitignore` case) only, invoking
`atlan-application-sdk-conformance bootstrap` is also permitted, despite it
writing under `.github/` and `.gitignore`. This is not a carve-out of the
no-self-judging discipline: the model never authors or chooses the written
content — `bootstrap` renders the same deterministic template the C002
checker itself renders for comparison, so there is nothing for the model to
judge or game.

`bootstrap`'s actual write footprint is wider than `.github/`/`.gitignore` and
must be accounted for whenever it is invoked as part of a C002 (or C003
absent-file) fix. Invoke it with `--json` and set `touched_files` to the
`touched` array of the trailing JSON line it prints (after all of its normal
human-readable output) — e.g.
`atlan-application-sdk-conformance bootstrap --json`, then
`json.loads(stdout.strip().splitlines()[-1])["touched"]`. This is genuinely
deterministic: `touched` is built by the CLI's own Python code from each
write's actual outcome, not by the model classifying which of the
`scaffolded:`/`installed:`/`updated:`/`backed up:`/`ok (...)` human-readable
lines above it means what — the same information the prefixes convey, but as
a structured value the model reads rather than one it has to parse. It is
what lets `detect-fix-recheck` revert the *entire* fix, not just
`finding.file`, if the gates below reject it. A path in the JSON line's
`unchanged` array was left alone and is not part of `touched_files`. (The
`backed up:` write only happens if a prior invocation passed `--enforce` or
`--renovate-automerge` explicitly and `renovate.json` had non-canonical
content, writing a `renovate.json.bak` that appears in `touched` alongside
`renovate.json` — not reachable via the no-flags procedure below, but real if
this function is ever invoked with an explicit `--enforce` or
`--renovate-automerge`.)

- It **always overwrites** `.claude/skills/remediate/SKILL.md` in consumer
  app repos — the very document driving this remediation loop — on every
  invocation (captured in `touched_files` like any other managed file). This
  is the same deterministic-re-sync argument as above (the model does not
  author SKILL.md's content, `bootstrap` renders it), but it is called out
  explicitly here so a reviewer auditing a C002 fix isn't surprised to see
  SKILL.md touched by a change that was nominally about a CI workflow file.
  **Exception: inside the `atlan-application-sdk-conformance` repo itself**
  (detected by `packages/conformance/pyproject.toml` naming that exact
  package — not merely a `packages/conformance/` directory existing, which a
  consumer monorepo could contain coincidentally and silently trip the
  guard), `bootstrap` skips this write entirely: `.claude/skills/remediate/
  SKILL.md` there is hand-maintained prose (this very file's sibling), not
  generated template output, so overwriting it would destroy human-authored
  content rather than re-sync a deterministic template. This guard is
  enforced in code (`main` in `conformance/bootstrap/command.py`), not just
  documented here — the same invocation's JSON line reports
  `"skipped": true` and empty `touched`/`unchanged` arrays in that case.
- It **write-if-absent scaffolds** `contract_schema.lock.json` (a B-series
  entrypoint-contract ledger baseline) whenever that file does not already
  exist — unrelated to the C-series finding being fixed. Whether this
  invocation created it is determined the same structural way: if
  `contract_schema.lock.json` appears in the JSON line's `touched` array (as
  opposed to `unchanged` or absent entirely, which mean it already existed),
  add a residue entry noting a new B-series baseline was established and
  needs human review — it was not produced to satisfy any C-series finding
  and must not be silently folded into the C002 fix's own outcome.

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
This is also enforced structurally, not solely by the model remembering to
set the field: C001's `RuleDefinition.forces_external_influence = true` (see
`suite/schema/catalog.py`) surfaces as `finding.forces_external_influence`,
which `detect-fix-recheck` ORs into the same residue condition — so a single
invocation that omits `external_influence` still can't skip human review for
this rule.

No other rule or area may write to `.github/`, `tests/`, or `conformance/`.

### Reference apps, impact analysis and verification

_Applies to every finding in every area, before and after the area
prescription below.  The model executing this function may be a small one; it
must not fix from memory, and it must not declare a fix done because the edit
compiled._

**1. Load the reference app before proposing anything.**

Every app-facing rule names a `canonical_reference`: a concrete file — and
usually a symbol — in one of the four maintained reference apps that already
has the compliant shape.  It arrives on the finding as
`finding.canonical_reference` (SARIF `atlan/canonicalReference`).  Only these
four apps count; an arbitrary connector may be mid-migration and is not a
model of anything:

| App | What it is the reference for |
|---|---|
| `atlan-mysql-app` | SQL-style connectors: extraction, transform templates, the contract and generated tree, SDR |
| `atlan-metabase-app` | API-style / BI connectors: pagination, typed clients, asset mapping |
| `atlan-openapi-app` | packaging and tooling baseline: `pyproject.toml`, pyright, ruff, CI shims |
| `atlan-hello-world-app` | the minimal skeleton: what an app needs and nothing else |

- Have the **full checkout** of all four available under `remediation/refs/`
  at `origin/main` for the whole run — the named file is the entry point, but
  the fix must mirror how the reference app does the pattern *everywhere*, and
  cross-references (a contract field, a generated artifact, a test fixture)
  resolve only inside a complete tree:

  ```sh
  mkdir -p remediation/refs
  for app in atlan-mysql-app atlan-metabase-app atlan-openapi-app atlan-hello-world-app; do
    [ -d "remediation/refs/$app" ] || git clone --depth 1 "https://github.com/atlanhq/$app.git" "remediation/refs/$app"
  done
  ```

  `remediation/refs/` is scratch.  It is never part of an edit, never appears
  in `touched_files`, and is never committed.
- Open the file `finding.canonical_reference` names and read the **whole
  file**, not the one line.  Then grep that reference app for the same pattern
  the rule is about — the SDK symbol, decorator, config key, contract field or
  log call — so the fix mirrors how the reference app does it consistently.
- **Mirror the reference.**  Do not invent an API, keyword argument, config
  key or import the reference app does not use.  The finding's `hint` and the
  area prescription say *what to change*; the reference app says *what the
  result must look like*.  The app named in `canonical_reference` is
  authoritative for that rule; the other three are for cross-checking only.
- If the reference app itself does not exhibit the pattern (or has an open
  finding for the same rule), do not guess: say so in `impact`, set
  `classification = "judgment"` and route to residue.

**2. Analyse the impact before applying.**

A one-line fix can break the app somewhere else.  Before `apply`, enumerate —
with grep across the **whole repo under scan**, not just `finding.file`:

- every caller, importer or subclass of a symbol the edit renames, removes or
  retypes;
- every test under `tests/` that pins the old behaviour (this function may not
  edit `tests/`, but it must list them: the orthogonal gate will run them);
- the contract and generated tree — `contract/**/*.pkl`, `app/generated/**`,
  `atlan.yaml`, `artifact_schemas.json`, `contract_schema.lock.json` — whenever
  the edit touches an `Input`/`Output` contract, an entrypoint name or the app
  identity;
- `pyproject.toml` and `uv.lock` whenever a dependency or extra changes;
- `.env.example`, `README.md` and docs that spell the value being changed.

Fold every consequential change that is inside the write scope into the same
edit and list it in `touched_files`.  Put everything outside the write scope
into `impact` so the reviewer — or the connector's per-rule sub-issue — picks
it up.  Never leave the app half-migrated between two states.

**3. Verify after applying.**

The loop's gates run regardless; this function reports what it verified so a
reviewer can see the evidence rather than trust the outcome:

- `finding_cleared` — `recheck-narrowest` no longer reports the fingerprint;
- `gate_passed` — the rule's orthogonal gate (tests / pkl-eval / docker-build)
  passed;
- `no_new_findings` — a whole-series re-detect on `touched_files` reports no
  finding, for **any** rule, that was not present before the edit.  A fix that
  clears L001 by introducing L011 is not a fix;
- `matches_reference` — re-open `finding.canonical_reference` and compare: the
  fixed site now has the reference's shape.

Record the four in `verification`.  If any is `false`, do not report
`outcome = "fix"`: revert and route to residue naming the failing check.

**Auto-fixable vs migration.**

- `finding.autofixable == true` (an **auto-fixable** rule): follow the area
  prescription and steps 1–3 above.  The area's apply/suggest mode is
  unchanged by this section — the suggest-only areas (P, F, S, and I without
  `apply_unverifiable`) still propose rather than write, and the write-scope
  constraint still stands — so a T-series or C004 result is a fully worked
  proposal routed to residue, not an applied edit.
- `finding.autofixable == false` (a **migration** rule): apply nothing.  Do
  steps 1 and 2 anyway, then return `not_remediable = true` with a
  `migration_brief` — target state as the reference app implements it (file
  and symbol), the files in this app that would change, the external skill or
  guide to run when the rule names one, and the owner decision if there is
  one.  The brief is the deliverable; the loop carries it into residue.

### Dispatch by area

Route on `finding.area` to the matching area file and follow its
**Fix Prescription** section for rule-by-rule guidance.  Only load the
relevant area file — this is the progressive-disclosure boundary.

| `finding.area` | Phase | Area file |
|---|---|---|
| `error-handling` | PHASE 1 | `areas/error-handling.prose.md` |
| `optimizations` | PHASE 1 | `areas/optimizations.prose.md` |
| `prescriptions` | PHASE 1 (suggest-only) | `areas/prescriptions.prose.md` |
| `preflight` | PHASE 1 (suggest-only) | `areas/preflight.prose.md` |
| `logging` | PHASE 2 | `areas/logging.prose.md` |
| `dependency` | PHASE 1 | `areas/dependency.prose.md` |
| `dockerfile` | PHASE 1 (suggest-only) | `areas/dockerfile.prose.md` |
| `deprecation` | PHASE 1 | `areas/deprecation.prose.md` |
| `tests` | PHASE 2 (strict-only) | `areas/tests.prose.md` |
| `ci` | PHASE 1 (partial) | `areas/ci.prose.md` — C002 (and C003's absent-file case) mechanical via `bootstrap`; C001 mechanical SHA-pin, always routed to residue (`external_influence`); C003 missing-entry and drifted `tests.yaml`/`renovate.json` `not_remediable = true` |
| `contract-toolkit` | PHASE 1 (strict-only; WARN-tier) | `areas/contract-toolkit.prose.md` |
| `security` | PHASE 1 (suggest-only) | `areas/security.prose.md` |
