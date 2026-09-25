---
name: remediate
description: >
  Drive the conformance remediation loop: detect violations, propose and verify
  fixes, and emit a residue report for anything that needs human review.

  Runs the conformance suite (deterministic) to detect violations, uses the
  model to propose fixes, re-runs the suite to verify each fix, and loops until
  the gate is clean or the attempt cap is reached.  Never games its own gate —
  source fixes are verified by re-detection; logic fixes are also verified by
  the orthogonal test gate.

  Backed by the OpenProse program shipped in the
  atlan-application-sdk-conformance package (resolve it with `programs-dir`). Run
  with the OpenProse skill to use the full Reactor-ready contract semantics; or
  invoke the program directly via the instructions below.

argument-hint: "[--area error-handling|deprecation|dependency|prescriptions|optimizations|dockerfile|tests|logging|ci|contract-toolkit|security] [--rule L004[,E002]] [--strict] [--apply-unverifiable] [path]"

inputs:
  - name: rule
    description: >
      Comma-separated list of exact rule IDs to restrict the run to, e.g.
      "L004" or "L001,L011".  Threaded through as the program's `rule_ids`
      input and pushed down to the runner NATIVELY as `--rule <IDS>` whenever
      the pinned runner accepts the flag: the runner derives the series,
      executes only those modules, and scopes findings, the exit state and the
      emitted SARIF catalog to exactly these rules.  Support is probed, never
      inferred from a version string — on a runner that rejects `--rule`,
      `detect-violations` falls back to the narrowest `--series` plus a
      rule-id post-filter — never `--series L004`, which matches a series
      *letter* and silently activates zero checks.

      This is what makes one-rule-per-run possible, and it is also the only way
      to express "blocking tier first": tier is a **per-rule** property (D001
      and D009 are BLOCK inside an otherwise-WARN series), so it cannot be
      selected through the area/series axis at all.
    required: false
    default: ""
  - name: apply-unverifiable
    description: >
      When present, the prescriptions (P), dockerfile (I) and security (S)
      areas apply their fixes instead of only drafting them.

      For the **I-series** this is now a genuinely gated fix: every I rule
      carries `orthogonal_gate = "docker-build"`, which builds the touched
      Dockerfile, and the failure modes the area was originally worried about
      (layer ordering, entrypoint interactions, build- vs run-time env) are all
      build-time-visible.  When docker is unavailable the gate returns
      `passed = false`, so the fix reverts rather than passing by default.

      For the **P-, F- and S-series** the gates remain structurally blind — P001's
      `MaxItems` is a declarative marker no test can observe, no unit test can
      prove a preflight verdict is truthful for the real source, and no gate can
      prove a relocated credential still resolves.  Those results are therefore
      force-classified `unverifiable`, always routed to residue, and accepted
      only with a cited source for the chosen value; S-series additionally
      delivers as a draft with a named reviewer.  An uncited fix is never
      applied at all.

      Omitted (the default), all four areas behave byte-identically to before
      this input existed.
    required: false
    default: false
  - name: area
    description: >
      Comma-separated list of areas to remediate.  Defaults to every area the
      top-level program enables (error-handling, deprecation, dependency,
      prescriptions, preflight, optimizations, dockerfile, tests, logging,
      contract-toolkit, security; ci is partially remediated — C002 and
      C003's absent-file case are fixed mechanically via `bootstrap`; C001 is
      mechanically pinned (SHA-resolve + repin) but always escalated to
      residue for mandatory human sign-off — assisted, not autonomous,
      remediation; C003's missing-entry case and drifted `tests.yaml`/
      `renovate.json` still route to residue rather than being auto-applied —
      the two drifted scaffolds with a `bootstrap --resync` remedy quoted for
      the human, the rest with no fix attempted; security
      is suggest-only — every S-series finding is routed to residue with a
      drafted fix, never auto-applied).
      Example: --area deprecation
    required: false
    default: "error-handling,deprecation,dependency,prescriptions,optimizations,dockerfile,tests,logging,ci,contract-toolkit,security"
  - name: strict
    description: >
      When present, also remediates WARNING-tier findings.  Each WARNING is
      resolved by either a real fix or a justified inline suppression
      (# conformance: ignore[Exxx] <reason>).  Every suppression is routed to
      the residue report for human audit.
    required: false
    default: false
  - name: path
    description: >
      Repo-root-relative path prefix to restrict findings to, e.g.
      "application_sdk" or "tools/migrate_v3".  Applied as a post-filter on
      result URIs after the runner produces the full-repo report — the runner
      has no --include flag, so filtering is done on the parsed output.  When
      omitted, all findings in the repo are considered.
    required: false
    default: ""

outputs:
  - name: sarif_before
    description: SARIF report before remediation (written to remediation/runs/before.sarif).
  - name: sarif_after
    description: SARIF report after remediation (written to remediation/runs/after.sarif).
  - name: residue_report
    description: >
      Structured markdown report of findings that need human review, written
      to remediation/runs/residue.md.  Items from an area that requires draft
      delivery (S-series under --apply-unverifiable) carry deliver_as_draft in
      their own column, so a human applying proposals sees that anything
      delivered from them ships as a draft PR with a named reviewer.

gates:
  - deterministic_recheck: >
      Every fix is re-checked with suite.runner --series <area-series> before
      it survives.  If recheck fails, the edit is reverted.
  - orthogonal_gate: >
      Every source-logic fix is also verified by the test suite (uv run poe
      test).  If tests break, the edit is reverted.  Suppression-only edits
      skip this gate (comment-only changes cannot break tests), as do rules
      whose orthogonal_gate is declared "skip" (e.g. C001/C002/C003 — a
      .github/.gitignore change cannot affect Python behaviour) — "skip"
      still runs a minimal YAML/JSON parseability check over every touched
      file, so a syntax-breaking rewrite is caught and reverted rather than
      auto-accepted (see orthogonal-gate.prose.md).
  - no_self_judging: >
      The remediator never touches tests/, .github/, or conformance/ — the
      gates it is judged against.  This is structural: remediate-finding's
      write scope excludes these paths, with two narrow exceptions: C002's
      fix invokes `atlan-application-sdk-conformance bootstrap`, which writes
      deterministic template content the model never authors or chooses; and
      C001's fix rewrites only the `@<ref>` suffix of one `uses:` line to a
      GitHub-resolved SHA, which is why C001 always carries
      `external_influence = true` and is escalated to residue for mandatory
      human sign-off on a passing recheck (a failing recheck reverts and
      residues as "recheck failed" like any other rule, never reaching this
      branch).
---

# /remediate — Conformance Remediation Loop

## What it does

Runs an iterative, gated remediation loop over the conformance suite's findings:

0. **Prelude** — upgrade the pinned conformance package and re-sync the
   scaffolds it owns (including this skill file), then converge on a
   fixpoint before any detection runs.  One-shot, capped at two bootstrap
   invocations, unreachable from the loop below.  See Phase 0.
1. **Detect** — run `suite.runner --series <series>` to get the current SARIF
   report; collect FAILING (and, with `--strict`, WARNING) findings.
2. **Fix or suppress** — for each finding, propose an edit (fix or suppression
   directive) guided by the rule's `atlan/hint` and the area prescription in
   `$PROGRAMS/areas/<area>.prose.md` (where `PROGRAMS=$(uv run
   atlan-application-sdk-conformance programs-dir)`).
3. **Re-check (narrowest gate)** — re-run the suite scoped to the touched file;
   confirm the finding's fingerprint is gone.
4. **Orthogonal gate** — for source-logic fixes, run the test suite; if it
   fails, revert the edit.
5. **Loop** — repeat until the finding-set is empty, an oscillation is detected,
   or the attempt cap (5) is reached.
6. **Residue report** — emit a structured markdown report of everything that
   needs human attention.

## Usage

```
/remediate                              # all areas, default mode (FAILING only)
/remediate --strict                     # all areas, strict mode (FAILING + WARNING)
/remediate --area error-handling        # error-handling only
/remediate --area error-handling --strict
/remediate application_sdk              # restrict to application_sdk/ subtree only
/remediate --area error-handling application_sdk
/remediate --rule L004                  # exactly one rule (one-rule-per-run)
/remediate --rule L001,L011             # a specific set of rules
/remediate --area dockerfile --apply-unverifiable   # let I-series apply (docker-build gated)
/remediate --apply-unverifiable         # P/I/S apply instead of only proposing
```

**Rule argument**: restricts the run to exact rule IDs.  Pushed down to the
runner natively as `--rule <IDS>` whenever the pinned runner accepts the flag —
the SARIF comes back scoped to exactly these rules, one descriptor per requested
rule.  A runner that rejects the flag falls back to the narrowest `--series` plus
a rule-id post-filter inside `detect-violations`.  Because tier is per-rule and not per-series, `--rule` is
also the only way to express "the blocking rules first".

**Path argument**: a repo-root-relative path prefix that filters *which findings
are remediated*.  It does **not** change what the runner scans — the runner
always scans the whole repo.  Findings outside the prefix are left untouched.

## Modes

**Default** — remediates only FAILING (BLOCK-tier, gate-blocking) findings.

**Strict** (`--strict`) — also remediates WARNING (WARN-tier) findings.  Each
WARNING is cleared by either a real fix or a justified inline suppression.  Every
suppression is routed to the residue report for human audit.

## Headless / harness-driven mode

When a harness invokes this skill non-interactively (no shell, a hard time
budget, detection pre-executed), the caller states so in its prompt.  Honor
these adjustments — they replace the corresponding steps of the loop, and
nothing else changes:

- **The shell being disabled is policy, not an error.**  Never attempt to run
  a command.  Every command-shaped step is pre-handled by the caller: the
  detect step has already run (the caller names the SARIF path, typically in
  the repository root) and the findings ride the prompt.  Work from those.
- **Recheck is the caller's.**  The harness re-runs detection and every gate
  after the session ends — an in-session recheck is neither possible nor
  needed.  Make the edits, then end the session.
- **Pace yourself: edit early, edit incrementally.**  Begin applying fixes
  within your first few actions and fix each site as you inspect it.  Do NOT
  survey the whole repository before the first edit — headless sessions have
  a hard deadline, and analysis without edits is discarded at it.
- **Residue is a report, not a retry loop.**  A finding that genuinely cannot
  be fixed safely is skipped (last resort) and the caller accounts for it;
  do not burn the budget re-attempting it.
- All standing rules hold unchanged: never add a suppression outside strict
  mode, never touch `tests/`, `.github/`, or `conformance/`, never commit or
  push — leave changes in the working tree.

## Area status

The live program (`conformance-remediation.prose.md`, resolved from the installed
package via `programs-dir`) fans out to every area below; `remediate-finding`
dispatches each finding to its area prescription.

| Area | Series | Remediation | Notes |
|---|---|---|---|
| error-handling | E | ✅ Implemented | Mechanical (E005, E016) auto-fixed; judgment (E002, E013, others) modelled + routed to residue |
| deprecation | B | ✅ Implemented | B001 guided fix (incl. legacy transformer → asset-mapper, BLDX-1399); B003/B004 detect-only → residue |
| dependency | D | ✅ Implemented | Guided + mechanical fixes; judgment routed to residue |
| prescriptions | P | ✅ Suggest-only (applies under `--apply-unverifiable`) | Default: findings modelled + routed to residue. With `--apply-unverifiable`: applied through the full gated loop, but the gates are **blind** (`MaxItems` is a declarative marker no test can observe), so results are force-classified `unverifiable`, always routed to residue, and only accepted with a cited source for the bound |
| optimizations | O | ✅ Implemented | Below-the-bar recommendations |
| dockerfile | I | ✅ Suggest-only (applies **gated** under `--apply-unverifiable`) | Default: findings modelled + routed to residue. With `--apply-unverifiable`: applied through the full loop, verified by `orthogonal_gate = "docker-build"` — a real `docker build` of the touched Dockerfile. This is the gate whose absence was the stated reason the area was propose-only, so I-series fixes here are genuinely verified, not unverifiable. Docker unavailable ⇒ gate returns `passed = false` and the fix reverts (never a pass-by-default) |
| tests | T | ✅ Strict-only | WARNING-tier; strict mode |
| logging | L | ✅ Implemented | Mechanical (L004, L007, L015, L017, L020) auto-fixed; judgment (L001, L002, L005, others) modelled + routed to residue |
| ci | C | ✅ Partial | C002 (managed-file drift) and C003's absent-`.gitignore` case both mechanical via the same `bootstrap` re-sync, invoked directly for either finding. C001 (unpinned action) mechanical SHA-resolve + repin, always escalated to residue for sign-off (external lookup). C003 missing-entry and drifted `tests.yaml`/`renovate.json` → residue, quoting `bootstrap --resync` (preserves each file's recognized values — tests.yaml's params, renovate.json's auto-merge mode; since FND-604 it refuses outright rather than dropping a declaration it cannot carry forward, so read its output for a `skipped:` line. Still not auto-applied: hand comments and a changed value on a canonical key are replaced, and the `.bak` is gitignored, so that loss would be invisible in the diff) |
| contract-toolkit | K | ✅ Strict-only | K001/K002 guided migration to App.pkl; verified by pkl-eval gate |
| security | S | ✅ Suggest-only (applies under `--apply-unverifiable`) | Default: S001/S002 (hardcoded credential / raw env access) drafted as proposed fixes routed to residue for mandatory human sign-off — never auto-applied, since no orthogonal gate can confirm a secret-relocation fix resolves the same credential. With `--apply-unverifiable`: applied, but force-classified `unverifiable`, always residued, **delivered as a draft** with a named reviewer, and accepted only with a cited relocation target (secret-store path or env-var NAME). A secret **value** is never read into an edit, comment, fixture, commit message or PR body under either mode |

To add a new area prescription: author `<programs-dir>/areas/<name>.prose.md`
and add a dispatch branch to `<programs-dir>/functions/remediate-finding.prose.md`.
The `contract-toolkit` area is the first example of an area using
`orthogonal_gate="pkl-eval"` instead of `"tests"` — useful precedent for any
future area whose fixes are validated by regenerating derived artifacts rather
than by running the test suite.

## Execution instructions

First resolve the live programs directory (the contracts ship inside the
installed `atlan-application-sdk-conformance` package — the `remediation/programs/`
tree in the repo root is the design doc only):

```
PROGRAMS=$(uv run atlan-application-sdk-conformance programs-dir)
```

### Phase 0: Prelude — pull the suite and scaffolds forward

Pull the pinned suite and the on-disk scaffolds forward before detecting
anything. The two steps fix different things: the lock upgrade changes the
**programs and rule set** (`programs-dir` resolves inside the installed
package), the bootstrap changes the **on-disk scaffolds** (this SKILL.md, the
managed workflow shims, the vendored detect action, tests.yaml, renovate.json).
Neither substitutes for the other.

This phase is structurally outside the remediation loop and unreachable from
it — that, not a counter, is what makes re-entry impossible. It is not C002's
in-loop `bootstrap`, which is per-finding and gated by recheck.

1. **Upgrade the pin**, within the range `pyproject.toml` allows:

   ```
   uv lock --upgrade-package atlan-application-sdk-conformance
   ```

   A no-op where the package is source-pinned to a local path (`[tool.uv.sources]`
   with `path = ...`), which is the case in the SDK monorepo itself. If the lock's
   index URLs are rewritten as a side effect, revert that part of the diff.

2. **Re-sync the scaffolds**, capturing the manifest:

   ```
   uv run atlan-application-sdk-conformance bootstrap --resync --json
   ```

   `--resync` is a superset of a bare run, not an alternative to it: the
   always-overwrite set runs either way, and `--resync` adds the write-if-absent
   scaffolds (tests.yaml, renovate.json) plus the connector review kit. So it is
   always the right call here. `.gitignore` and `contract_schema.lock.json` are
   deliberately outside its scope.

3. **Read both outputs** — the JSON manifest *and* stdout:

   | Signal | Meaning | Action |
   |---|---|---|
   | `"skipped": true` | library / conformance-source repo; whole write phase no-ops | no re-read → Phase 1 |
   | a `skipped:` line on stdout | a scaffold **refused** to re-render (it declares a key the canonical cannot carry forward) | record as residue → Phase 1 |
   | `touched == []` | already canonical | no re-read → Phase 1 |
   | `touched != []` | scaffolds moved — including, possibly, this file | re-read this SKILL.md, then run step 2 **once** more |

   `touched` counts only `installed`/`updated`/`scaffolded`/`backed_up`/`removed`;
   an up-to-date file reports under `unchanged`. A per-file refusal *also* reports
   under `unchanged`, which is why stdout must be read separately — **the JSON
   alone cannot tell a refusal from convergence.**

4. **Fixpoint check** (the second run, at most). `touched == []` → converged,
   go to Phase 1. Still non-empty → **stop; do not run a third time.** Record
   `bootstrap non-idempotent on <paths>` as residue and go to Phase 1 anyway.

   **Hard cap: two bootstrap invocations, never three.** The second run is not
   belt-and-braces. Bootstrap's render kwargs come from autodetect reading back
   the files bootstrap itself writes, so run N+1's inputs are run N's outputs —
   and when a readback is not the exact inverse of its render, `touched` never
   empties. FND-361 was exactly this: `services-script` rendered bare but matched
   quoted-only, so resync deleted the live line on every single run. A second
   non-empty `touched` is that bug. It is a finding, not something to retry —
   same discipline as the loop's own oscillation detection (freeze and escalate,
   never re-attempt).

**Invariant:** after a converged Phase 0, the Phase 1 baseline should carry
**zero C002 findings**. One that appears means Phase 0 did not converge, and is
an independent check on the same property.

### Reference apps — load before any fix, verify against them after

Do not fix from memory. Every app-facing rule names a `canonical_reference`
(SARIF `atlan/canonicalReference` on the finding): a file in one of the three
maintained reference apps — `atlan-mysql-app`, `atlan-metabase-app`,
`atlan-openapi-app` — that already has the compliant shape. Before the first
edit of a run, make the **full checkout** of all three available **outside
the repo** (shallow clones of `origin/main`; read-only — never edited, never
committed, never in a fix's `touched_files`):

```sh
REFS="${XDG_CACHE_HOME:-$HOME/.cache}/atlan-conformance/refs"
mkdir -p "$REFS"
for app in atlan-mysql-app atlan-metabase-app atlan-openapi-app; do
  if [ -d "$REFS/$app/.git" ]; then
    git -C "$REFS/$app" fetch --depth 1 origin HEAD && git -C "$REFS/$app" checkout --quiet --detach FETCH_HEAD
  else
    git clone --depth 1 "https://github.com/atlanhq/$app.git" "$REFS/$app"
  fi
done
echo "$REFS"
```

Read them by the absolute path the snippet echoes. Never clone them into the
repo: `detect` scans the whole tree, and a reference app's `@entrypoint`s
under `remediation/` were added to the F016 matrix as false BLOCK failures.

For every finding, in this order (contract:
`$PROGRAMS/functions/remediate-finding.prose.md`, section *Reference apps,
impact analysis and verification*):

1. **Read** the whole file the finding's `canonical_reference` names, grep that
   reference app for the same pattern, and mirror it — never invent an API,
   kwarg or config key the reference app does not use.
2. **Analyse the impact** across the whole repo before applying: callers and
   importers, tests that pin the old behaviour, the contract and generated
   tree, `pyproject.toml`/`uv.lock`, `.env.example` and docs. Fold every
   in-scope consequence into the same edit; list the rest in `impact`.
3. **Verify** after applying and record it in `verification`: finding gone
   (`recheck-narrowest`), orthogonal gate passed, whole-series re-detect on the
   touched files introduced no new finding for any rule, fixed site reads like
   the reference. All four true, or revert.
4. **Review the consequences** once verified — what the fix changed
   behaviourally (control flow, signatures, runtime surfaces the gates do not
   exercise, new runtime dependencies) and who is affected. Fix what is in
   scope in the same unit and re-verify; list the rest in `impact.after`.
5. **A suppression is a rule-defect signal.** If the finding will not clear
   and the only way out is an inline ignore, classify why: `site-exception`
   (normal strict-mode suppression), `false-positive` or `prescription-defect`.
   For the last two run `$PROGRAMS/functions/report-rule-defect.prose.md`: it
   opens a `fix(conformance):` PR against `atlanhq/application-sdk` with a
   failing reproducer test (and the fix when local) for the SDK owners to
   review; suppress WARN-tier only, citing that PR; BLOCK-tier stays in residue
   with the PR link. Never merge that PR; never edit this repo's own gate.

`autofixable = true` rules (the **auto-fixable** ruleset) are applied this way.
`autofixable = false` rules (the **migration** ruleset) are never applied by
the loop: steps 1–2 still run and the result is a `migration_brief` in residue.

### Phase 1: Baseline

Call `detect-violations` to run the suite and capture the before-state.  Use the
full enabled series so every remediable area is covered (scope is auto-detected,
so app-only series no-op on the SDK):

```
let before = call detect-violations
  scope: .
  series: E,L,C,P,F,O,D,B,I,T,K,S
  rule_ids: <--rule argument split on commas, if any>
  target: if strict then "failing+warning" else "failing"
  path_prefix: <path argument, if any>
```

Copy `before.sarif_path` → `remediation/runs/before.sarif` to preserve it
before the remediation loop overwrites `detect.sarif`.  Note the counts
(failing, warning, suppressed) from `before.findings`.  Do not invoke
`suite.runner` directly — `detect-violations` is the single owner of that
invocation.

### Phase 2: Execute the remediation loop

Read and execute the OpenProse contracts in `$PROGRAMS`, starting with
`conformance-remediation.prose.md`.  The contracts are self-contained
English-plus-ProseScript — execute them directly as an agent (no separate
OpenProse runtime required for the skill path).

Execution order (from `conformance-remediation.prose.md`):

1. Run every area responsibility in parallel (error-handling, deprecation,
   dependency, prescriptions, preflight, optimizations, dockerfile, tests,
   logging, ci, contract-toolkit, security) — the top-level contract fans out to all of
   them; do not hardcode a subset.

2. Each area responsibility calls the `detect-fix-recheck` pattern
   (`patterns/detect-fix-recheck.prose.md`), which loops:
   - `functions/detect-violations.prose.md` — run `suite.runner`, parse SARIF
   - `functions/remediate-finding.prose.md` — propose fix or suppress (dispatches
     on `finding.area`, e.g. `deprecation` → `areas/deprecation.prose.md`)
   - `functions/recheck-narrowest.prose.md` — deterministic re-check
   - `functions/orthogonal-gate.prose.md` — test suite (fix path only)

3. Accumulate residue across all areas; emit the unified report.

If the OpenProse skill is installed, you may alternatively run:
`npx reactor run conformance-remediation scope=<path> mode=<default|strict>`

### Phase 3: After-state

Call `detect-violations` again and copy the result to `after.sarif`:

```
let after = call detect-violations
  scope: .
  series: E,L,C,P,F,O,D,B,I,T,K,S
  rule_ids: <--rule argument split on commas, if any>
  target: if strict then "failing+warning" else "failing"
  path_prefix: <path argument, if any>
```

Copy `after.sarif_path` → `remediation/runs/after.sarif`.  Compare
`after.findings` counts against Phase 1.  The `failing` count should be 0 (or
equal to the escalated residue count — never silently passed).  In strict mode,
`warning` should also be 0.

### Phase 4: Residue report

All residue items (judgment fixes, suppressions, recheck-failures,
oscillations) are written to `remediation/runs/residue.md` with:
- rule_id, file, line, fingerprint
- proposed edit (if any)
- classification and outcome
- reason the item is in residue

Review each item before merging.

## Anti-gaming disciplines (design §6)

| Discipline | Enforcement |
|---|---|
| No self-judging (§6.1) | Write scope excludes `tests/`, `.github/`, `conformance/` — except C002's `bootstrap` re-sync (deterministic, non-model-authored content, including its side-effect writes to `.claude/skills/remediate/SKILL.md` and `contract_schema.lock.json` — see `remediate-finding.prose.md`) and C001's ref-suffix repin (model-obtained SHA, so always escalated via `external_influence`) |
| Orthogonal gate (§6.1) | Test suite runs after every source-logic fix; fail → revert |
| Oscillation detection (§6.2) | Fingerprint-set identity check across rounds → freeze-and-escalate |
| Bounded loop (§6.2) | 5-attempt cap; batch per-file fixes in one pass |
| Ensures = check not belief (§5.2) | Postconditions bottom out in `suite.runner` exit code |

## OpenProse contracts

The full program is in `remediation/programs/`. For Reactor-ready execution:

```bash
# Install (dev-only — never a SDK runtime dep):
npm i -D @openprose/reactor @openprose/reactor-cli @openprose/reactor-devtools

# Scaffold state (first time):
npx reactor init remediation

# Compile DAG:
npx reactor compile

# Run:
npx reactor run conformance-remediation scope=. mode=default
```
