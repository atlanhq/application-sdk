"""Tests for O007 UploadRetainsLocalCopy.

The rule fires on SDK ``upload_file`` calls that keep the local file — the
keyword omitted, or passed as the literal ``True`` — and only on calls whose
binding resolves to the SDK.  Both halves are pinned: every import shape the
fleet uses must fire, and every lookalike (an app helper of the same name, a
client method, the v2 classmethod) must stay silent, because a false positive
here asks an engineer to delete a file their code may still need.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite import runner
from conformance.suite.checks.optimizations import scan_text
from conformance.suite.checks.optimizations._upload_retain import (
    SDK_UPLOAD_MODULES,
    collect_upload_bindings,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope


def _o007(src: str) -> list[int]:
    """Lines of the O007 findings in *src*."""
    return [f.line for f in scan_text(src, "app/x.py") if f.rule_id == "O007"]


_IMPORT = "from application_sdk.storage import upload_file\n\n"


# ── fires ──────────────────────────────────────────────────────────────────────


def test_fires_when_retain_local_copy_is_omitted() -> None:
    src = _IMPORT + "async def f(p):\n    await upload_file('k', p)\n"
    assert _o007(src) == [4]


def test_fires_on_literal_true() -> None:
    src = (
        _IMPORT
        + "async def f(p):\n    await upload_file('k', p, retain_local_copy=True)\n"
    )
    findings = [f for f in scan_text(src, "app/x.py") if f.rule_id == "O007"]
    assert [f.line for f in findings] == [4]
    assert "retain_local_copy=True" in findings[0].message


@pytest.mark.parametrize("module", sorted(SDK_UPLOAD_MODULES))
def test_fires_for_every_module_that_exports_the_sdk_function(module: str) -> None:
    src = f"from {module} import upload_file\n\nasync def f(p):\n    await upload_file('k', p)\n"
    assert _o007(src) == [4]


def test_fires_through_an_aliased_from_import() -> None:
    src = (
        "from application_sdk.storage.ops import upload_file as storage_upload\n\n"
        "async def f(p, store):\n"
        "    await storage_upload('k', local_path=p, store=store)\n"
    )
    findings = [f for f in scan_text(src, "app/x.py") if f.rule_id == "O007"]
    assert [f.line for f in findings] == [4]
    # The message names the call as written, so an aliased site is findable.
    assert findings[0].message.startswith("storage_upload() (the SDK's upload_file)")


def test_fires_through_a_module_alias() -> None:
    src = (
        "import application_sdk.storage.ops as ops\n\n"
        "async def f(p):\n    await ops.upload_file('k', p)\n"
    )
    assert _o007(src) == [4]


def test_fires_through_a_from_imported_module() -> None:
    src = (
        "from application_sdk import storage\n"
        "from application_sdk.storage import ops\n\n"
        "async def f(p):\n"
        "    await storage.upload_file('k', p)\n"
        "    await ops.upload_file('k', p)\n"
    )
    assert _o007(src) == [5, 6]


def test_fires_through_the_fully_dotted_path() -> None:
    src = (
        "import application_sdk.storage.ops\n\n"
        "async def f(p):\n    await application_sdk.storage.ops.upload_file('k', p)\n"
    )
    assert _o007(src) == [4]


def test_fires_on_a_function_local_import() -> None:
    src = (
        "async def f(p):\n"
        "    from application_sdk.storage import upload_file\n"
        "    await upload_file('k', p)\n"
    )
    assert _o007(src) == [3]


def test_fires_on_each_call_in_a_module() -> None:
    src = (
        _IMPORT + "async def f(paths):\n"
        "    for p in paths:\n"
        "        await upload_file('k', p)\n"
        "    await upload_file('k2', paths[0], retain_local_copy=True)\n"
    )
    assert _o007(src) == [5, 6]


# ── silent: compliant or unknowable ────────────────────────────────────────────


def test_silent_on_literal_false() -> None:
    src = (
        _IMPORT
        + "async def f(p):\n    await upload_file('k', p, retain_local_copy=False)\n"
    )
    assert _o007(src) == []


@pytest.mark.parametrize(
    "expr",
    ["keep", "self.keep_local", "not cleanup", "keep if debug else False", "flag()"],
)
def test_silent_on_a_non_literal_expression(expr: str) -> None:
    src = (
        _IMPORT + f"async def f(self, p, keep, cleanup, debug, flag):\n"
        f"    await upload_file('k', p, retain_local_copy={expr})\n"
    )
    assert _o007(src) == []


def test_silent_when_a_kwargs_splat_may_carry_the_keyword() -> None:
    src = _IMPORT + "async def f(p, opts):\n    await upload_file('k', p, **opts)\n"
    assert _o007(src) == []


# ── silent: import resolution negatives ────────────────────────────────────────


def test_silent_on_an_app_defined_upload_file() -> None:
    src = (
        "async def upload_file(key, path):\n    return key\n\n"
        "async def f(p):\n    await upload_file('k', p)\n"
    )
    assert _o007(src) == []


@pytest.mark.parametrize(
    "imp",
    [
        "from app.storage import upload_file",
        "from mylib.storage.ops import upload_file",
        "from .storage import upload_file",
        # transfer.py imports upload_file inside a function, so it exports nothing.
        "from application_sdk.storage.transfer import upload_file",
    ],
)
def test_silent_when_upload_file_comes_from_elsewhere(imp: str) -> None:
    src = f"{imp}\n\nasync def f(p):\n    await upload_file('k', p)\n"
    assert _o007(src) == []


def test_sdk_upload_modules_cover_every_installed_re_export() -> None:
    """Drift guard: a module that starts binding ``upload_file`` at top level is
    a new way to reach the SDK function, and the rule must learn it."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("application_sdk")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("application_sdk is not installed")
    root = Path(next(iter(spec.submodule_search_locations)))
    exporting: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root.parent).with_suffix("").parts
        module = ".".join(rel[:-1] if rel[-1] == "__init__" else rel)
        for node in tree.body:
            binds = (
                isinstance(node, ast.ImportFrom)
                and any((a.asname or a.name) == "upload_file" for a in node.names)
            ) or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "upload_file"
            )
            if binds:
                exporting.add(module)
    assert exporting, "found no module binding upload_file — the walk is broken"
    assert exporting <= SDK_UPLOAD_MODULES, sorted(exporting - SDK_UPLOAD_MODULES)


def test_silent_on_method_calls_even_with_the_sdk_function_imported() -> None:
    src = (
        _IMPORT + "async def f(self, client, p):\n"
        "    await client.upload_file('k', p)\n"
        "    await self.upload_file('k', p)\n"
        "    ObjectStore.upload_file(source=p, destination='k')\n"
    )
    assert _o007(src) == []


def test_silent_on_an_unrelated_module_alias() -> None:
    src = (
        "import app.storage as storage\n"
        "from app import ops\n\n"
        "async def f(p):\n"
        "    await storage.upload_file('k', p)\n"
        "    await ops.upload_file('k', p)\n"
    )
    assert _o007(src) == []


def test_silent_on_a_dotted_path_outside_the_sdk_modules() -> None:
    src = (
        "import application_sdk.storage.ops\n\n"
        "async def f(p):\n"
        "    await application_sdk.storage.transfer.upload_file('k', p)\n"
    )
    assert _o007(src) == []


def test_silent_on_sibling_sdk_functions() -> None:
    src = (
        "from application_sdk.storage import upload_file_from_bytes, upload_prefix\n\n"
        "async def f(p, b):\n"
        "    await upload_file_from_bytes('k', b)\n"
        "    await upload_prefix('k', p)\n"
    )
    assert _o007(src) == []


def test_collect_bindings_reports_each_binding_kind() -> None:
    import ast

    tree = ast.parse(
        "from application_sdk.storage.batch import upload_file as up\n"
        "import application_sdk.storage as st\n"
        "from application_sdk.common.incremental import marker\n"
        "import application_sdk.storage.ops\n"
    )
    func_names, module_names, root_bound = collect_upload_bindings(tree)
    assert func_names == frozenset({"up"})
    assert module_names == frozenset({"st", "marker"})
    assert root_bound is True


# ── suppression, metadata, scope ───────────────────────────────────────────────


def test_suppressed_by_a_justified_directive() -> None:
    src = (
        _IMPORT + "async def f(p):\n"
        "    # conformance: ignore[O007] checkpoint is re-read by resume() below\n"
        "    await upload_file('k', p)\n"
    )
    findings = [f for f in scan_text(src, "app/x.py") if f.rule_id == "O007"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_message_states_the_cause_and_both_fixes() -> None:
    src = _IMPORT + "async def f(p):\n    await upload_file('k', p)\n"
    (finding,) = [f for f in scan_text(src, "app/x.py") if f.rule_id == "O007"]
    for needle in (
        "retain_local_copy defaults to True",
        "retain_local_copy=False",
        "TEMPORARY_PATH",
        "delete the file right after the upload",
    ):
        assert needle in finding.message


def test_rule_metadata() -> None:
    rule = get_rule("O007")
    assert rule.name == "UploadRetainsLocalCopy"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is False
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.GUIDE
    assert rule.remediation_reference.target == "programs/areas/optimizations.prose.md"


def _repo(tmp_path: Path, project: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "io.py").write_text(
        _IMPORT + "async def f(p):\n    await upload_file('k', p)\n", encoding="utf-8"
    )
    return tmp_path


def _rule_ids(repo: Path) -> list[str]:
    out = repo / "out.sarif"
    runner.main(["--repo", str(repo), "--rule", "O007", "--output", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return [r["ruleId"] for r in doc["runs"][0]["results"]]


def test_runner_reports_o007_on_an_app_repo(tmp_path: Path) -> None:
    assert _rule_ids(_repo(tmp_path, "atlan-demo-app")) == ["O007"]


def test_runner_drops_o007_on_the_sdk(tmp_path: Path) -> None:
    """APP-scoped: the SDK publishes upload_file, so its own calls are not graded."""
    assert _rule_ids(_repo(tmp_path, "atlan-application-sdk")) == []
