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

argument-hint: "[--area error-handling|deprecation|dependency|prescriptions|optimizations|dockerfile|tests|logging|ci|contract-toolkit] [--strict] [path]"

inputs:
  - name: area
    description: >
      Comma-separated list of areas to remediate.  Defaults to every area the
      top-level program enables (error-handling, deprecation, dependency,
      prescriptions, optimizations, dockerfile, tests, logging; ci is
      partially remediated — C002 and C003's absent-file case are fixed
      mechanically via `bootstrap`, everything else in the C-series routes
      to residue).  Example: --area deprecation
    required: false
    default: "error-handling,deprecation,dependency,prescriptions,optimizations,dockerfile,tests,logging,ci,contract-toolkit"
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
      to remediation/runs/residue.md.

gates:
  - deterministic_recheck: >
      Every fix is re-checked with suite.runner --series <area-series> before
      it survives.  If recheck fails, the edit is reverted.
  - orthogonal_gate: >
      Every source-logic fix is also verified by the test suite (uv run poe
      test).  If tests break, the edit is reverted.  Suppression-only edits
      skip this gate (comment-only changes cannot break tests), as do rules
      whose orthogonal_gate is declared "none" (e.g. C002/C003 — a
      .github/.gitignore re-sync cannot affect Python behaviour).
  - no_self_judging: >
      The remediator never touches tests/, .github/, or conformance/ — the
      gates it is judged against.  This is structural: remediate-finding's
      write scope excludes these paths, with one narrow exception: C002's fix
      is invoking `atlan-application-sdk-conformance bootstrap`, which writes
      deterministic template content the model never authors or chooses.
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
```

**Path argument**: a repo-root-relative path prefix that filters *which findings
are remediated*.  It does **not** change what the runner scans — the runner
always scans the whole repo.  Findings outside the prefix are left untouched.

## Modes

**Default** — remediates only FAILING (BLOCK-tier, gate-blocking) findings.

**Strict** (`--strict`) — also remediates WARNING (WARN-tier) findings.  Each
WARNING is cleared by either a real fix or a justified inline suppression.  Every
suppression is routed to the residue report for human audit.

## Area status

The live program (`conformance-remediation.prose.md`, resolved from the installed
package via `programs-dir`) fans out to every area below; `remediate-finding`
dispatches each finding to its area prescription.

| Area | Series | Remediation | Notes |
|---|---|---|---|
| error-handling | E | ✅ Implemented | Mechanical (E005, E016) auto-fixed; judgment (E002, E013, others) modelled + routed to residue |
| deprecation | B | ✅ Implemented | B001 guided fix (incl. legacy transformer → asset-mapper, BLDX-1399); B003/B004 detect-only → residue |
| dependency | D | ✅ Implemented | Guided + mechanical fixes; judgment routed to residue |
| prescriptions | P | ✅ Suggest-only | Findings modelled + routed to residue |
| optimizations | O | ✅ Implemented | Below-the-bar recommendations |
| dockerfile | I | ✅ Suggest-only | Findings modelled + routed to residue |
| tests | T | ✅ Strict-only | WARNING-tier; strict mode |
| logging | L | ✅ Implemented | Mechanical (L004, L007, L015, L017, L020) auto-fixed; judgment (L001, L002, L005, others) modelled + routed to residue |
| ci | C | ✅ Partial | C002 (managed-file drift) mechanical via `bootstrap` re-sync; also clears C003's absent-`.gitignore` case as a side effect. C001, C003 missing-entry, and drifted `tests.yaml`/`renovate.json` → residue |
| contract-toolkit | K | ✅ Strict-only | K001/K002 guided migration to App.pkl; verified by pkl-eval gate |

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
  series: E,L,C,P,O,D,B,I,T,K
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
   contract-toolkit) — the top-level contract fans out to all of them; do not
   hardcode a subset.

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
  series: E,L,C,P,O,D,B,I,T,K
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
| No self-judging (§6.1) | Write scope excludes `tests/`, `.github/`, `conformance/` — except C002's `bootstrap` re-sync, which writes deterministic, non-model-authored template content, not a judged edit |
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
