---
kind: responsibility
name: error-handling-area
description: >
  Maintains the current E-series violation-set and drives remediation of
  error-handling conformance findings.  The only fully-implemented area in
  phase 1.
---

### Maintains

The current set of unsuppressed E-series (error-handling) conformance findings
in the working tree, classified by disposition (FAILING / WARNING) and
remediability.

#### violations-error-handling

The fingerprint-set of all unsuppressed FAILING E-series results in the
current working tree, as reported by `suite.runner --series E`.

In strict mode the fingerprint-set extends to include unsuppressed WARNING
results as well.

This facet's fingerprint moves when any E-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `PYTHONPATH=conformance python -m suite.runner --repo . --series E` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
This is the Reactor-ready wake source — in the Claude Code skill path, the
skill caller re-invokes on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "E"
  mode: mode
  max_attempts: 5
```
