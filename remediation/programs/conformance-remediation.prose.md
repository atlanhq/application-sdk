---
kind: responsibility
name: conformance-remediation
description: >
  Top-level conformance remediation entry point.  Subscribes to every
  per-area violations facet and is clean only when all subscribed areas are
  clean.  Delegates the bounded gated loop to each area responsibility.
---

### Maintains

The current remediation state of the working tree across all enabled
conformance rule areas (error-handling, logging, CI).

#### violations-summary

An aggregate of violation counts across all enabled areas:

```
{
  "failing": <count of unsuppressed FAILING results>,
  "warning": <count of unsuppressed WARNING results>,
  "suppressed": <count of suppressed results (audit trail)>,
  "residue": <count of findings routed to human review>
}
```

Postcondition (deterministic validator — never render-attested):

**Default mode:** `PYTHONPATH=conformance python -m suite.runner --repo .
--series E,L,C` exits 0 — zero unsuppressed FAILING results across all
enabled areas.

**Strict mode** (`--strict`): additionally, the `atlan/summary.warning` count
in the SARIF output is 0 — zero unsuppressed WARNING results.  Every WARNING
was cleared by a real fix or by a justified inline suppression.

#### residue

The set of findings that could not be auto-resolved this run, together with
the reason each was routed here:

- `judgment` — the model made a non-trivial fix; route to human for review
  before merge.
- `suppressed` — the model proposed a `# conformance: ignore` directive for a
  WARNING (strict mode); route to human to confirm the justification is sound.
- `recheck-failed` — the proposed edit did not clear the finding; manual fix
  needed.
- `orthogonal-gate-failed` — the edit cleared the finding but broke tests.
- `not-remediable` — no prescription exists for this area yet.
- `oscillation` — the loop detected a repeating violation-set and froze.
- `max-attempts` — the cap was reached with violations remaining.

### Requires

- `violations-error-handling` from `error-handling-area`
- `violations-logging` from `logging-area`
- `violations-ci` from `ci-area`

Forme auto-wires these subscriptions from the matching `#### facet` names in
the area responsibilities.  This node is clean only when every subscribed
facet is clean.

### Continuity

Input-driven: re-render when any `*.py` file or `.github/` file under `scope`
changes.  In the Reactor-ready path this is the filesystem watch source; in
the Claude Code skill path the skill caller drives re-invocation on demand.

### Execution

```prose
parallel:
  call error-handling-area
    scope: scope
    mode: mode
  call logging-area
    scope: scope
    mode: mode
  call ci-area
    scope: scope
    mode: mode

# Collect residue from all areas and emit the unified report.
emit violations-summary and residue
```
