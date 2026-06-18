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
)
