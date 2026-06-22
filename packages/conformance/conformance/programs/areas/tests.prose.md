---
kind: responsibility
name: tests-area
description: >
  Maintains the current T-series violation-set and drives remediation of
  test-quality conformance findings.  T001 (unmarked integration test) is the
  only rule in this series; its fix is mechanical — add the correct pytest
  marker — but classification is "judgment" because the caller must confirm
  which marker is appropriate for the test's integration scope.
---

### Maintains

The current set of unsuppressed T-series (test-quality) conformance findings
in the working tree, as reported by `suite.runner --series T`.

#### violations-tests

The fingerprint-set of all unsuppressed FAILING T-series results in the current
working tree, as reported by `suite.runner --series T`.

T-series rules are WARN-tier, so in **default** mode this facet is typically
empty (warnings do not fail the gate).  In **strict** mode the fingerprint-set
includes unsuppressed WARNING results, which is where T001 remediation runs.

This facet's fingerprint moves when any T-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series T` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for T-series in the SARIF output is 0 (every
> T-series WARNING was cleared by a real fix or a justified suppression).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `*.py` file under `tests/` within
`scope` changes.  In the Claude Code skill path the skill caller re-invokes on
demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "T"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "tests"`._

Consult the finding's `hint` and `message`, then look at the actual source
lines around `finding.line` in `finding.file` before proposing a fix.

**Judgment rules** (`autofixable = false`, `classification = "judgment"`; route
to residue):

- **T001 UnmarkedIntegrationTest** — a test function or class under
  `tests/integration/` (or any path the runner identifies as an integration
  test location) carries none of the required pytest markers.  The required
  markers are listed in the finding message; typical values are `integration`,
  `s3_integration`, or other scope-specific markers defined in the project's
  `pyproject.toml` `[tool.pytest.ini_options] markers`.

  Fix: add the appropriate `@pytest.mark.<marker>` decorator to the test
  function or class.  Choose the marker by reading the test's body:
  - Tests that call external services, databases, or cloud APIs → `integration`
  - Tests that specifically exercise S3/object-store paths → `s3_integration`
  - When the test covers multiple integration scopes, apply all relevant markers.

  If the file is under `tests/integration/` but the test itself is a pure-unit
  test (no external I/O), propose moving it to `tests/unit/` rather than adding
  an integration marker — and note that in the edit description.

  If the correct marker for this test is genuinely ambiguous (e.g. a new
  integration type with no established marker), route to residue with a
  suggested marker name for the maintainer to enshrine in `pyproject.toml`.

  `classification` is always `"judgment"` — the specific marker choice requires
  reading the test's I/O intent.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose a suppression instead of a fix if the test location is a false positive
(e.g. a conftest helper that looks like a test but is not one):

```
# conformance: ignore[T001] <concise justification, 8–40 words>
```

The justification must describe *why* the marker is inappropriate here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit regardless.
