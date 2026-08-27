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

      For the **P- and S-series** the gates remain structurally blind — P001's
      `MaxItems` is a declarative marker no test can observe, and no gate can
      prove a relocated credential still resolves.  Those results are therefore
      force-classified `unverifiable`, always routed to residue, and accepted
      only with a cited source for the chosen value; S-series additionally
      delivers as a draft with a named reviewer.  An uncited fix is never
      applied at all.

      Omitted (the default), all three areas behave byte-identically to before
      this input existed.
    required: false
    default: false
  - name: area
    description: >
      Comma-separated list of areas to remediate.  Defaults to every area the
      top-level program enables (error-handling, deprecation, dependency,
      prescriptions, optimizations, dockerfile, tests, logging,
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

### Phase 1: Baseline

Call `detect-violations` to run the suite and capture the before-state.  Use the
full enabled series so every remediable area is covered (scope is auto-detected,
so app-only series no-op on the SDK):

```
let before = call detect-violations
  scope: .
  series: E,L,C,P,O,D,B,I,T,K,S
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
   dependency, prescriptions, optimizations, dockerfile, tests, logging, ci,
   contract-toolkit, security) — the top-level contract fans out to all of
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
  series: E,L,C,P,O,D,B,I,T,K,S
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
