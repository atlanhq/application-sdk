---
kind: responsibility
name: dependency-area
description: >
  Maintains the current D-series violation-set and drives remediation of
  pyproject.toml dependency-contract findings.  Mechanical fixes (add an SDK
  upper bound, delete a redeclared line, raise the requires-python floor) are
  applied automatically; the bare-SDK-pin case is proposed and routed to
  residue for human review.
---

### Maintains

The current set of unsuppressed D-series (dependency-contract) conformance
findings in the working tree, as reported by `suite.runner --series D`.

#### violations-dependency

The fingerprint-set of all unsuppressed FAILING D-series results.  Extends to
include WARNING results in strict mode — D002/D004/D005/D006/D007/D008 are
WARN-tier, so they are processed in strict mode; D001 is BLOCK-tier and
processed in both modes.

This facet's fingerprint moves when any D-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series D` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### The re-detection gate is authoritative for this area

D-series edits change `pyproject.toml` text, **not** the installed
environment: the loop does not `uv sync` between the edit and the gates.  The
orthogonal gate (`uv run … pytest`) therefore runs against the *unchanged*
resolved env, so it can neither break (no wrongful revert) nor validate a
dependency edit — a green orthogonal result here is vacuous and must not be
read as confirming the fix.  The protective gate for D is
`recheck-narrowest`, which re-runs `suite.runner --series D` scoped to the
touched `pyproject.toml`; the detector reads the file directly, so the edit is
reflected and the finding's fingerprint genuinely disappears only when the
text is correct.  Treat D edits like suppression-only edits with respect to
trust in the test gate: rely on re-detection.

**No gate validates the D001 cap *value*, only its presence.** `D001`'s
detector (`_is_bounded_specifier`) tests only that an upper bound exists — a
wrong cap such as `<3.0.0` that *excludes* the installed major still clears the
finding and ships a broken pyproject.  The prescription wording in
`remediate-finding.prose.md` (cap at the next major so the range includes the
pinned version) is therefore the sole safeguard for cap correctness; the
remediator must follow it exactly, and the human review of residue is the
backstop.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when `pyproject.toml` under `scope` changes.  In the
Claude Code skill path the skill caller drives re-invocation on demand rather
than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "D"
  mode: mode
  max_attempts: 5
```
