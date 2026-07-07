"""Test-quality rule definitions (T-series).

Integration tests under ``tests/integration/`` are heavyweight: they boot an
embedded Temporal dev server (and sometimes ``daprd`` or cloud emulators) and are
*selected* in dedicated integration CI jobs (``-m integration`` /
``-m s3_integration`` / …) while being *deselected* from the unit job by the
``addopts = -m 'not integration and not e2e and not s3_integration …'`` expression
in ``pyproject.toml``.  That deselection only works if every such test carries one
of those markers.  A file that forgets them all is **not** deselected — it leaks
into every unit matrix cell (every Python × OS combination), where the
Temporal/emulator boot can exceed the unit job's tight timeout, while being
**excluded** from the integration job that was meant to run it.

* ``T001`` — every test collected under ``tests/integration/`` must carry a marker
  that the unit job deselects (via a module-level ``pytestmark``, an
  enclosing-class decorator, or its own ``@pytest.mark.<marker>`` decorator).  The
  accepted set is derived per-repo from the ``-m`` deselection expression in the
  app's own ``pyproject.toml`` ``addopts`` (default: ``{"integration"}``).

SDR test-quality rules (DISTR-752):

* ``T002`` — apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``
  must have a ``BaseSDRIntegrationTest`` subclass in their test suite.  Without
  one there is no test that validates manifest inputs (including ``agent_json``),
  credential routing, or upload behaviour in an SDR-like environment.

* ``T003`` — a ``BaseSDRIntegrationTest`` subclass that sets
  ``agent_spec_template`` (and not ``manifest_path``) bypasses manifest
  validation: the hand-crafted spec can satisfy SDR requirements even when the
  committed ``manifest.json`` is broken.  The MSSQL regression (atlan-mssql-app#177,
  DISTR-752) slipped through exactly this way.  Subclasses must switch to
  ``manifest_path`` so the test reads inputs from the committed manifest.

Dev-entrypoint conformance (BLDX-1520):

* ``T004`` — root ``main.py`` must not call ``application_sdk.main.main()``
  directly.  That is the production, ``ATLAN_APP_MODULE``-driven launcher, but
  ``main.py`` is also what CI's ``connector-integration-tests`` composite
  action runs directly (``python main.py``) for local/dev-mode testing — and
  the bootstrapped ``tests-reusable.yaml`` path has no input to inject
  ``ATLAN_APP_MODULE`` into that job.  A ``main.py`` that delegates straight to
  ``application_sdk.main.main()`` therefore fails every PR with
  ``MissingAppModuleError``.  Delegate instead to a local dev entrypoint
  (conventionally ``app/run_dev.py``) that constructs the ``App`` subclass
  directly and calls ``run_dev_combined(MyApp, ...)`` — see
  ``atlan-metabase-app``, ``atlan-openapi-app``, or ``atlan-mysql-app`` for the
  reference pattern.

Test-coverage-and-quality rules (BLDX-1400):

The rules above police *placement* and *SDR readiness*; the rules below police
whether the tests that exist are actually meaningful — closing the gap where a
coverage percentage is reached by code that runs but never verifies an
outcome. Four sub-families:

**Assertion meaningfulness** — a test file can be "covered" by pytest without
a single assertion ever running:

* ``T005`` — AssertionFreeTest: a collected test has a non-empty body but no
  recognised assertion (no ``assert``, ``pytest.raises``/``warns``,
  ``mock.assert_*``, ``self.assert*``, scenario-helper call, etc.). The
  flagship "ran but verified nothing" rule.
* ``T006`` — EmptyTestBody: a collected test's body is only ``pass``/``...``/a
  docstring — a placeholder stub, not merely assertion-free.
* ``T007`` — VacuousAssertion: every assertion in a collected test is a
  constant-true expression (``assert True``, ``assert 1``) that can never
  fail.

**Silent non-execution** — a test can look present in the diff while never
actually running in CI:

* ``T008`` — UncollectableTestFile: a file under a test-tier directory defines
  ``test*``/``Test*`` collectables but its filename doesn't match pytest's
  default collection glob (``test_*.py``/``*_test.py``), so it is silently
  never collected.
* ``T009`` — UnconditionalModuleSkip: a module-level
  ``pytest.skip(..., allow_module_level=True)`` that isn't guarded by an
  ``if``/``try`` — an unconditional blanket disable, as opposed to the
  legitimate env-guarded pattern used by e2e suites.

**Tier structure & placement** — the CI composite actions locate tiers by
directory convention (``tests/unit``, ``tests/integration``, ``tests/e2e``);
a tier that doesn't exist where expected silently contributes zero coverage:

* ``T010`` — MissingUnitTestSuite: no collectable tests under ``tests/unit/``.
  The universal floor — every canonical app has one. Not exemptable.
* ``T011`` — MissingIntegrationTestSuite: no collectable tests under
  ``tests/integration/``. Exemptable per-repo for scaffold/minimal apps via
  ``[tool.conformance].exempt_test_tiers`` in ``pyproject.toml`` (``atlan.yaml``
  is generated from the app's Pkl contract and must not be hand-edited, so the
  opt-out lives in the one config file conformance already reads for D-series
  and T001).
* ``T012`` — MissingE2ETestSuite: no collectable tests under ``tests/e2e/``.
  Exemptable the same way. Weakest of the three: end-to-end needs only one
  representative run, not scenario-level coverage.
* ``T013`` — TestFileOutsideTierDir: a collectable test file lives directly
  under ``tests/`` (or in a non-canonical subdirectory) instead of one of the
  four tier directories, so no CI composite action is wired to run it.

**Coverage-config integrity** — a coverage percentage is only a meaningful
signal if the gate that produces it can actually fail and actually measures
the code that ships:

* ``T014`` — CoverageGateDisabled: ``[tool.coverage]`` is configured but
  ``[tool.coverage.report].fail_under`` is absent or ``0`` — coverage is
  measured but never enforced.
* ``T015`` — CoverageOmitsProductCode: ``[tool.coverage.run].omit`` (or a
  narrowed ``source``) excludes real product code under ``app/`` — inflating
  the reported percentage by hiding uncovered code from the denominator.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="T001",
        scope=RuleScope.BOTH,
        name="UnmarkedIntegrationTest",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-marking",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "Unit/integration separation in CI is enforced purely by pytest markers: "
            "the unit job deselects '-m not integration and not s3_integration and …' "
            "and the integration jobs select '-m integration' / '-m s3_integration'. A "
            "test under tests/integration/ carrying none of those markers therefore "
            "runs in the wrong job — it pollutes every unit matrix cell (where a slow "
            "Temporal/emulator boot can blow the job timeout) and never runs in its "
            "integration job at all. Making 'lives in tests/integration/ => carries a "
            "unit-deselecting marker' a deterministic, reviewable rule closes that gap "
            "without the hidden behaviour of an auto-marking conftest hook. The "
            "accepted marker set is read from the repo's own addopts so it is correct "
            "for any app, not just the SDK."
        ),
        short_description=(
            "Test under tests/integration/ is not marked with a pytest marker that "
            "deselects it from the unit job"
        ),
        full_description=(
            "Every test collected under ``tests/integration/`` must carry a marker "
            "that the unit job deselects (e.g. ``integration``, ``s3_integration``, "
            "``storage_emulator``) so the unit job skips it and a dedicated "
            "integration job runs it.  A test is considered marked when the module "
            "declares ``pytestmark`` containing such a marker (bare or in a "
            "list/tuple), when an enclosing ``Test*`` class is decorated with one, or "
            "when the test function itself carries one.  The accepted set is derived "
            "per-repo from the ``-m 'not …'`` expression in ``[tool.pytest."
            'ini_options].addopts`` (falling back to ``{"integration"}``).  Unmarked '
            "tests leak into the unit matrix — where the embedded Temporal/Dapr/"
            "emulator boot can exceed the unit job timeout — and are skipped by the "
            "dedicated integration job.  Tracked in BLDX-1455; chosen over an "
            "auto-marking ``conftest.py`` hook precisely to avoid non-obvious hidden "
            "behaviour."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t001"
        ),
    ),
    RuleDefinition(
        id="T002",
        scope=RuleScope.APP,
        name="MissingSdrTestClass",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-test-coverage",
        autofixable=False,
        since="0.9.0",
        rationale=(
            "An SDR app that declares self_deployed_runtime: true in atlan.yaml "
            "but has no BaseSDRIntegrationTest subclass has no automated test that "
            "validates SDR-specific behaviour: manifest inputs (agent_json, etc.), "
            "credential routing via the agent-mode dispatch path, and the "
            "ENABLE_ATLAN_UPLOAD upload gate. The MSSQL regression (DISTR-752) "
            "slipped through status-only CI exactly because no SDR test class "
            "validated manifest-derived inputs — the manifest was broken but all "
            "status checks passed."
        ),
        short_description=(
            "SDR app declares self_deployed_runtime but has no BaseSDRIntegrationTest subclass"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "at least one ``BaseSDRIntegrationTest`` subclass must be present\n"
            "somewhere under ``tests/``.\n"
            "\n"
            "``BaseSDRIntegrationTest`` (from ``application_sdk.testing.sdr.base``)\n"
            "is the SDK's integration test harness for SDR apps.  It boots a\n"
            "Temporal dev server, injects credentials from the test environment,\n"
            "and validates that the end-to-end SDR workflow completes correctly —\n"
            "including manifest-derived inputs, credential routing, and the\n"
            "``ENABLE_ATLAN_UPLOAD`` gate.  An SDR app without this harness has no\n"
            "automated coverage of the code paths that differ between standard and\n"
            "SDR deployments.\n"
            "\n"
            "**Remediation:** create a test class that:\n"
            "\n"
            ".. code-block:: python\n"
            "\n"
            "    class TestMyAppSDR(BaseSDRIntegrationTest):\n"
            "        manifest_path = 'app/generated/manifest.json'\n"
            "        workflow_type = 'extraction'\n"
            "\n"
            "Set ``manifest_path`` (not the legacy ``agent_spec_template``) so the\n"
            "test reads inputs from the committed manifest and validates the\n"
            "``agent_json`` slot — see T003 for the complementary rule.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t002"
        ),
    ),
    RuleDefinition(
        id="T003",
        scope=RuleScope.APP,
        name="SdrTestLegacyAgentSpec",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-test-coverage",
        autofixable=False,
        since="0.9.0",
        rationale=(
            "A BaseSDRIntegrationTest subclass that sets agent_spec_template (and "
            "not manifest_path) supplies credentials to the test workflow via a "
            "hand-crafted JSON blob rather than reading inputs from the committed "
            "manifest.json. This means the test can pass even when the manifest is "
            "missing the agent_json slot — the hand-crafted spec fills the gap the "
            "manifest was supposed to fill. This is the exact mechanism that allowed "
            "the MSSQL regression (atlan-mssql-app#177, DISTR-752) to slip through: "
            "the test passed because agent_spec_template bypassed the broken manifest, "
            "but production runs failed because the manifest had no agent_json slot. "
            "Switching to manifest_path forces the test to read inputs from the "
            "committed manifest, catching missing-agent_json and other manifest "
            "defects at CI time."
        ),
        short_description=(
            "BaseSDRIntegrationTest subclass uses legacy agent_spec_template instead of manifest_path"
        ),
        full_description=(
            "A ``BaseSDRIntegrationTest`` subclass must use ``manifest_path``\n"
            "(not ``agent_spec_template``) so the test reads workflow inputs from\n"
            "the committed ``manifest.json`` file.\n"
            "\n"
            "``agent_spec_template`` is the legacy class var: it supplies a\n"
            "hand-crafted JSON blob directly to the test workflow, bypassing the\n"
            "manifest entirely.  This means the test can pass even when\n"
            "``manifest.json`` is missing the ``agent_json`` slot or has other\n"
            "defects — the template fills in what the manifest was supposed to\n"
            "provide.  P029 closes the static manifest gap; T003 closes the test\n"
            "gap: a subclass using ``manifest_path`` will fail at test time\n"
            "whenever ``manifest.json`` is broken, not silently pass.\n"
            "\n"
            "**Remediation:** in the subclass body, replace::\n"
            "\n"
            "    agent_spec_template = '{...}'    # legacy\n"
            "\n"
            "with::\n"
            "\n"
            "    manifest_path = 'app/generated/manifest.json'\n"
            "\n"
            "The ``manifest_path`` class var tells ``BaseSDRIntegrationTest`` to\n"
            "call ``_manifest_extract_inputs()`` which reads ``dag.extract.inputs``\n"
            "from the manifest — including the ``agent_json`` slot — and passes\n"
            "them as the workflow start parameters.  If ``agent_json`` is missing\n"
            "from the manifest the test will raise a ``KeyError`` at startup,\n"
            "surface the defect, and fail the CI run rather than letting the\n"
            "broken manifest reach production.\n"
            "\n"
            "Suppress with ``# conformance: ignore[T003] <reason>`` on the class\n"
            "definition line when ``agent_spec_template`` is intentionally used\n"
            "for a non-manifest test scenario (e.g. a negative-path test that\n"
            "supplies deliberately invalid credentials).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t003"
        ),
    ),
    RuleDefinition(
        id="T004",
        scope=RuleScope.APP,
        name="DevEntrypointRequiresAppModule",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dev-entrypoint",
        autofixable=False,
        since="0.10.0",
        rationale=(
            "application_sdk.main.main() is the production, "
            "ATLAN_APP_MODULE-driven launcher: it always calls "
            "AppConfig.from_args_and_env(args), which raises "
            "MissingAppModuleError unless ATLAN_APP_MODULE (or --app) is set. "
            "That is correct in production, where the base image's own CMD "
            "sets the env var and never even executes the repo's main.py. But "
            "main.py is also what CI's connector-integration-tests composite "
            "action runs directly ('python main.py') to boot the app for "
            "local/dev-mode testing, and the bootstrapped tests-reusable.yaml "
            "path exposes no input to inject ATLAN_APP_MODULE into that job. A "
            "main.py that delegates straight to application_sdk.main.main() "
            "therefore fails every PR with MissingAppModuleError / 'App server "
            "failed to start within 60s' (BLDX-1520)."
        ),
        short_description=(
            "Root main.py calls application_sdk.main.main() directly, which "
            "requires ATLAN_APP_MODULE and breaks CI's dev-mode boot"
        ),
        full_description=(
            "Root ``main.py`` must not call ``application_sdk.main.main()``\n"
            "directly (whether via ``from application_sdk.main import main``,\n"
            "an aliased module import, or a bare dotted call).\n"
            "\n"
            "``main()`` always resolves its ``App`` class from\n"
            "``ATLAN_APP_MODULE``/``--app`` — there is no way to supply it any\n"
            "other way.  That is the right contract for the production\n"
            "container, which never runs ``main.py`` at all (the base image's\n"
            "own CMD sets ``ATLAN_APP_MODULE`` and boots directly).  But\n"
            "``main.py`` *is* what CI's ``connector-integration-tests``\n"
            "composite action runs directly (``python main.py``) to boot the\n"
            "app for local/dev-mode testing, and the bootstrapped\n"
            "``tests-reusable.yaml`` path has no input that lets a caller\n"
            "inject ``ATLAN_APP_MODULE`` into that job.  A ``main.py`` wired\n"
            "this way fails every PR with ``MissingAppModuleError``.\n"
            "\n"
            "**Remediation:** delegate to a local dev entrypoint —\n"
            "conventionally ``app/run_dev.py`` — that constructs your ``App``\n"
            "subclass directly and calls ``run_dev_combined(MyApp, ...)``: no\n"
            "env var required.  See ``atlan-metabase-app``,\n"
            "``atlan-openapi-app``, or ``atlan-mysql-app`` for the reference\n"
            "pattern::\n"
            "\n"
            "    # main.py\n"
            "    import asyncio\n"
            "    from app.run_dev import main\n"
            "\n"
            "    if __name__ == '__main__':\n"
            "        asyncio.run(main())\n"
            "\n"
            "Suppress with ``# conformance: ignore[T004] <reason>`` on the\n"
            "call's line when the app genuinely has no local dev-mode boot\n"
            "path and relies on ``ATLAN_APP_MODULE`` being set out-of-band\n"
            "even for CI (e.g. some utility/CSA apps).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t004"
        ),
    ),
    RuleDefinition(
        id="T005",
        scope=RuleScope.BOTH,
        name="AssertionFreeTest",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-assertion-quality",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "Code coverage measures whether a line executed, not whether anything was "
            "verified about its behaviour. A test function that calls the code under "
            "test but never asserts on the outcome inflates the coverage percentage "
            "while providing zero protection against a regression — it passes whether "
            "the code is correct, subtly wrong, or completely broken, as long as it "
            "doesn't raise. This is the single most common way 'meaningful test "
            "coverage' targets are gamed unintentionally: a developer writes a test "
            "that exercises a code path to satisfy a coverage gate, intending to add "
            "assertions later, and the assertions never arrive. Flagging this "
            "deterministically closes the gap between 'the coverage tool is green' and "
            "'the tests actually verify something.'"
        ),
        short_description=(
            "Test has a non-empty body but no recognised assertion — it runs but "
            "verifies nothing"
        ),
        full_description=(
            "A collected test function (``test*``, including methods of a ``Test*``\n"
            "class) has a non-empty body but contains none of the recognised\n"
            "assertion forms::\n"
            "\n"
            "    a bare `assert` statement\n"
            "    `with pytest.raises(...)` / `pytest.warns(...)` / `pytest.deprecated_call(...)`\n"
            "    a call named `assert_*` (`self.assertEqual`, `mock.assert_called_once`,\n"
            "        `pandas.testing.assert_frame_equal`, a project-local `_assert_*` helper)\n"
            "    `pytest.fail(...)` / `self.fail(...)`\n"
            "    an SDK integration-test scenario-helper call: `.equals` / `.contains` /\n"
            "        `.exists` / `.is_dict` / `.is_string` / `.is_true` / `.is_list`\n"
            "\n"
            "This vocabulary is intentionally broad — the check is biased toward zero\n"
            "false positives at WARN tier rather than toward catching every possible\n"
            "assertion idiom, mirroring T001's documented-limits approach.\n"
            "\n"
            "**Remediation:** add an assertion on the outcome you actually care about.\n"
            "Before::\n"
            "\n"
            "    def test_extracts_users():\n"
            "        result = extract_users(client)\n"
            "\n"
            "After::\n"
            "\n"
            "    def test_extracts_users():\n"
            "        result = extract_users(client)\n"
            "        assert result.record_count == 3\n"
            "\n"
            "Suppress with ``# conformance: ignore[T005] <reason>`` only for a test\n"
            "whose sole purpose is confirming the call doesn't raise (rare — usually\n"
            "better expressed as ``pytest.raises``'s absence isn't a thing worth a\n"
            "dedicated test on its own; prefer folding the no-raise expectation into a\n"
            "test that also asserts on the return value).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t005"
        ),
    ),
    RuleDefinition(
        id="T006",
        scope=RuleScope.BOTH,
        name="EmptyTestBody",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-assertion-quality",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "A test whose body is only 'pass', '...', or a docstring is a placeholder "
            "that was scaffolded and never filled in. It is worse than an "
            "assertion-free test (T005): it doesn't even exercise the code under test, "
            "so it contributes to the visible test count without contributing any "
            "coverage at all. Left in place, it reads as 'this behaviour is tested' to "
            "anyone scanning the test file, which is actively misleading."
        ),
        short_description=("Test body is a stub — only 'pass', '...', or a docstring"),
        full_description=(
            "A collected test function's body consists solely of ``pass``, an\n"
            "``Ellipsis`` (``...``), a docstring, or some combination of those — no\n"
            "other statement is present.\n"
            "\n"
            "**Remediation:** either implement the test, or remove it. A stub that\n"
            "documents intent without a target date tends to stay a stub forever;\n"
            "prefer tracking the gap in an issue over leaving a placeholder that reads\n"
            "as tested coverage. If the test is genuinely not yet actionable, use\n"
            "``@pytest.mark.skip(reason='<ticket> — not yet implemented')`` so pytest's\n"
            "own reporting surfaces it as skipped rather than passing silently.\n"
            "\n"
            "Suppress with ``# conformance: ignore[T006] <reason>`` on the ``def``\n"
            "line only for an intentionally-empty test used purely to assert\n"
            "collection/import succeeds (rare).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t006"
        ),
    ),
    RuleDefinition(
        id="T007",
        scope=RuleScope.BOTH,
        name="VacuousAssertion",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-assertion-quality",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "'assert True' and equivalents (assert 1, assert \"x\") satisfy T005's "
            "assertion-presence check but can never fail — they provide the visual "
            "appearance of verification with none of the substance. This is the "
            "quieter sibling of T005: a reviewer scanning for 'does this test have an "
            "assert' sees one and moves on, without noticing it is unconditionally "
            "true. Both are 'coverage without verification'; this one specifically "
            "targets a test whose entire assertion surface is a truism."
        ),
        short_description=(
            "Every assertion in this test is a constant-true expression that can "
            "never fail"
        ),
        full_description=(
            "A collected test's only assertion(s) evaluate a literal truthy constant\n"
            '(``assert True``, ``assert 1``, ``assert "non-empty string"``) rather\n'
            "than an expression whose value depends on the code under test. Such an\n"
            "assertion can never fail regardless of what the test exercised.\n"
            "\n"
            "**Remediation:** assert on something that actually depends on the call\n"
            "under test. Before::\n"
            "\n"
            "    def test_creates_asset():\n"
            "        asset = build_asset(record)\n"
            "        assert True  # created without error\n"
            "\n"
            "After::\n"
            "\n"
            "    def test_creates_asset():\n"
            "        asset = build_asset(record)\n"
            "        assert asset.qualified_name == 'default/mysql/db/table'\n"
            "\n"
            "Suppress with ``# conformance: ignore[T007] <reason>`` on the assert\n"
            "line only when the constant assertion is a deliberate reachability\n"
            "marker in a larger test body that also contains real assertions\n"
            "elsewhere (in which case T007 shouldn't fire in the first place — file\n"
            "a correction if it does).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t007"
        ),
    ),
    RuleDefinition(
        id="T008",
        scope=RuleScope.BOTH,
        name="UncollectableTestFile",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-collection",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "pytest only collects files matching its python_files convention "
            "(test_*.py / *_test.py by default). A file under a test-tier directory "
            "that defines def test_* functions or Test* classes but is named "
            "something else (helpers.py, connector_tests.py) is never collected — it "
            "contributes zero coverage and zero CI signal while looking, to anyone "
            "reading the directory listing, exactly like a real test file. This is a "
            "particularly dangerous failure mode because it is invisible in the "
            "pytest run output: there is no error, no skip, nothing — the tests "
            "simply never exist as far as CI is concerned."
        ),
        short_description=(
            "File defines test*/Test* collectables but its filename doesn't match "
            "pytest's collection glob — never collected"
        ),
        full_description=(
            "A ``.py`` file under a test-tier directory (``tests/unit``,\n"
            "``tests/integration``, ``tests/e2e``, ``tests/ui``) defines at least one\n"
            "``def test*`` function or ``class Test*``, but its own filename does not\n"
            "match pytest's default collection glob (``test_*.py`` / ``*_test.py``).\n"
            "pytest's default configuration never collects such a file, so every test\n"
            "it defines silently never runs.\n"
            "\n"
            "**Remediation:** rename the file to match the convention. Before::\n"
            "\n"
            "    tests/unit/connector_tests.py\n"
            "\n"
            "After::\n"
            "\n"
            "    tests/unit/test_connector.py\n"
            "\n"
            "Suppress with ``# conformance: ignore[T008] <reason>`` on the first line\n"
            "of the file only when the repo has a non-default ``python_files``\n"
            "override in ``pyproject.toml`` that legitimately collects this name (the\n"
            "check does not read that override — see the module docstring).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t008"
        ),
    ),
    RuleDefinition(
        id="T009",
        scope=RuleScope.BOTH,
        name="UnconditionalModuleSkip",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-collection",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "A module-level pytest.skip(..., allow_module_level=True) that is not "
            "nested inside an if/try guard unconditionally disables every test in the "
            "file on every run, in every environment, forever. This differs from the "
            "legitimate e2e pattern — 'if not os.environ.get(...): pytest.skip(...)' "
            "— which disables the file only when a real precondition (credentials, a "
            "live tenant) is absent, and re-enables it automatically once the "
            "precondition is met. An unconditional skip usually starts as a temporary "
            "'disable this flaky suite' workaround and is forgotten, silently zeroing "
            "out that file's contribution to coverage from that point on."
        ),
        short_description=(
            "Module-level pytest.skip(allow_module_level=True) is unconditional — "
            "the whole file is permanently disabled"
        ),
        full_description=(
            "A module-level call to ``pytest.skip(..., allow_module_level=True)``\n"
            "appears directly in the module body (not nested inside an ``if`` or\n"
            "``try`` statement), so it executes — and disables every test in the\n"
            "file — on every collection, unconditionally.\n"
            "\n"
            "The legitimate form guards the skip behind a real precondition, so the\n"
            "file re-enables itself once the precondition is satisfied::\n"
            "\n"
            "    if not os.environ.get('ATLAN_API_KEY'):\n"
            "        pytest.skip('e2e harness needs ATLAN_API_KEY', allow_module_level=True)\n"
            "\n"
            "That guarded form is **not** flagged by T009 — only a bare, unguarded\n"
            "call at module scope is.\n"
            "\n"
            "**Remediation:** either delete the file's tests (if they are genuinely\n"
            "obsolete) or replace the unconditional skip with a real precondition\n"
            "guard, or with ``@pytest.mark.skip(reason='<ticket>')`` on the individual\n"
            "tests that are temporarily disabled — which at least reports as a\n"
            "visible per-test skip in CI output rather than silently vanishing at\n"
            "collection time.\n"
            "\n"
            "Suppress with ``# conformance: ignore[T009] <reason>`` on the ``skip(...)``\n"
            "call's line when the file is intentionally, permanently disabled pending\n"
            "removal in a tracked follow-up.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t009"
        ),
    ),
    RuleDefinition(
        id="T010",
        scope=RuleScope.APP,
        name="MissingUnitTestSuite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-tier-coverage",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "Unit tests — method-by-method coverage of helper functions and "
            "activities — are the universal floor of the agreed testing-tier "
            "architecture: every canonical app, including the minimal hello-world "
            "scaffold, has one. An app with no tests/unit/ directory (or one with no "
            "collectable tests in it) has no fast, hermetic verification of its own "
            "logic at all — every other tier (integration, e2e) is slower, "
            "network-bound, and exercises the app only end-to-end, so a defect in a "
            "helper function has no tier positioned to catch it cheaply. Unlike "
            "T011/T012, this rule has no scaffold exemption: even the smallest app "
            "has some logic worth a fast unit test."
        ),
        short_description=("No collectable unit tests under tests/unit/"),
        full_description=(
            "No collectable pytest tests (``def test*`` / ``class Test*`` in a\n"
            "``test_*.py`` / ``*_test.py`` file) exist under ``tests/unit/``. This is\n"
            "the universal floor of the tiering architecture — unlike\n"
            "``tests/integration/`` and ``tests/e2e/`` (T011/T012), this tier has no\n"
            "``exempt_test_tiers`` opt-out: every canonical app, including the minimal\n"
            "``hello-world`` scaffold, ships a real unit suite.\n"
            "\n"
            "**Remediation:** add ``tests/unit/test_<module>.py`` files exercising the\n"
            "app's helper functions and ``@task``-decorated activities directly (call\n"
            "them as coroutines — the decorator only attaches metadata outside the\n"
            "workflow runtime). See ``atlan-hello-world-app/tests/unit/`` for the\n"
            "minimal reference shape: typed ``Input``/``Output`` contracts, a\n"
            "``pytest.fixture`` for the app instance, and real outcome assertions\n"
            "(record counts, on-disk side effects, error paths via\n"
            "``pytest.raises``).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t010"
        ),
    ),
    RuleDefinition(
        id="T011",
        scope=RuleScope.APP,
        name="MissingIntegrationTestSuite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-tier-coverage",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "Integration tests — connecting to the real source and running the app's "
            "extract only, no system apps — are where most scenario variations "
            "(auth modes, schema shapes, include/exclude filters) belong per the "
            "agreed tiering architecture; the SDK provides hermetic paths for this "
            "tier (embedded Temporal, testcontainers, mocked infra) so there is no "
            "cost excuse for skipping it. An app with no tests/integration/ suite has "
            "no verification that its extraction logic works against anything "
            "resembling the real source. Scaffold/minimal apps that genuinely have no "
            "external source to integrate against (e.g. a template with no connector "
            "logic yet) can opt out via [tool.conformance].exempt_test_tiers in "
            "pyproject.toml — atlan.yaml is generated from the Pkl contract and must "
            "not be hand-edited, so the exemption can't live there."
        ),
        short_description=("No collectable integration tests under tests/integration/"),
        full_description=(
            "No collectable pytest tests exist under ``tests/integration/``. Per the\n"
            "agreed tiering architecture, integration tests connect to the real\n"
            "source and run the app's extract path (no system apps) — this is where\n"
            "most scenario-variation coverage belongs, and the SDK ships hermetic\n"
            "paths for it (embedded Temporal dev server, testcontainers, or mocked\n"
            "infra — see ``atlan-mysql-app``/``atlan-metabase-app``/\n"
            "``atlan-openapi-app`` for the reference shapes).\n"
            "\n"
            "**Remediation:** add an integration suite under ``tests/integration/``\n"
            "using one of the SDK's hermetic test paths, marked so the unit job\n"
            "deselects it (see T001).\n"
            "\n"
            "**Exemption:** for a scaffold/minimal app with no external source to\n"
            "integrate against yet, add to the app's ``pyproject.toml``:\n"
            "\n"
            ".. code-block:: toml\n"
            "\n"
            "    [tool.conformance]\n"
            '    exempt_test_tiers = ["integration"]\n'
            "\n"
            "State the reason in a comment above the table. Suppress a single\n"
            "instance instead with ``# conformance: ignore[T011] <reason>`` on the\n"
            "first line of ``pyproject.toml``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t011"
        ),
    ),
    RuleDefinition(
        id="T012",
        scope=RuleScope.APP,
        name="MissingE2ETestSuite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-tier-coverage",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "End-to-end tests — the full pipeline including system apps, operating "
            "in SDR mode against a real tenant — are the tier that catches "
            "integration failures between the app and the platform itself (AE "
            "dispatch, agent routing, upload gating) that a tests/integration/ suite "
            "cannot see because it deliberately excludes system apps. Per the agreed "
            "architecture, e2e needs only one representative run, not "
            "scenario-level coverage, so this rule is the weakest of the three tier "
            "rules — it only asks that the tier exist at all. Exemptable the same way "
            "as T011 for scaffold/minimal apps via [tool.conformance].exempt_test_tiers."
        ),
        short_description=("No collectable end-to-end tests under tests/e2e/"),
        full_description=(
            "No collectable pytest tests exist under ``tests/e2e/``. Per the agreed\n"
            "tiering architecture this tier needs only one representative run — the\n"
            "full pipeline including system apps, in SDR mode against a real tenant\n"
            "— not scenario-level coverage (that belongs to ``tests/integration/``,\n"
            "T011). See ``atlan-mysql-app``/``atlan-metabase-app``/\n"
            "``atlan-openapi-app`` for the reference shape: a thin test class\n"
            "inheriting from the SDK-generated ``*GeneratedE2EBase``, double\n"
            "env-guarded (skips without ``ATLAN_BASE_URL``/``ATLAN_API_KEY`` and\n"
            "without the harness import), marked ``@pytest.mark.e2e``.\n"
            "\n"
            "**Remediation:** add a representative e2e test under ``tests/e2e/``\n"
            "following that pattern.\n"
            "\n"
            "**Exemption:** for a scaffold/minimal app with no system-app integration\n"
            "to exercise yet, add to the app's ``pyproject.toml``:\n"
            "\n"
            ".. code-block:: toml\n"
            "\n"
            "    [tool.conformance]\n"
            '    exempt_test_tiers = ["e2e"]\n'
            "\n"
            "State the reason in a comment above the table. Suppress a single\n"
            "instance instead with ``# conformance: ignore[T012] <reason>`` on the\n"
            "first line of ``pyproject.toml``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t012"
        ),
    ),
    RuleDefinition(
        id="T013",
        scope=RuleScope.BOTH,
        name="TestFileOutsideTierDir",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test-tier-coverage",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "CI's composite actions locate each tier by directory convention — "
            "connector-unit-tests runs tests/unit, the integration action defaults "
            "to auto-discovery but is commonly scoped to tests/integration, and the "
            "sdr-e2e/e2e-full-reusable actions default to tests/sdr or tests/e2e or "
            "tests/full_dag. A collectable test file placed loose under tests/ (or in "
            "an ad hoc subdirectory outside the four canonical tier dirs) may still "
            "get picked up by a broad auto-discovery run, or may not — depending on "
            "exactly how the calling workflow scoped test-paths — making its actual "
            "execution status ambiguous from the file layout alone. Enforcing the "
            "placement convention removes that ambiguity."
        ),
        short_description=(
            "Collectable test file lives outside the four canonical tier "
            "directories (tests/unit, tests/integration, tests/e2e, tests/ui)"
        ),
        full_description=(
            "A file matching pytest's collection glob (``test_*.py`` / ``*_test.py``)\n"
            "and defining at least one collectable test lives under ``tests/`` but\n"
            "outside all four canonical tier directories\n"
            "(``tests/unit``, ``tests/integration``, ``tests/e2e``, ``tests/ui``) —\n"
            "for example directly in ``tests/`` itself, or under an ad hoc\n"
            "subdirectory like ``tests/scratch/``.\n"
            "\n"
            "**Remediation:** move the file into the tier directory matching what it\n"
            "actually tests — a file with no external I/O belongs in\n"
            "``tests/unit/``; a file connecting to a real source belongs in\n"
            "``tests/integration/``.\n"
            "\n"
            "Suppress with ``# conformance: ignore[T013] <reason>`` on the file's\n"
            "first line for intentional non-tier test infrastructure that happens to\n"
            "match the collection glob (rare — prefer a filename that doesn't match\n"
            "the glob for pure helpers, which also avoids T008-adjacent confusion).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t013"
        ),
    ),
    RuleDefinition(
        id="T014",
        scope=RuleScope.APP,
        name="CoverageGateDisabled",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="coverage-config",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "A coverage percentage that cannot fail a build is decorative: it is "
            "reported in every PR comment and dashboard, creating the appearance of "
            "an enforced quality bar, while [tool.coverage.report].fail_under absent "
            "or 0 means no percentage — however low — actually blocks anything. This "
            "is the config-level counterpart to T005-T007: those catch tests that run "
            "without asserting; this catches a coverage number that exists without "
            "enforcing. The unified test-framework onboarding path deliberately "
            "starts new adopters at --cov-fail-under=0 and ramps up over time (Athena "
            "at 20%, mssql at 60%), so WARN (not BLOCK) matches the agreed rollout "
            "reality — this rule's value is making the '0 is temporary, not the "
            "final state' expectation visible and trackable, not blocking the "
            "initial adoption PR."
        ),
        short_description=(
            "Coverage is configured but fail_under is absent or 0 — the number is "
            "measured but never enforced"
        ),
        full_description=(
            "``[tool.coverage.report]`` exists in ``pyproject.toml`` — the repo has\n"
            "opted into coverage measurement — but ``fail_under`` is either absent\n"
            "(defaults to 0) or explicitly set to ``0``, *and* no CI workflow\n"
            "declares an overriding floor. Coverage is measured and reported (e.g.\n"
            "as a PR comment via the ``connector-unit-tests`` composite action) but\n"
            "can never cause a run to fail, regardless of how low it drops.\n"
            "\n"
            "coverage.py's CLI flag always overrides ``pyproject.toml``, so this\n"
            "rule also checks the repo's own ``.github/workflows/*.yml`` for a\n"
            "``connector-unit-tests`` ``fail-under:`` input or a\n"
            "``--cov-fail-under=N`` flag embedded in a ``tests-reusable.yaml``\n"
            "``pytest-args`` override. Either one, if non-zero, is treated as the\n"
            "effective floor — the finding only fires when neither source enforces\n"
            "anything.\n"
            "\n"
            "**Remediation:** set a real, ratcheting floor:\n"
            "\n"
            ".. code-block:: toml\n"
            "\n"
            "    [tool.coverage.report]\n"
            "    fail_under = 60\n"
            "\n"
            "Per the unified test-framework's own onboarding guidance, start at the\n"
            "repo's *current* measured percentage (never below what's already true)\n"
            "and raise it in follow-up PRs as coverage improves — the agreed target\n"
            "for unit tests is 90-100%, but a repo mid-adoption is not expected to\n"
            "jump there in one step.\n"
            "\n"
            "Suppress with ``# conformance: ignore[T014] <reason>`` on the\n"
            "``[tool.coverage.report]`` line only during the initial adoption PR\n"
            "itself, explicitly naming the follow-up tracking issue that will set a\n"
            "real floor.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t014"
        ),
    ),
    RuleDefinition(
        id="T015",
        scope=RuleScope.APP,
        name="CoverageOmitsProductCode",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="coverage-config",
        autofixable=False,
        since="0.12.0",
        rationale=(
            "[tool.coverage.run].omit (or a narrowed source) controls the "
            "denominator of the coverage percentage: excluding real product code "
            "under app/ makes the percentage look higher without a single additional "
            "test being written, which is a more direct form of gaming than "
            "T014's disabled gate — the number moves in the intended direction while "
            "measuring less of what actually ships. Legitimate omissions exist (test "
            "helpers, generated code under app/generated/, vendored code) but those "
            "are not product logic; a pattern that reaches into ordinary app/ "
            "submodules is the signal this rule targets."
        ),
        short_description=(
            "coverage omit/source excludes real product code under app/, inflating "
            "the reported percentage"
        ),
        full_description=(
            "``[tool.coverage.run].omit`` contains a pattern matching source under\n"
            "``app/`` that is not one of the recognised legitimate exclusions\n"
            "(``app/generated/**`` — generated contract artifacts;\n"
            "``**/test_*.py``/``**/conftest.py`` — test infra that happens to live\n"
            "under ``app/`` in some layouts), or ``[tool.coverage.run].source`` is\n"
            "narrowed to a subset of ``app/`` that excludes real handler/mapper/\n"
            "client modules.\n"
            "\n"
            "**Remediation:** narrow the omission to only what shouldn't count —\n"
            "generated code and test infra — and let real product modules\n"
            "contribute to (and be held to) the coverage floor. Before:\n"
            "\n"
            ".. code-block:: toml\n"
            "\n"
            "    [tool.coverage.run]\n"
            '    omit = ["app/handlers/*", "app/clients/*"]\n'
            "\n"
            "After:\n"
            "\n"
            ".. code-block:: toml\n"
            "\n"
            "    [tool.coverage.run]\n"
            '    omit = ["app/generated/*"]\n'
            "\n"
            "Suppress with ``# conformance: ignore[T015] <reason>`` on the ``omit``/\n"
            "``source`` line naming the specific module and why it's legitimately\n"
            "excluded (e.g. a vendored third-party shim with no branch logic worth\n"
            "covering).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/tests.md#t015"
        ),
    ),
)
