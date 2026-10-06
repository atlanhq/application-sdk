"""Tests for K006 ManifestContractFieldMismatch.

Covers the manifest-vs-contract cross-check: every ``$.extract.outputs.<field>``
reference in a committed ``app/generated/**/manifest.json`` DAG node's
``inputs.args`` must be a field the entrypoint's Python ``Output`` contract
declares — directly, or via an inherited base/mixin (e.g.
``PublishInputMixin``, the fix that resolved the motivating incident).

Test helpers
------------
``_write_py``: writes ``{relative_path: source_text}`` under ``tmp_path``.
``_write_manifest``: writes a ``manifest.json`` with a given ``dag`` dict at a
given path — richer than the ``{"dag": {}}`` placeholder P016's tests use,
since K006 needs real ``inputs.args`` JSONPath strings.
``_run``: writes both, calls :func:`scan_all`, returns its findings.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from conformance.suite.checks._entrypoint_contract_classes import (
    CodeContractScan,
    scan_file_for_entrypoint_contracts,
)
from conformance.suite.checks._entrypoint_contract_fields import (
    collect_entrypoint_contract_names,
)
from conformance.suite.checks.manifest_contract import scan_all
from conformance.suite.checks.preflight._common import (
    build_registry,
    collect_entrypoint_input_contract_names,
)
from conformance.suite.checks.prescriptions import scan_all as prescriptions_scan_all
from conformance.suite.checks.prescriptions._decorator_provenance import (
    collect_import_provenance,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def _write_manifest(path: Path, dag: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dag": dag}), encoding="utf-8")


def _extract_node(app_name: str = "myapp") -> dict:
    """A minimal root 'extract' node (no depends_on, no downstream refs)."""
    return {
        "activity_name": "execute_workflow",
        "app_name": app_name,
        "inputs": {
            "workflow_type": "MyWorkflow",
            "app_name": app_name,
            "task_queue": "q",
            "args": {},
        },
    }


def _publish_node(fields: list[str]) -> dict:
    """A downstream node referencing ``$.extract.outputs.<field>`` for each *fields*."""
    return {
        "activity_name": "execute_workflow",
        "app_name": "publish",
        "inputs": {
            "workflow_type": "PublishWorkflow",
            "app_name": "publish",
            "task_queue": "q",
            "args": {f: f"$.extract.outputs.{f}" for f in fields},
        },
        "depends_on": {"node_id": "extract"},
    }


def _k006(findings: list) -> list:
    return [f for f in findings if f.rule_id == "K006"]


def _ids(findings: list) -> list[str]:
    """Rule IDs of unsuppressed findings (matching runner gate semantics)."""
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k006_rule_metadata() -> None:
    rule = get_rule("K006")
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


# ---------------------------------------------------------------------------
# The motivating incident: a mixin-derived field silently deleted
# ---------------------------------------------------------------------------

_EXTRACT_MISSING_PUBLISH_FIELDS = """\
from application_sdk.app import App, entrypoint

class ExtractInput:
    url: str

class ExtractOutput:
    connection_qualified_name: str
    # publish_state_prefix and current_state_prefix were removed here —
    # the exact shape of the incident this rule exists to catch.

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        pass
"""

_PUBLISH_FIELDS = [
    "connection_qualified_name",
    "transformed_data_prefix",
    "publish_state_prefix",
    "current_state_prefix",
]


def test_k006_fires_when_referenced_field_missing_from_output(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_MISSING_PUBLISH_FIELDS})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "publish": _publish_node(_PUBLISH_FIELDS)},
    )
    findings = scan_all(paths, tmp_path)
    k006 = _k006(findings)
    missing = {f.message for f in k006}
    # publish_state_prefix, current_state_prefix, and transformed_data_prefix are
    # all referenced by _publish_node(_PUBLISH_FIELDS) but not declared on
    # ExtractOutput — all three must be flagged.
    assert any("publish_state_prefix" in m for m in missing)
    assert any("current_state_prefix" in m for m in missing)
    assert any("transformed_data_prefix" in m for m in missing)
    assert not any(
        "connection_qualified_name" in m for m in missing
    ), "connection_qualified_name is declared — must not be flagged"


def test_k006_finding_anchored_on_output_class(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_MISSING_PUBLISH_FIELDS})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(),
            "publish": _publish_node(["publish_state_prefix"]),
        },
    )
    findings = scan_all(paths, tmp_path)
    k006 = _k006(findings)
    assert k006
    assert k006[0].file == "app.py"


# ---------------------------------------------------------------------------
# Fixed via PublishInputMixin (inherited fields) — the actual downstream fix
# ---------------------------------------------------------------------------

_EXTRACT_WITH_MIXIN = """\
from application_sdk.app import App, entrypoint
from application_sdk.contracts.base import PublishInputMixin

class ExtractInput:
    url: str

class ExtractOutput(PublishInputMixin):
    pass

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        pass
"""


def test_k006_silent_when_field_provided_via_sdk_mixin(tmp_path: Path) -> None:
    """Guards the actual fix: PublishInputMixin resolves all four publish fields.

    This is the case that requires the shared, inheritance-aware
    ``resolve_contract_fields`` (BLDX-1528) — a same-body-only field scan would
    false-positive here.
    """
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_WITH_MIXIN})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "publish": _publish_node(_PUBLISH_FIELDS)},
    )
    findings = scan_all(paths, tmp_path)
    assert "K006" not in _ids(findings)


# ---------------------------------------------------------------------------
# Fields declared directly on the Output's own body
# ---------------------------------------------------------------------------

_EXTRACT_OWN_BODY_FIELDS = """\
from application_sdk.app import App, entrypoint

class ExtractInput:
    url: str

class ExtractOutput:
    connection_qualified_name: str
    transformed_data_prefix: str
    publish_state_prefix: str
    current_state_prefix: str

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        pass
"""


def test_k006_silent_when_field_declared_on_own_body(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_OWN_BODY_FIELDS})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "publish": _publish_node(_PUBLISH_FIELDS)},
    )
    findings = scan_all(paths, tmp_path)
    assert "K006" not in _ids(findings)


# ---------------------------------------------------------------------------
# References to a non-"extract" node are out of scope
# ---------------------------------------------------------------------------


def test_k006_ignores_refs_to_a_different_node(tmp_path: Path) -> None:
    """A '$.qi.outputs.*' ref (not '$.extract.outputs.*') is never K006's concern,
    even when ExtractOutput plainly lacks a matching field."""
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_MISSING_PUBLISH_FIELDS})
    other_node = {
        "activity_name": "execute_workflow",
        "app_name": "query-intelligence",
        "inputs": {
            "workflow_type": "QueryIntelligenceWorkflow",
            "app_name": "query-intelligence",
            "task_queue": "q",
            "args": {"input_prefix": "$.qi.outputs.something_unrelated"},
        },
        "depends_on": {"node_id": "extract"},
    }
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "qi": other_node},
    )
    findings = scan_all(paths, tmp_path)
    assert "K006" not in _ids(findings)


def test_k006_ignores_workflow_and_failure_refs(tmp_path: Path) -> None:
    """'$.workflow.*' / '$.failure.*' (AE run/failure context) never match 'outputs'."""
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_MISSING_PUBLISH_FIELDS})
    notifications_node = {
        "activity_name": "execute_workflow",
        "app_name": "notification-app",
        "inputs": {
            "workflow_type": "NotificationWorkflow",
            "app_name": "notification-app",
            "task_queue": "q",
            "args": {
                "metadata": {
                    "workflow_name": "$.workflow.name",
                    "error_message": "$.failure.message",
                }
            },
        },
        "depends_on": {"tag": "workflow_complete"},
    }
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "notifications": notifications_node},
    )
    findings = scan_all(paths, tmp_path)
    assert "K006" not in _ids(findings)


# ---------------------------------------------------------------------------
# Multi-entrypoint (bundle): subdir <-> wire-name mapping
# ---------------------------------------------------------------------------

_BUNDLE_APP = """\
from application_sdk.app import App, entrypoint

class CrawlInput:
    url: str

class CrawlOutput:
    connection_qualified_name: str
    # publish_state_prefix removed — should fire only for the crawler manifest.

class MineInput:
    path: str

class MineOutput:
    connection_qualified_name: str
    publish_state_prefix: str

class MyApp(App):
    @entrypoint(name="crawler")
    async def crawl(self, input: CrawlInput) -> CrawlOutput:
        pass

    @entrypoint(name="miner")
    async def mine(self, input: MineInput) -> MineOutput:
        pass
"""


def test_k006_multi_entrypoint_maps_subdir_to_wire_name(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _BUNDLE_APP})
    _write_manifest(
        tmp_path / "app" / "generated" / "crawler" / "manifest.json",
        {
            "extract": _extract_node("crawler"),
            "publish": _publish_node(
                ["connection_qualified_name", "publish_state_prefix"]
            ),
        },
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "miner" / "manifest.json",
        {
            "extract": _extract_node("miner"),
            "publish": _publish_node(
                ["connection_qualified_name", "publish_state_prefix"]
            ),
        },
    )
    findings = scan_all(paths, tmp_path)
    k006 = _k006(findings)
    assert len(k006) == 1
    assert "publish_state_prefix" in k006[0].message
    assert "CrawlOutput" in k006[0].message


_BUNDLE_APP_ATTRIBUTE_DECORATOR = """\
import application_sdk.app as sdk

class CrawlInput:
    url: str

class CrawlOutput:
    connection_qualified_name: str
    # publish_state_prefix removed — should fire only for the crawler manifest.

class MineInput:
    path: str

class MineOutput:
    connection_qualified_name: str
    publish_state_prefix: str

class MyApp(sdk.App):
    @sdk.entrypoint(name="crawler")
    async def crawl(self, input: CrawlInput) -> CrawlOutput:
        pass

    @sdk.entrypoint(name="miner")
    async def mine(self, input: MineInput) -> MineOutput:
        pass
"""


def test_k006_multi_entrypoint_with_attribute_style_decorator(tmp_path: Path) -> None:
    """Attribute-form decorators (``@sdk.entrypoint``, via a module alias) must
    resolve a wire name just like the bare ``@entrypoint`` form.

    ``is_entrypoint_decorator`` (used for entrypoint detection) recognises
    attribute-form decorators, so wire-name extraction must too — otherwise
    the entrypoint resolves with ``wire_name=None``, never matches its manifest
    subdir, and K006 silently skips it (a false negative)."""
    paths = _write_py(tmp_path, {"app.py": _BUNDLE_APP_ATTRIBUTE_DECORATOR})
    _write_manifest(
        tmp_path / "app" / "generated" / "crawler" / "manifest.json",
        {
            "extract": _extract_node("crawler"),
            "publish": _publish_node(
                ["connection_qualified_name", "publish_state_prefix"]
            ),
        },
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "miner" / "manifest.json",
        {
            "extract": _extract_node("miner"),
            "publish": _publish_node(
                ["connection_qualified_name", "publish_state_prefix"]
            ),
        },
    )
    findings = scan_all(paths, tmp_path)
    k006 = _k006(findings)
    assert len(k006) == 1
    assert "publish_state_prefix" in k006[0].message
    assert "CrawlOutput" in k006[0].message


# ---------------------------------------------------------------------------
# Ambiguous / no-op scenarios
# ---------------------------------------------------------------------------


def test_k006_noop_when_app_generated_absent(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_MISSING_PUBLISH_FIELDS})
    assert scan_all(paths, tmp_path) == []


def test_k006_noop_when_no_entrypoints_in_code(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": "x = 1\n"})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "publish": _publish_node(_PUBLISH_FIELDS)},
    )
    assert scan_all(paths, tmp_path) == []


_TWO_ENTRYPOINTS_SINGLE_MANIFEST = """\
from application_sdk.app import App, entrypoint

class AInput:
    url: str

class AOutput:
    pass

class BInput:
    url: str

class BOutput:
    pass

class MyApp(App):
    @entrypoint
    async def a(self, input: AInput) -> AOutput:
        pass

    @entrypoint
    async def b(self, input: BInput) -> BOutput:
        pass
"""


def test_k006_skips_single_manifest_when_code_has_multiple_entrypoints(
    tmp_path: Path,
) -> None:
    """Ambiguous mapping (single-EP manifest, >1 entrypoint in code) is skipped
    rather than guessed — conservative, matches P016's own-mode semantics."""
    paths = _write_py(tmp_path, {"app.py": _TWO_ENTRYPOINTS_SINGLE_MANIFEST})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "publish": _publish_node(["missing_field"])},
    )
    assert scan_all(paths, tmp_path) == []


_EXTRACT_OUTPUT_NOT_IN_REPO = """\
from application_sdk.app import App, entrypoint
from some_external_package import ExternalOutput

class ExtractInput:
    url: str

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExternalOutput:
        pass
"""


def test_k006_skips_when_output_class_not_resolvable_in_repo(tmp_path: Path) -> None:
    """An Output class not defined anywhere in the scanned source (e.g. imported
    from a third-party package) cannot be resolved — skip conservatively rather
    than false-positive on every field."""
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_OUTPUT_NOT_IN_REPO})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(),
            "publish": _publish_node(["connection_qualified_name"]),
        },
    )
    assert scan_all(paths, tmp_path) == []


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

_EXTRACT_MISSING_FIELD_SUPPRESSED = """\
from application_sdk.app import App, entrypoint

class ExtractInput:
    url: str

# conformance: ignore[K006] intentional: publish disabled in a follow-up PR
class ExtractOutput:
    connection_qualified_name: str

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        pass
"""


def test_k006_suppression_directive_on_output_class(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _EXTRACT_MISSING_FIELD_SUPPRESSED})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(),
            "publish": _publish_node(["publish_state_prefix"]),
        },
    )
    findings = scan_all(paths, tmp_path)
    k006 = _k006(findings)
    assert k006
    assert all(f.suppressed for f in k006)
    assert "K006" not in _ids(findings)


# ---------------------------------------------------------------------------
# Implicit run() on an SDK App-template base
# ---------------------------------------------------------------------------

# An undecorated ``async def run`` is the entrypoint of any class whose base is
# an SDK App-family class imported from ``application_sdk`` — ``App`` itself or
# one of the templates (``SqlApp``, ``BaseMetadataExtractor``, ...). The
# templates live outside the scanned repo, so the in-repo class registry alone
# can never prove that ancestry; import provenance has to.

_TEMPLATE_RUN_APP = """\
{imports}
from application_sdk.contracts.base import Input, Output

class MyInput(Input):
    foo: str = ""

class MyOutput(Output):
    bar: str = ""

{prelude}class MyApp({base}):
{decorator}    {kw}def run(self, input: MyInput) -> MyOutput:
        pass
"""


def _template_run_app(
    base: str,
    *,
    imports: str | None = None,
    prelude: str = "",
    decorator: str = "",
    kw: str = "async ",
) -> str:
    return _TEMPLATE_RUN_APP.format(
        imports=(
            imports
            if imports is not None
            else f"from application_sdk.templates import {base}"
        ),
        base=base,
        prelude=prelude,
        decorator=decorator,
        kw=kw,
    )


_TEMPLATE_BASES = [
    "BaseMetadataExtractor",
    "SqlApp",
    "SqlMetadataExtractor",
    "IncrementalSqlMetadataExtractor",
    "SqlQueryExtractor",
]

_RECOGNISED = {f"template-{b}": _template_run_app(b) for b in _TEMPLATE_BASES} | {
    "aliased-template": _template_run_app(
        "Base",
        imports="from application_sdk.templates import SqlApp as Base",
    ),
    "template-submodule": _template_run_app(
        "BaseMetadataExtractor",
        imports=(
            "from application_sdk.templates.base_metadata_extractor "
            "import BaseMetadataExtractor"
        ),
    ),
    "star-import": _template_run_app(
        "SqlApp", imports="from application_sdk.templates import *"
    ),
}

_NOT_RECOGNISED = {
    "local-class-named-like-template": _template_run_app(
        "BaseMetadataExtractor",
        imports="",
        prelude="class BaseMetadataExtractor:\n    pass\n\n",
    ),
    "sdk-import-shadowed-by-local-class": _template_run_app(
        "SqlApp",
        prelude="class SqlApp:\n    pass\n\n",
    ),
    "undefined-name-like-template": _template_run_app("SqlApp", imports=""),
    "non-sdk-module": _template_run_app(
        "SqlApp", imports="from other_sdk.templates import SqlApp"
    ),
    "relative-import": _template_run_app(
        "SqlApp", imports="from .templates import SqlApp"
    ),
    "star-import-beside-non-sdk-star": _template_run_app(
        "SqlApp",
        imports="from application_sdk.templates import *\nfrom other_sdk import *",
    ),
    "same-name-attribute-of-other-module": _template_run_app(
        "other_sdk.SqlApp",
        imports="import other_sdk\nfrom application_sdk.templates import SqlApp",
    ),
    "sync-run": _template_run_app("SqlApp", kw=""),
    "task-decorated-run": _template_run_app(
        "SqlApp",
        imports=(
            "from application_sdk.templates import SqlApp\n"
            "from application_sdk.app import task"
        ),
        decorator="    @task\n",
    ),
}


_SQLAPP_BASE_MODULE = """\
from application_sdk.templates import SqlApp

class MyBase(SqlApp):
    pass
"""

_RUN_ON_MYBASE = _template_run_app("MyBase", imports="from base import MyBase")

_RECOGNISED_IN_REPO_BASES: dict[str, dict[str, str]] = {
    "in-repo-base-same-file": {
        "app.py": _template_run_app(
            "MyBase",
            imports="from application_sdk.templates import SqlApp",
            prelude="class MyBase(SqlApp):\n    pass\n\n",
        ),
    },
    "in-repo-base-other-file": {
        "base.py": _SQLAPP_BASE_MODULE,
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-base-two-hops-across-files": {
        "base.py": _SQLAPP_BASE_MODULE,
        "mid.py": "from base import MyBase\n\nclass Mid(MyBase):\n    pass\n",
        "app.py": _template_run_app("Mid", imports="from mid import Mid"),
    },
    "in-repo-base-aliased-sdk-import-other-file": {
        "base.py": (
            "from application_sdk.templates import SqlApp as _Sql\n\n"
            "class MyBase(_Sql):\n    pass\n"
        ),
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-template-named-subclass-of-sdk-import-other-file": {
        "base.py": (
            "from application_sdk.templates import SqlApp as _SqlApp\n\n"
            "class SqlApp(_SqlApp):\n    pass\n"
        ),
        "app.py": _template_run_app("SqlApp", imports="from base import SqlApp"),
    },
    "in-repo-app-base-other-file": {
        "base.py": (
            "from application_sdk.app import App\n\nclass MyBase(App):\n    pass\n"
        ),
        "app.py": _RUN_ON_MYBASE,
    },
}

_NOT_RECOGNISED_IN_REPO_BASES: dict[str, dict[str, str]] = {
    "in-repo-base-on-local-template-name-same-file": {
        "app.py": _template_run_app(
            "MyBase",
            imports="",
            prelude=("class SqlApp:\n    pass\n\nclass MyBase(SqlApp):\n    pass\n\n"),
        ),
    },
    "in-repo-base-on-local-template-name-other-file": {
        "base.py": "class SqlApp:\n    pass\n\nclass MyBase(SqlApp):\n    pass\n",
        "app.py": _RUN_ON_MYBASE,
    },
    "local-template-name-imported-from-other-file": {
        "base.py": "class SqlApp:\n    pass\n",
        "app.py": _template_run_app("SqlApp", imports="from base import SqlApp"),
    },
    "in-repo-base-on-shadowed-sdk-import-other-file": {
        "base.py": (
            "from application_sdk.templates import SqlApp\n\n"
            "class SqlApp:\n    pass\n\n"
            "class MyBase(SqlApp):\n    pass\n"
        ),
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-base-on-never-imported-name-other-file": {
        "base.py": "class MyBase(SqlApp):\n    pass\n",
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-base-on-non-sdk-module-other-file": {
        "base.py": (
            "from other_sdk.templates import SqlApp\n\n"
            "class MyBase(SqlApp):\n    pass\n"
        ),
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-base-on-relative-import-other-file": {
        "base.py": (
            "from .templates import SqlApp\n\nclass MyBase(SqlApp):\n    pass\n"
        ),
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-base-on-other-module-attribute-other-file": {
        "base.py": (
            "import other_sdk\nfrom application_sdk.templates import SqlApp\n\n"
            "class MyBase(other_sdk.SqlApp):\n    pass\n"
        ),
        "app.py": _RUN_ON_MYBASE,
    },
    "in-repo-base-sync-run-other-file": {
        "base.py": _SQLAPP_BASE_MODULE,
        "app.py": _template_run_app("MyBase", imports="from base import MyBase", kw=""),
    },
    "in-repo-base-task-decorated-run-other-file": {
        "base.py": _SQLAPP_BASE_MODULE,
        "app.py": _template_run_app(
            "MyBase",
            imports="from base import MyBase\nfrom application_sdk.app import task",
            decorator="    @task\n",
        ),
    },
}

_P013_INPUT = re.compile(r"input annotation '(\w+)'")


def _p013_entrypoint_inputs(root: Path, files: dict[str, str]) -> set[str]:
    """Entrypoint Inputs P013 sees; it reports only violations, so MyInput is untyped."""
    paths = _write_py(
        root,
        {
            name: src.replace("class MyInput(Input):", "class MyInput:")
            for name, src in files.items()
        },
    )
    return {
        m.group(1)
        for f in prescriptions_scan_all(paths, root)
        if f.rule_id == "P013" and (m := _P013_INPUT.search(f.message))
    }


def _entrypoint_inputs_by_scan(
    tmp_path: Path, src: str | dict[str, str]
) -> dict[str, set[str]]:
    """Resolve the entrypoint Input names of *src* through every shared scan."""
    files = {"app.py": src} if isinstance(src, str) else src
    scan_root = tmp_path / "scan"
    paths = _write_py(scan_root, files)
    trees: dict[Path, ast.Module] = {p: ast.parse(p.read_text()) for p in paths}
    aliases = {p: collect_import_aliases(t) for p, t in trees.items()}
    by_name: dict[str, ClassRecord] = {}
    for p, tree in trees.items():
        for rec in collect_classes(tree, p.name, aliases[p]):
            by_name.setdefault(rec.name, rec)

    k_scan = CodeContractScan()
    app_cache: dict[str, bool | None] = {}
    for p, tree in trees.items():
        scan_file_for_entrypoint_contracts(
            tree,
            p.name,
            aliases[p],
            collect_import_provenance(tree),
            by_name,
            app_cache,
            k_scan,
        )
    return {
        "k_series": {
            ep.input_class_name for ep in k_scan.entrypoints if ep.input_class_name
        },
        "contract_names": set(collect_entrypoint_contract_names(trees, by_name))
        - {"MyOutput"},
        "preflight_inputs": set(
            collect_entrypoint_input_contract_names(build_registry(paths, scan_root))
        ),
        "p013": _p013_entrypoint_inputs(tmp_path / "p013", files),
    }


_ALL_SCANS_SEE_MY_INPUT = {
    "k_series": {"MyInput"},
    "contract_names": {"MyInput"},
    "preflight_inputs": {"MyInput"},
    "p013": {"MyInput"},
}

_NO_SCAN_SEES_AN_ENTRYPOINT: dict[str, set[str]] = {
    "k_series": set(),
    "contract_names": set(),
    "preflight_inputs": set(),
    "p013": set(),
}


@pytest.mark.parametrize("src", _RECOGNISED.values(), ids=_RECOGNISED.keys())
def test_implicit_run_on_sdk_template_base_is_an_entrypoint(
    tmp_path: Path, src: str
) -> None:
    assert _entrypoint_inputs_by_scan(tmp_path, src) == _ALL_SCANS_SEE_MY_INPUT


@pytest.mark.parametrize("src", _NOT_RECOGNISED.values(), ids=_NOT_RECOGNISED.keys())
def test_implicit_run_near_misses_are_not_entrypoints(tmp_path: Path, src: str) -> None:
    assert _entrypoint_inputs_by_scan(tmp_path, src) == _NO_SCAN_SEES_AN_ENTRYPOINT


@pytest.mark.parametrize(
    "files",
    _RECOGNISED_IN_REPO_BASES.values(),
    ids=_RECOGNISED_IN_REPO_BASES.keys(),
)
def test_implicit_run_on_in_repo_base_of_sdk_template_is_an_entrypoint(
    tmp_path: Path, files: dict[str, str]
) -> None:
    assert _entrypoint_inputs_by_scan(tmp_path, files) == _ALL_SCANS_SEE_MY_INPUT


@pytest.mark.parametrize(
    "files",
    _NOT_RECOGNISED_IN_REPO_BASES.values(),
    ids=_NOT_RECOGNISED_IN_REPO_BASES.keys(),
)
def test_implicit_run_on_in_repo_base_near_misses_are_not_entrypoints(
    tmp_path: Path, files: dict[str, str]
) -> None:
    assert _entrypoint_inputs_by_scan(tmp_path, files) == _NO_SCAN_SEES_AN_ENTRYPOINT


def test_k006_resolves_output_of_run_on_sdk_template_base(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _template_run_app("BaseMetadataExtractor")})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(), "publish": _publish_node(["bar", "missing"])},
    )
    k006 = _k006(scan_all(paths, tmp_path))
    assert len(k006) == 1
    assert "$.extract.outputs.missing" in k006[0].message
    assert "'MyOutput'" in k006[0].message
