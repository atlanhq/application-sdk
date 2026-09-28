"""B009 DeprecatedHandlerImportPath — the handler surface moved to application_sdk_api."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks.deprecation import scan_all, scan_handler_import_path
from conformance.suite.checks.deprecation._handler_path import (
    DEPRECATED_HANDLER_MODULES,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope


def _b009(src: str) -> list:
    return scan_handler_import_path(
        ast.parse(src), "app/handler.py", _parse_directives(src)
    )


# ── fires ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        "from application_sdk.handler import Handler\n",
        "from application_sdk.handler.base import Handler\n",
        "from application_sdk.handler.contracts import PreflightInput, PreflightOutput\n",
        "from application_sdk.handler.context import HandlerContext\n",
        "from application_sdk.handler.manifest import ManifestRoute\n",
        "from application_sdk.handler.service_errors import ServiceError\n",
        "from application_sdk.handler import *\n",
        "def f():\n    from application_sdk.handler import Handler\n",
    ],
)
def test_fires_on_a_from_import_of_a_handler_surface_name(src: str) -> None:
    (finding,) = _b009(src)
    assert finding.rule_id == "B009"
    assert "application_sdk_api.handler" in finding.message
    assert "v4.0" in finding.message


@pytest.mark.parametrize(
    "src",
    [
        "import application_sdk.handler.contracts as hc\nhc.PreflightInput\n",
        "import application_sdk.handler.contracts\n"
        "application_sdk.handler.contracts.PreflightInput\n",
        "import application_sdk.handler\napplication_sdk.handler.Handler\n",
        "from application_sdk import handler\nhandler.Handler\n",
        "from application_sdk.handler import contracts\ncontracts.PreflightInput\n",
        # Bound but never dereferenced: nothing shows it is for worker names.
        "import application_sdk.handler.contracts as hc\n",
        # Used bare: cannot be resolved to names.
        "import application_sdk.handler.contracts as hc\nregister(hc)\n",
    ],
)
def test_fires_on_a_bound_deprecated_module(src: str) -> None:
    assert [f.rule_id for f in _b009(src)] == ["B009"]


def test_mixed_import_is_one_finding_naming_only_the_deprecated_names() -> None:
    (finding,) = _b009(
        "from application_sdk.handler.contracts import PreflightGateMode, PreflightInput\n"
    )
    assert "'PreflightInput'" in finding.message
    assert "Keep 'PreflightGateMode'" in finding.message


# ── silent ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        "from application_sdk_api.handler import Handler\n",
        "from application_sdk_api.handler.contracts import PreflightInput\n",
        "import application_sdk_api.handler.contracts as hc\nhc.PreflightInput\n",
        "from application_sdk.errors import AppError\n",
        "from application_sdk.errors.base import AppError\n",
        "from application_sdk.handler.service import create_app_handler_service\n",
        "from application_sdk.handler.invocation import bind_invocation_context\n",
        "from application_sdk.handler.context import bind_invocation_context\n",
        "from application_sdk.handler import create_app_handler_service\n",
        "from application_sdk.handler import service\n",
        "from application_sdk.handler.contracts import (\n"
        "    PreflightGateMode, EventTriggerConfig, EventFilterRule,\n"
        "    SubscriptionConfig, CloudEventEnvelope, FileUploadResponse,\n"
        ")\n",
        "from application_sdk.handler import PreflightGateMode\n",
        "import application_sdk.handler.contracts as hc\nhc.PreflightGateMode\n",
        "from .handler import Handler\n",
        "from app.handler import Handler\n",
    ],
)
def test_silent(src: str) -> None:
    assert _b009(src) == []


def test_suppressed_inline() -> None:
    src = (
        "from application_sdk.handler import Handler"
        "  # conformance: ignore[B009] migrating in the next release\n"
    )
    (finding,) = _b009(src)
    assert finding.suppressed
    assert finding.suppression_justification == "migrating in the next release"


def test_worker_surface_sets_mirror_the_sdk_shims() -> None:
    """The exempt sets are copied from each shim's ``_NOT_DEPRECATED``; pin them."""
    root = Path(__file__).resolve().parents[3] / "application_sdk" / "handler"
    if not root.is_dir():
        pytest.skip("SDK source not alongside the conformance package")
    for module, exempt in DEPRECATED_HANDLER_MODULES.items():
        rel = module.split(".")[2:] or ["__init__"]
        tree = ast.parse((root / f"{rel[0]}.py").read_text(encoding="utf-8"))
        declared: set[str] | None = None
        for node in tree.body:
            target = node.target if isinstance(node, ast.AnnAssign) else None
            if (
                isinstance(target, ast.Name)
                and target.id == "_NOT_DEPRECATED"
                and node.value is not None
            ):
                declared = {
                    c.value
                    for c in ast.walk(node.value)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                }
        assert declared is not None, f"{module}: no _NOT_DEPRECATED set"
        assert declared == set(exempt), module


# ── runner integration + metadata ────────────────────────────────────────────


def test_scan_all_reports_b009_on_an_app(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "atlan-demo-app"\n')
    app = tmp_path / "app"
    app.mkdir()
    (app / "handler.py").write_text("from application_sdk.handler import Handler\n")
    findings = scan_all([app / "handler.py"], tmp_path)
    assert [f.rule_id for f in findings if f.rule_id == "B009"] == ["B009"]


def _app_with_lock(root: Path, lock: str | None) -> Path:
    (root / "pyproject.toml").write_text('[project]\nname = "atlan-demo-app"\n')
    if lock is not None:
        (root / "uv.lock").write_text(lock)
    app = root / "app"
    app.mkdir()
    (app / "handler.py").write_text("from application_sdk.handler import Handler\n")
    return app / "handler.py"


_SDK_ONLY_LOCK = (
    'version = 1\n\n[[package]]\nname = "atlan-application-sdk"\nversion = "3.39.1"\n'
)


def test_not_evaluated_when_the_lock_predates_the_api_package(tmp_path: Path) -> None:
    """On an SDK without atlan-application-sdk-api the old path is the real module."""
    handler = _app_with_lock(tmp_path, _SDK_ONLY_LOCK)
    assert [f for f in scan_all([handler], tmp_path) if f.rule_id == "B009"] == []


def test_evaluated_when_the_lock_resolves_the_api_package(tmp_path: Path) -> None:
    lock = _SDK_ONLY_LOCK + (
        '\n[[package]]\nname = "atlan-application-sdk-api"\nversion = "3.40.0"\n'
    )
    handler = _app_with_lock(tmp_path, lock)
    assert [
        f.rule_id for f in scan_all([handler], tmp_path) if f.rule_id == "B009"
    ] == ["B009"]


def test_evaluated_when_the_lock_is_unreadable(tmp_path: Path) -> None:
    handler = _app_with_lock(tmp_path, "this is not toml = = =\n")
    assert [
        f.rule_id for f in scan_all([handler], tmp_path) if f.rule_id == "B009"
    ] == ["B009"]


def test_rule_metadata() -> None:
    rule = get_rule("B009")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.autofixable
