"""B009 ``RemovedSdkImport`` — an app importing an SDK module that no longer exists.

FND-4661: six connector e2e test files still import
``application_sdk.test_utils.e2e``, removed in v3.0.0, so none of them has
collected since. B009 reports the import from a static registry of removed
modules; the registry test at the bottom holds every entry against the SDK
tree so the table cannot drift from what the SDK actually ships.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks._version import parse_version
from conformance.suite.checks.deprecation import scan_all
from conformance.suite.checks.deprecation._removed_imports import (
    REMOVED_SDK_MODULES,
    removed_entry,
    scan_removed_imports,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_OWN = frozenset({"app", "tests", "local", "scripts"})


def _b009(src: str, own: frozenset[str] = _OWN) -> list:
    return scan_removed_imports(
        ast.parse(src), "tests/e2e/test_x.py", _parse_directives(src), own
    )


# ── the fleet shapes ────────────────────────────────────────────────────────


def test_b009_fires_on_the_fnd_4661_import() -> None:
    """The line the six connector e2e files carry."""
    findings = _b009("from application_sdk.test_utils.e2e.base import BaseTest\n")
    assert [f.rule_id for f in findings] == ["B009"]
    message = findings[0].message
    assert "application_sdk.test_utils.e2e" in message
    assert "v3.0.0" in message
    assert "application_sdk.testing.e2e" in message


@pytest.mark.parametrize(
    "src",
    [
        "import application_sdk.test_utils.e2e\n",
        "import application_sdk.workflows.metadata_extraction.sql as sql\n",
        "from application_sdk.test_utils import e2e\n",
        "from application_sdk import workflows\n",
        "from application_sdk.handlers.sql import BaseSQLHandler\n",
    ],
)
def test_b009_fires_on_every_import_shape(src: str) -> None:
    assert [f.rule_id for f in _b009(src)] == ["B009"]


def test_b009_reports_one_finding_per_statement() -> None:
    """Two removed members in one ``from`` line are one dead import, not two."""
    src = "from application_sdk import workflows, activities\n"
    assert len(_b009(src)) == 1


def test_b009_message_points_v3_removals_at_the_upgrade_guide() -> None:
    (finding,) = _b009("from application_sdk.services.objectstore import X\n")
    assert "upgrade-guide-v3" in finding.message


def test_b009_message_for_a_later_removal_names_its_version() -> None:
    (finding,) = _b009("from application_sdk.common.exc_utils import rewrap\n")
    assert "v3.12.0" in finding.message


# ── what it must not flag ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        # Live modules, including ones that share a prefix with a removed one.
        "from application_sdk.testing.e2e import BaseE2ETest\n",
        "from application_sdk.testing.integration import Scenario\n",
        "from application_sdk.outputs import get_outputs\n",
        "from application_sdk.handler import Handler\n",
        "import application_sdk.workflows_v2\n",
        # Not the SDK at all.
        "from workflows import x\n",
        "import services\n",
        # Relative imports are the app's own.
        "from .test_utils.e2e import helper\n",
    ],
)
def test_b009_ignores_live_and_foreign_modules(src: str) -> None:
    assert _b009(src) == []


@pytest.mark.parametrize(
    "handler", ["ImportError", "ModuleNotFoundError", "(ImportError, OSError)", ""]
)
def test_b009_ignores_an_import_probe(handler: str) -> None:
    """A guarded import survives the module's absence, so it is not dead."""
    src = (
        "try:\n"
        "    from application_sdk.test_utils.e2e.base import BaseTest\n"
        f"except {handler}:\n"
        "    BaseTest = None\n"
    )
    assert _b009(src) == []


def test_b009_still_fires_when_the_try_catches_something_else() -> None:
    src = (
        "try:\n"
        "    from application_sdk.test_utils.e2e.base import BaseTest\n"
        "except KeyError:\n"
        "    pass\n"
    )
    assert [f.rule_id for f in _b009(src)] == ["B009"]


@pytest.mark.parametrize(
    "handler", ["app_errors.ImportError", "errors.ModuleNotFoundError"]
)
def test_b009_fires_when_the_handler_is_a_foreign_same_named_class(
    handler: str,
) -> None:
    """``app_errors.ImportError`` may not catch the built-in, so no probe."""
    src = (
        "try:\n"
        "    from application_sdk.test_utils.e2e.base import BaseTest\n"
        f"except {handler}:\n"
        "    pass\n"
    )
    assert [f.rule_id for f in _b009(src)] == ["B009"]


def test_b009_accepts_an_explicit_builtins_handler() -> None:
    src = (
        "try:\n"
        "    from application_sdk.test_utils.e2e.base import BaseTest\n"
        "except builtins.ImportError:\n"
        "    pass\n"
    )
    assert _b009(src) == []


@pytest.mark.parametrize("kind", ["def", "async def"])
def test_b009_fires_on_an_import_deferred_into_a_function(kind: str) -> None:
    """The function runs after the ``try`` ends, so the handler guards nothing."""
    src = (
        "try:\n"
        f"    {kind} load():\n"
        "        from application_sdk.workflows import x\n"
        "except ImportError:\n"
        "    pass\n"
    )
    assert [f.rule_id for f in _b009(src)] == ["B009"]


def test_b009_fires_on_a_method_import_but_not_a_class_body_import() -> None:
    """A class body runs inside the ``try``; its methods do not."""
    src = (
        "try:\n"
        "    class Shim:\n"
        "        from application_sdk.test_utils.e2e import base\n"
        "        def load(self):\n"
        "            from application_sdk.workflows import x\n"
        "except ImportError:\n"
        "    pass\n"
    )
    findings = _b009(src)
    assert [f.rule_id for f in findings] == ["B009"]
    assert "application_sdk.workflows" in findings[0].message


def test_b009_is_silent_in_a_repo_that_owns_application_sdk() -> None:
    """The SDK (or a vendored copy) is the authority on its own tree."""
    src = "from application_sdk.workflows import x\n"
    assert _b009(src, own=_OWN | {"application_sdk"}) == []


def test_b009_is_suppressible_inline() -> None:
    src = (
        "# conformance: ignore[B009] kept as a migration reference until FND-1 lands\n"
        "from application_sdk.test_utils.e2e.base import BaseTest\n"
    )
    (finding,) = _b009(src)
    assert finding.suppressed is True


def test_b009_runs_on_a_consumer_app_through_scan_all(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "atlan-example-app"\nversion = "0.1.0"\n'
    )
    test_file = tmp_path / "tests" / "e2e" / "test_old.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("from application_sdk.test_utils.e2e.base import BaseTest\n")
    findings = [f for f in scan_all([test_file], tmp_path) if f.rule_id == "B009"]
    assert len(findings) == 1


# ── the rule and its registry ───────────────────────────────────────────────


def test_b009_is_an_app_scoped_warn_rule() -> None:
    rule = get_rule("B009")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN


def test_removed_entry_matches_whole_components_only() -> None:
    assert removed_entry("application_sdk.workflows") is not None
    assert removed_entry("application_sdk.workflows.sql") is not None
    assert removed_entry("application_sdk.workflowsx") is None
    assert removed_entry("application_sdk") is None


def test_registry_versions_parse() -> None:
    bad = [
        m for m, e in REMOVED_SDK_MODULES.items() if parse_version(e.removed_in) is None
    ]
    assert not bad, f"unparseable removed_in: {bad}"


def test_registry_has_no_nested_entries() -> None:
    """Entries are the topmost removed package; a child entry is dead weight."""
    nested = [
        child
        for child in REMOVED_SDK_MODULES
        for parent in REMOVED_SDK_MODULES
        if child != parent and child.startswith(parent + ".")
    ]
    assert not nested, f"entries nested under another entry: {nested}"


def _find_sdk_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "application_sdk" / "__init__.py").is_file():
            return parent
    return None


def test_registry_names_only_modules_absent_from_the_sdk_tree() -> None:
    """A module that comes back must leave the registry, or B009 flags live code."""
    root = _find_sdk_root()
    if root is None:
        pytest.skip("application_sdk/ source tree not available (installed wheel)")
    present = []
    for module in REMOVED_SDK_MODULES:
        path = root.joinpath(*module.split("."))
        if path.with_suffix(".py").exists() or path.is_dir():
            present.append(module)
    assert not present, f"registry entries that exist in application_sdk/: {present}"


def test_registry_replacements_exist_in_the_sdk_tree() -> None:
    root = _find_sdk_root()
    if root is None:
        pytest.skip("application_sdk/ source tree not available (installed wheel)")
    missing = []
    for entry in REMOVED_SDK_MODULES.values():
        if entry.replacement is None:
            continue
        path = root.joinpath(*entry.replacement.split("."))
        if not (path.with_suffix(".py").exists() or path.is_dir()):
            missing.append(entry.replacement)
    assert not missing, f"replacement modules not in application_sdk/: {missing}"
