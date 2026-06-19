---
kind: responsibility
name: deprecation-area
description: >
  Maintains the current B-series violation-set and drives remediation of
  deprecation findings.  B001 (app: stop consuming a deprecated SDK symbol) and
  B002 (sdk: fix a malformed deprecation notice) are guided fixes; B003 (overdue
  removal) and B004 (unmarked claim) are detect-only and route to residue.
---

### Maintains

The current set of unsuppressed B-series (backwards-compatibility / deprecation)
conformance findings in the working tree, classified by disposition and
remediability.

#### violations-deprecation

The fingerprint-set of all unsuppressed FAILING B-series results in the current
working tree, as reported by `suite.runner --series B`.

B-series rules are WARN-tier, so in **default** mode this facet is typically
empty (warnings do not fail the gate).  In **strict** mode the fingerprint-set
includes unsuppressed WARNING results, which is where B-series remediation
actually runs.

The active scope decides which rules can appear: on a consumer app only B001
(scope `app`) surfaces; on the SDK only B002/B003/B004 (scope `sdk`).  The runner
auto-detects scope, so each repo only ever sees its own half.

This facet's fingerprint moves when any B-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series B` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for B-series in the SARIF output is 0 (every
> B-series WARNING was cleared by a real fix or a justified suppression).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
In the Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "B"
  mode: mode
  max_attempts: 5
```
