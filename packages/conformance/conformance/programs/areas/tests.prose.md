---
kind: responsibility
name: tests-area
description: >
  Maintains the current T-series violation-set and drives remediation of
  test-quality conformance findings: unmarked integration tests (T001), SDR
  test-coverage gaps (T002-T003), dev-entrypoint delegation (T004), assertion
  meaningfulness and silent non-execution (T005-T009), test-tier structure and
  placement (T010-T013), coverage-config integrity (T014-T015), and e2e CI
  compose-overlay queue isolation (T016).  Every rule in this series classifies
  as "judgment" — each fix requires reading the test's I/O intent, the app's
  manifest/contract, or the app's App subclass before a fix can be proposed with
  confidence.  T014/T015 are the most mechanical of the set (usually a one-line
  pyproject.toml edit) but still route to residue like the rest of the series —
  picking a real fail_under value or confirming an omit pattern is legitimate
  still requires judgment.  T016's fix is deterministic but edits a file under
  `.github/`, outside the remediator's write-scope, so it too routes to residue.
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

- **T005 AssertionFreeTest** — a collected test's body is non-empty but
  contains no recognised assertion. Read the test body and the function it
  calls to determine what outcome the test was meant to verify, then add a
  concrete assertion on that outcome — e.g. a return value, a record count, a
  side effect on disk, or a raised exception. Do not add `assert True` or any
  other constant-true placeholder to make the finding go away — that
  converts a T005 into a T007 (`VacuousAssertion`), which is not a fix.

  If the test's only real purpose is confirming a call doesn't raise (rare —
  most such tests are better expressed by also asserting on the return
  value), leave it flagged and route to residue with that assessment rather
  than fabricating an assertion that doesn't reflect real intent.

  `classification` is always `"judgment"` — knowing what outcome a test
  *should* verify requires reading the code under test, not just the finding.

- **T006 EmptyTestBody** — a collected test's body is only a stub
  (`pass`/`...`/a docstring). Either implement the test by reading what it is
  named to cover and what the surrounding test class/module already
  exercises, or propose deleting it if it duplicates coverage elsewhere.
  Never fill a stub with a vacuous assertion just to satisfy the checker.

  `classification` is always `"judgment"`.

- **T007 VacuousAssertion** — every assertion in a collected test is a
  constant-true expression (`assert True`, `assert 1`) that can never fail.
  Read what the test body actually exercises (the setup code above the
  vacuous assert almost always reveals the intended check — e.g. a comment
  like "verify cleanup was performed" right above `assert True` is a strong
  signal of what to assert instead) and replace the constant with a real
  assertion on that outcome.

  `classification` is always `"judgment"`.

- **T008 UncollectableTestFile** — a file under `tests/` defines
  `test*`/`Test*` collectables but its filename doesn't match pytest's
  default collection glob (`test_*.py`/`*_test.py`), so it is silently never
  collected. Fix: rename the file to match the convention (e.g.
  `tests/unit/connector_tests.py` → `tests/unit/test_connector.py`). Check
  for import references to the old filename (rare, since test modules are
  not usually imported by name) before renaming.

  `classification` is always `"judgment"` — even when the new name is
  unambiguous, the fix renames a file under `tests/`, which is outside the
  remediator's write-scope (see `remediate-finding.prose.md`); route to
  residue with the suggested rename for a human to apply.

- **T009 UnconditionalModuleSkip** — a module-level
  `pytest.skip(..., allow_module_level=True)` is not guarded by an `if`/`try`,
  unconditionally disabling every test in the file. Read why the skip was
  added (check git history / a nearby comment) to decide the fix:
  - If the file should run again, remove the skip and re-verify the tests
    still pass — or, if they don't yet, route to residue with what's broken.
  - If the file needs a real precondition guard (the legitimate e2e
    pattern), wrap it: `if not os.environ.get('<VAR>'): pytest.skip(..., allow_module_level=True)`.
  - If the file is intentionally, permanently disabled pending removal,
    suppress with `# conformance: ignore[T009] <reason>` naming the tracked
    removal issue, rather than leaving an unexplained unconditional skip.

  `classification` is always `"judgment"` — the correct outcome depends on
  why the skip exists, which the finding alone doesn't say.

- **T010 MissingUnitTestSuite** — no collectable tests under `tests/unit/`.
  Draft an initial unit suite covering the app's helper functions and
  `@task`-decorated activities directly (call them as coroutines — the
  decorator only attaches metadata outside the workflow runtime), following
  the minimal shape in `atlan-hello-world-app/tests/unit/`: typed
  `Input`/`Output` contracts, a `pytest.fixture` for the app instance, and
  real outcome assertions. This is not exemptable — do not propose a
  suppression or an `exempt_test_tiers` entry for T010.

  `classification` is always `"judgment"` — route to residue; a from-scratch
  unit suite requires reading the app's actual handler/mapper/client code,
  not just the finding.

- **T011 MissingIntegrationTestSuite** / **T012 MissingE2ETestSuite** — no
  collectable tests under `tests/integration/` or `tests/e2e/`. Two possible
  fixes, and the finding alone can't tell you which applies:
  1. The app genuinely needs this tier — draft a suite following the
     reference shape in `atlan-mysql-app`/`atlan-metabase-app`/
     `atlan-openapi-app` (integration: embedded Temporal + testcontainers or
     mocked infra; e2e: a thin class inheriting the SDK-generated
     `*GeneratedE2EBase`, env-guarded, `@pytest.mark.e2e`).
  2. The app is a genuine scaffold/minimal app with nothing to exercise at
     this tier yet — propose adding to `pyproject.toml`:

     ```toml
     [tool.conformance]
     exempt_test_tiers = ["integration"]  # or ["e2e"], or both
     ```

     State the reason in a comment above the table (e.g. "no external source
     configured yet — tracked in <ticket>").

  Route to residue either way — deciding between "draft the suite" and
  "exempt the tier" requires knowing whether the app has a real source/
  system-app integration to test, which only reading the app's code
  (handlers, clients, `atlan.yaml` entrypoints) can answer.

  `classification` is always `"judgment"`.

- **T013 TestFileOutsideTierDir** — a collectable test file lives under
  `tests/` but outside the four canonical tier directories. Read what the
  test actually exercises (external I/O → `tests/integration/`; no I/O →
  `tests/unit/`) and move it into the matching tier directory. If the file
  is genuinely non-tier test infrastructure that happens to match the
  collection glob (rare), suppress with
  `# conformance: ignore[T013] <reason>` instead of moving it.

  `classification` is always `"judgment"` — even when the correct tier is
  unambiguous from the test's imports/fixtures (e.g. it uses a
  `testcontainers` fixture → integration), the fix moves a file under
  `tests/`, which is outside the remediator's write-scope (see
  `remediate-finding.prose.md`); route to residue with the suggested
  destination for a human to apply.

- **T014 CoverageGateDisabled** — `[tool.coverage]` is configured but
  `[tool.coverage.report].fail_under` is absent or `0`. Run the test suite's
  own coverage report (or read the most recent CI coverage artifact) to find
  the *current* measured percentage, then set `fail_under` to that value (or
  the nearest whole number at or below it — never above, or the fix itself
  would fail the gate it's setting). Do not jump straight to the 90-100%
  target in one step; that is a follow-up ratchet, not this fix.

  ```toml
  [tool.coverage.report]
  fail_under = 60  # current measured percentage; ratchet up in follow-ups
  ```

  `classification` is `"judgment"` — the specific number requires knowing
  the repo's actual current coverage, which isn't in the finding.

- **T015 CoverageOmitsProductCode** — `[tool.coverage.run].omit` excludes
  real product code under `app/`, or `source` is configured but excludes the
  `app/` tree entirely. Read each offending `omit` entry named in the
  finding's message: if it targets a real handler/client/mapper module,
  remove it from `omit` (or narrow `source` to include `app/`) so that
  module contributes to (and is held to) the coverage floor. Only keep an
  `app/`-rooted omission if it targets generated contract artifacts
  (`app/generated/**`) or test infra — anything else should come out.

  `classification` is `"judgment"` — confirming a given module is safe to
  keep omitted (vs. real product code that should count) requires reading
  what the module does, not just its path.

- **T016 E2EDeploymentNameNotInherited** — an e2e CI docker-compose overlay
  under `.github/` (a YAML file with a top-level `services:` key that mentions
  `ATLAN_DEPLOYMENT_NAME`) assigns `ATLAN_DEPLOYMENT_NAME` in a service's
  `environment` to a literal that does not reference the inherited
  `${ATLAN_DEPLOYMENT_NAME...}` env var.  The SDK's `sdr-e2e` composite action
  derives a per-leg `ATLAN_DEPLOYMENT_NAME` (`e2e-full-ci-<run_id>[-<leg>]`) and
  exports it to `$GITHUB_ENV`; a hard-coded overlay value overrides that
  inherited env, so the worker container polls a different Temporal queue than
  the harness dispatches to — the extract activity stalls with "No Workers
  Running" until the run times out (observed on atlan-mysql-app; atlan-metabase-app
  had the same bug in map form).

  The fix is deterministic: wrap the existing hard-coded value as the fallback
  default so the value is inherited when set and preserved for local
  `docker compose` runs when not.  For the list form
  `- ATLAN_DEPLOYMENT_NAME=<value>`:

  ```yaml
  - ATLAN_DEPLOYMENT_NAME=${ATLAN_DEPLOYMENT_NAME:-<value>}
  ```

  For the mapping form `ATLAN_DEPLOYMENT_NAME: "<value>"`:

  ```yaml
  ATLAN_DEPLOYMENT_NAME: "${ATLAN_DEPLOYMENT_NAME:-<value>}"
  ```

  (A bare pass-through list entry `- ATLAN_DEPLOYMENT_NAME`, with no `=`, is
  also acceptable — it inherits the runner env directly.)

  **Route to residue** with the suggested edit — this function may not write
  under `.github/` (see the write-scope constraint in
  `remediate-finding.prose.md`; the remediator must never touch the CI gate it
  is judged against), so T016 is not auto-applied even though the transform is
  mechanical.

  Suppress with `# conformance: ignore[T016] <reason>` on the assignment line
  only when the overlay is intentionally single-queue (never fans out across
  matrix legs) and the hard-coded name is deliberate — state that reason
  explicitly.

  `classification` is always `"judgment"` (edits a file outside write-scope;
  routed to residue for a human to apply).

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose a suppression instead of a fix if the location is a false positive
(e.g. a conftest helper that looks like a test but is not one; or, for
T011/T012, an app with a deliberate `exempt_test_tiers` entry not yet added):

```
# conformance: ignore[TNNN] <concise justification, 8–40 words>
```

using the specific rule id from `finding.rule_id` (e.g. `T001`, `T009`,
`T014`).

The justification must describe *why* the marker is inappropriate here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit regardless.
