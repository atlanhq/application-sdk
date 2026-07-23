---
kind: responsibility
name: tests-area
description: >
  Maintains the current T-series violation-set and drives remediation of
  test-quality conformance findings: unmarked integration tests (T001), SDR
  test-coverage gaps (T002-T003), dev-entrypoint delegation (T004), assertion
  meaningfulness and silent non-execution (T005-T009), test-tier structure and
  placement (T010-T013), coverage-config integrity (T014-T015), e2e CI
  queue isolation (T016 worker-side overlay + T017 harness-side agent_spec), and
  the directory-scoped integration tier being deselected by pyproject addopts
  (T018).
  Every rule in this series classifies as "judgment" — each fix requires reading
  the test's I/O intent, the app's manifest/contract, or the app's App subclass
  before a fix can be proposed with confidence.  T014/T015 are the most
  mechanical of the set (usually a one-line pyproject.toml edit) but still route
  to residue like the rest of the series — picking a real fail_under value or
  confirming an omit pattern is legitimate still requires judgment.  T016/T017
  are a matched pair (the worker queue and the harness queue must agree);
  T016 edits a file under `.github/` (outside write-scope) and T017 edits a file
  under `tests/` (also outside write-scope), so both route to residue.  T018
  edits `pyproject.toml` (within write-scope), but whether an integration-tier
  deselect is legitimate is a judgment call, so it routes to residue too.
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
  in `atlan.yaml` but no test drives the SDR (agent-mode) path.  Two harnesses
  satisfy the rule; prefer the first:

  **Preferred — agent-mode e2e test.**  A `BaseE2ETest` subclass (usually via the
  generated `*GeneratedE2EBase`) with a class-level `mode = RunMode.AGENT`:

  ```python
  @pytest.mark.e2e
  class TestMyAppE2E(MyAppGeneratedE2EBase):
      mode = RunMode.AGENT
  ```

  This is what the migrated fleet (openapi/mysql/metabase) uses — env-guarded
  (`ATLAN_BASE_URL`/`ATLAN_API_KEY`) and `e2e`-label-gated, so it validates the
  live SDR path rather than running on every PR.

  **Alternative — legacy SDR integration harness.**  A `BaseSDRIntegrationTest`
  subclass, using `manifest_path` (not `agent_spec_template`, see T003):

  ```python
  class TestMyAppSDR(BaseSDRIntegrationTest):
      manifest_path = "app/generated/manifest.json"
      workflow_type = "extraction"
  ```

  Route to residue — the correct harness, `manifest_path`/`workflow_type`, and
  mustache/agent-spec wiring require reading the app's contract, generated
  bases, and manifests.

  `classification` is always `"judgment"`.

- **T003 DeprecatedSdrHarness** — a class under `tests/` subclasses
  `BaseSDRIntegrationTest`, which is deprecated and will be removed in v4.0.
  Migrate to the agnostic agent-mode e2e harness (same shape as the T002
  example above): a `BaseE2ETest` subclass via the generated `*GeneratedE2EBase`
  with `mode = RunMode.AGENT`, `@pytest.mark.e2e`, guarded import.

  **Ordering matters:** ADD the agent-mode e2e test first and confirm T002 is
  satisfied, THEN delete the `BaseSDRIntegrationTest` subclass (`test_sdr.py`) —
  an app that removes the SDR test before adding the e2e replacement would fail
  T002.  Carry over any behaviour the SDR suite validated (e.g. `manifest_path`
  assertions) into the e2e test.  Suppress with
  `# conformance: ignore[T003] <reason>` on the class definition line for a
  legitimate exception (e.g. a shim intentionally keeping the legacy harness
  during migration).

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

  **Pair with T017.** T016 fixes the *worker* queue; the *harness* must land on
  the same queue via its `agent_spec` (T017). If the connector's e2e test
  hard-codes `agent_spec` (see T017), applying this overlay fix *alone* makes
  the worker inherit the leg suffix while the harness stays pinned to the
  un-suffixed queue — breaking a previously-passing e2e (the atlan-metabase-app
  regression). When routing a T016 finding to residue, check for a T017 finding
  in the same repo and note that both must be applied together, never one alone.

  Suppress with `# conformance: ignore[T016] <reason>` on the assignment line
  only when the overlay is intentionally single-queue (never fans out across
  matrix legs) and the hard-coded name is deliberate — state that reason
  explicitly.

  `classification` is always `"judgment"` (edits a file outside write-scope;
  routed to residue for a human to apply).

- **T017 E2EAgentSpecPinsQueue** — an `agent_spec` override under `tests/`
  returns a hard-coded `AgentSpec(agent_name=...)` (a plain string or an
  f-string like `f"myconn-e2e-full-ci-{self.run_id}"`) that neither reads
  `ATLAN_DEPLOYMENT_NAME` nor calls `super().agent_spec()`. The harness builds
  its extract-node queue as `atlan-{agent_name}`; a hard-coded name pins it to
  `atlan-<app>-e2e-full-ci-<run_id>` (no matrix-leg suffix), so once the worker
  inherits the sdr-e2e per-leg `ATLAN_DEPLOYMENT_NAME` (T016) the two diverge
  and the run hangs with "No Workers Running".

  Fix (preferred): **delete the override.** `BaseE2ETest.agent_spec` derives
  `atlan-{app}-{deployment}` from the worker's env in CI and falls back to
  `{connector_short_name}-{connection_name_prefix}-{run_id}` locally, so no
  override is needed on either path — the harness picks up the per-leg suffix
  automatically and always matches the worker queue. Also drop the now-unused
  `AgentSpec` import if nothing else in the file uses it.

  If the override must stay (pinning a genuinely different agent identity), make
  it read the deployment env — defer to `super().agent_spec()` when
  `ATLAN_APPLICATION_NAME` + `ATLAN_DEPLOYMENT_NAME` are set, keeping the run-id
  name only as a local fallback (mirrors `SQLAppE2ETest.agent_spec`):

  ```python
  def agent_spec(self) -> AgentSpec:
      if os.environ.get("ATLAN_APPLICATION_NAME") and os.environ.get(
          "ATLAN_DEPLOYMENT_NAME"
      ):
          return super().agent_spec()
      return AgentSpec(agent_name=f"myconn-e2e-full-ci-{self.run_id}")
  ```

  **Route to residue** with the suggested edit — this edits a file under
  `tests/`, outside the remediator's write-scope (see
  `remediate-finding.prose.md`).

  **Pair with T016** (see above): the worker overlay and the harness agent_spec
  must be fixed together. A repo showing only a T017 finding (overlay already
  inherits, agent_spec still hard-coded) is actively broken and needs this fix;
  a repo showing both must have both applied in the same change.

  Suppress with `# conformance: ignore[T017] <reason>` on the `def agent_spec`
  line only when the hard-coded queue is deliberate (a single-leg suite that
  never fans out, whose overlay also hard-codes the same un-suffixed value) —
  state that reason explicitly.

  `classification` is always `"judgment"` (edits a file outside write-scope;
  routed to residue for a human to apply).

- **T018 IntegrationTierDeselectedByAddopts** — `[tool.pytest.ini_options].addopts`
  in `pyproject.toml` carries a `-m 'not <marker>'` selection expression, and one
  or more collectable tests under `tests/integration/` carry that deselected
  marker. The reusable Tests workflow (application-sdk#2852) runs the integration
  tier **by directory** (`pytest tests/integration/`, no `-m` re-selection), but
  `addopts` applies to every pytest run, so the deselect is still applied to the
  integration job and removes those tests from the only job meant to run them.
  When it deselects every collectable test the job collects nothing and pytest
  exits 5 (a hard CI failure); when it deselects only some, those run in no tier
  at all (the unit job never collects `tests/integration/`; the integration job
  deselects them), so they silently stop contributing signal. This is the inverse
  of T001: keep the `integration` marker present (T001), but do **not**
  `addopts`-deselect it — the directory is the tier boundary.

  The fix is to remove the `-m 'not …'` deselection from `addopts` and rely on
  the directory boundary plus the standard `integration` marker (T001), exactly
  as `atlan-mysql-app` / `atlan-metabase-app` do. Before:

  ```toml
  [tool.pytest.ini_options]
  markers = ["s3_integration: ...", "azure_integration: ..."]
  addopts = "-m 'not s3_integration and not azure_integration'"
  ```

  After:

  ```toml
  [tool.pytest.ini_options]
  markers = ["integration: requires external services; deselect locally with -m 'not integration'"]
  # no addopts -m deselection
  ```

  For tests that need an external service (an emulator, a live source), self-skip
  at runtime when it is unavailable — a module-scoped autouse fixture that probes
  the endpoint and calls `pytest.skip(...)` — so a bare local
  `pytest tests/integration/` stays green without the service while CI (which
  provisions it) runs the tests. Do not fall back to an `addopts` deselect for
  this: it hides the tests from the CI tier too.

  **Route to residue** with the suggested edit. Unlike T016/T017 the target file
  (`pyproject.toml`) is within write-scope, but deciding whether a given deselect
  is legitimate — versus one that silently drops a real integration tier — is a
  judgment call series-typical for the T-series, so T018 is not auto-applied.

  Suppress with `# conformance: ignore[T018] <reason>` on the `addopts` line only
  when the deselection is deliberate and the deselected tests are run by some
  other explicitly-configured CI job (rare — prefer the directory + runtime-skip
  pattern above); state that reason explicitly.

  `classification` is always `"judgment"` (whether an integration-tier deselect
  is legitimate requires reading the app's CI jobs and test intent; routed to
  residue for a human to confirm).

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
