# Conformance Remediation (OpenProse)

> **Dev-only.** This directory is never bundled in the PyPI package.

The `remediation/` directory contains the **OpenProse remediation program** for
the Fleet Drift Remediator (BLDX-1388).  It consumes the SARIF output from the
`conformance/` suite and drives a bounded, gated loop that resolves violations
using the model — with deterministic re-checks as the arbiter of success.

---

## Design principle

> *Detection must be deterministic; the model's job is remediation.*

The conformance suite (`conformance/`) detects violations deterministically and
emits SARIF 2.1.0 (the versioned seam, BLDX-1385).  This program is a consumer
of that output — not a restatement of the detection logic.  The `Ensures`
postcondition of every responsibility here bottoms out in a `suite.runner` exit
code, never a model belief.

For the full design rationale, see the Fleet Drift Remediator project doc in
Linear.

---

## Quick start (Claude Code skill)

```bash
# Run from the repo root:
/remediate                        # all areas, FAILING only
/remediate --strict               # all areas, FAILING + WARNING
/remediate --area error-handling  # error-handling only
```

The skill writes `remediation/runs/before.sarif`, `after.sarif`, and
`residue.md` on each run.

## Structure

```
remediation/
  README.md                                   (this file)
  programs/
    conformance-remediation.prose.md           TOP-LEVEL responsibility
    functions/
      detect-violations.prose.md               cross-area: runs suite.runner, parses SARIF
      remediate-finding.prose.md               dispatches by area; proposes fix or suppress
      recheck-narrowest.prose.md               deterministic re-check (narrowest gate)
      orthogonal-gate.prose.md                 runs the test suite (orthogonal gate)
    patterns/
      detect-fix-recheck.prose.md              bounded gated loop (area-agnostic)
    areas/
      error-handling.prose.md                  E-series — PHASE 1: implemented
      logging.prose.md                         L-series — registered; prescription deferred
      ci.prose.md                              C-series — registered; prescription deferred
  runs/                                        (gitignored) per-run SARIF + residue reports
```

## Area status

| Area | Series | Phase 1 remediation |
|---|---|---|
| error-handling | E | ✅ Mechanical (E005, E016) + judgment (E001–E002, E013, others) |
| logging | L | 🚧 Detection only → residue |
| ci | C | 🚧 Detection only → residue |

### Adding a new area

1. Author `programs/areas/<name>.prose.md` with a `#### violations-<name>` facet
   and a prescription in the `### Execution` block.
2. Add a dispatch branch to `programs/functions/remediate-finding.prose.md`.
3. Add a `Requires` entry for `violations-<name>` in
   `programs/conformance-remediation.prose.md`.

---

## Reactor-ready path (later phase)

The contracts are authored to be Reactor-compilable — proper `### Maintains`
facets, `### Requires` subscriptions, and `### Continuity` wake sources.  To
use the full reconciler:

```bash
# Install (dev-only):
npm i -D @openprose/reactor @openprose/reactor-cli @openprose/reactor-devtools

# First-time scaffold:
npx reactor init remediation

# Compile DAG:
npx reactor compile

# Run:
npx reactor run conformance-remediation scope=. mode=default
```

In the Reactor path, the reconciler watches `*.py` files for changes and
re-renders only the area facets whose inputs moved — inference cost scales
with change, not fleet size.

---

## Anti-gaming disciplines

| Discipline | How enforced |
|---|---|
| No self-judging (§6.1) | `remediate-finding` write scope excludes `tests/`, `.github/`, `conformance/` |
| Orthogonal gate (§6.1) | Test suite runs after every source-logic fix; failure reverts the edit |
| Oscillation detection (§6.2) | Fingerprint-set identity check across rounds → freeze-and-escalate |
| Bounded loop (§6.2) | 5-attempt cap; findings batched per file |
| Ensures = check not belief | Postconditions bottom out in `suite.runner` exit/SARIF — never model-attested |
