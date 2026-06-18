---
kind: responsibility
name: optimizations-area
description: >
  Maintains the current O-series violation-set and drives remediation of
  optimisation / recommendation findings.  Fully implemented: O-series fixes
  are judgment edits with a gate that bites (behavioural tests catch a
  bytes/str regression), so the bounded loop is safe to run here.
---

### Maintains

The current set of unsuppressed O-series (optimisation) conformance findings
in the working tree, classified by disposition (FAILING / WARNING) and
remediability.

#### violations-optimizations

The fingerprint-set of all unsuppressed FAILING O-series results in the
current working tree, as reported by `suite.runner --series O`.

O-series rules are WARN-tier, so in **default** mode this facet is typically
empty (warnings do not fail the gate).  In **strict** mode the fingerprint-set
includes unsuppressed WARNING results, which is where O001 remediation
actually runs.

This facet's fingerprint moves when any O-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series O` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for O-series in the SARIF output is 0 (every
> O-series WARNING was cleared by a real fix or a justified suppression).

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
  series: "O"
  mode: mode
  max_attempts: 5
```
