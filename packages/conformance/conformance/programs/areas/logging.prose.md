---
kind: responsibility
name: logging-area
description: >
  Maintains the current L-series violation-set and drives remediation of
  logging conformance findings.  Mechanical fixes (method renames, kwarg
  additions, format-string rewrites) are applied automatically; judgment
  fixes (factory swaps, print replacements, complex rewrites) are proposed
  and routed to residue for human review.
---

### Maintains

The current set of unsuppressed L-series (logging) conformance findings in
the working tree, as reported by `suite.runner --series L`.

#### violations-logging

The fingerprint-set of all unsuppressed FAILING L-series results.  Extends to
include WARNING results in strict mode.

This facet's fingerprint moves when any L-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series L` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any `*.py` file or `pyproject.toml` under `scope`
changes.  In the Claude Code skill path the skill caller drives re-invocation
on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "L"
  mode: mode
  max_attempts: 5
```
