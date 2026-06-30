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
            "provide.  P026 closes the static manifest gap; T003 closes the test\n"
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
)
