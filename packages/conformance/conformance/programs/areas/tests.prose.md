---
kind: responsibility
name: tests-area
description: >
  Maintains the current T-series violation-set and drives remediation of
  test-quality conformance findings: unmarked integration tests (T001), SDR
  test-coverage gaps (T002-T003), and dev-entrypoint delegation (T004).  Every
  rule in this series classifies as "judgment" — each fix requires reading the
  test's I/O intent, the app's manifest/contract, or the app's App subclass
  before a fix can be proposed with confidence.
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

**Default mode**: this facet is typically empty — all T-series rules are
WARN-tier and do not contribute to the FAILING count.  The gate exits 0
vacuously; no remediation runs.

**Strict mode**: `atlan-application-sdk-conformance detect --repo . --series T`
additionally reports `atlan/summary.warning` count 0 for T-series — every
T-series WARNING was cleared by a real fix or a justified suppression.

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

- **T002 MissingSdrTestClass** — the app declares `self_deployed_runtime: true`
  in `atlan.yaml` but no `BaseSDRIntegrationTest` subclass exists anywhere under
  `tests/`.  Draft a new test file (e.g. `tests/integration/test_sdr.py`) with
  a minimal subclass:

  ```python
  class TestMyAppSDR(BaseSDRIntegrationTest):
      manifest_path = "app/generated/manifest.json"
      workflow_type = "extraction"
  ```

  Use `manifest_path` (not `agent_spec_template`) so the test reads inputs from
  the committed manifest — see T003.  Apply `@pytest.mark.integration` (or the
  repo's equivalent SDR marker) so T001 is satisfied and the integration CI job
  picks it up.  Route to residue — the correct `manifest_path` and
  `workflow_type` require reading the app's contract and generated manifests.

  `classification` is always `"judgment"`.

- **T003 SdrTestLegacyAgentSpec** — a `BaseSDRIntegrationTest` subclass sets
  `agent_spec_template` (with a non-empty dict/string literal) but no
  `manifest_path`.  The hand-crafted spec bypasses manifest validation: the test
  can pass even when `manifest.json` is missing the `agent_json` slot — the
  exact mechanism behind the MSSQL regression (atlan-mssql-app#177, DISTR-752).
  Fix: on the class body, replace the `agent_spec_template` assignment with:

  ```python
  manifest_path = "app/generated/<name>/manifest.json"
  ```

  Use the committed manifest path for this app's workflow type.  Suppress with
  `# conformance: ignore[T003] <reason>` on the class definition line only when
  `agent_spec_template` is intentionally used for a non-manifest test scenario
  (e.g. a negative-path test that supplies deliberately invalid credentials) —
  and state that reason explicitly.

  `classification` is always `"judgment"`.

- **T004 DevEntrypointRequiresAppModule** — the repo-root `main.py` calls
  `application_sdk.main.main()` directly (via a bare name, an aliased module
  import, or a bare dotted chain).  `main()` always resolves its `App` class
  from `ATLAN_APP_MODULE`/`--app`, but `main.py` is also what CI's
  `connector-integration-tests` action runs directly (`python main.py`) for
  local/dev-mode testing, and the bootstrapped `tests-reusable.yaml` path has
  no input that lets a caller inject `ATLAN_APP_MODULE` into that job — so
  this fails every PR with `MissingAppModuleError`.  Fix: delegate to a local
  dev entrypoint — conventionally `app/run_dev.py` — that constructs the
  app's `App` subclass directly and calls `run_dev_combined(MyApp, ...)`:

  ```python
  # main.py
  import asyncio
  from app.run_dev import main

  if __name__ == "__main__":
      asyncio.run(main())
  ```

  If `app/run_dev.py` doesn't exist yet, draft one following the reference
  pattern in `atlan-metabase-app`, `atlan-openapi-app`, or `atlan-mysql-app`:
  import the app's own `App` subclass and call
  `await run_dev_combined(MyApp, credential_stores={...}, example_input={...})`.
  Route to residue — identifying the app's `App` subclass (module path and
  constructor requirements) and a representative `example_input` requires
  reading the app's own code, not just the finding.

  Suppress with `# conformance: ignore[T004] <reason>` on the call's line
  only when the app genuinely has no local dev-mode boot path and relies on
  `ATLAN_APP_MODULE` being set out-of-band even for CI (e.g. some
  utility/CSA apps) — state that reason explicitly.

  `classification` is always `"judgment"`.

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
