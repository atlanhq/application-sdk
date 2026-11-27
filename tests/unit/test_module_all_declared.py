"""Every public SDK module says what it offers, in ``__all__``.

Without ``__all__``, the symbol-removal gate (``.github/scripts/check_symbol_removals.py``)
counts every intra-package import of a module as public surface, so splitting
a module or tidying an import reads as a removal. With it, the surface is what
the module lists (``docs/standards/symbols.md``).

Names a module only imported, and that connector apps did import, either stay
declared or became deprecated aliases that point at their home (FND-4259). The
second half of this file pins both lists.
"""

from __future__ import annotations

import ast
import importlib
import warnings
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "application_sdk"


def _public_modules() -> list[str]:
    modules = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        parts = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if any(p.startswith("_") for p in parts):
            continue  # a private module or package promises nothing
        if parts[1:2] == ("testing",):
            continue  # test tooling, out of scope for FND-4259
        modules.append(path.relative_to(_PACKAGE_ROOT.parent).as_posix())
    return modules


def _declares_all(source: str) -> bool:
    for node in ast.parse(source).body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            return True
    return False


def test_the_scan_sees_the_package() -> None:
    modules = _public_modules()
    assert "application_sdk/handler/service.py" in modules
    assert "application_sdk/__init__.py" in modules
    assert not any("/_" in m.removesuffix("/__init__.py") for m in modules)


def test_every_public_module_declares_all() -> None:
    missing = [
        m
        for m in _public_modules()
        if not _declares_all((_PACKAGE_ROOT.parent / m).read_text(encoding="utf-8"))
    ]
    assert missing == [], (
        "these public modules have no __all__, so the symbol-removal gate "
        "counts every application_sdk import in them as surface; list what "
        f"each defines on purpose: {missing}"
    )


#: (module, name, home): imported names connector apps imported from a module
#: that only imported them. Each is now a deprecated alias of its home.
_ALIASES = [
    ("application_sdk.app.context", "get_correlation_context", "application_sdk.observability.correlation"),
    ("application_sdk.clients.sql", "EngineNotInitializedError", "application_sdk.clients.sql_errors"),
    ("application_sdk.clients.sql", "SqlBindParameterError", "application_sdk.clients.sql_errors"),
    ("application_sdk.clients.sql", "SqlPandasResultError", "application_sdk.clients.sql_errors"),
    ("application_sdk.clients.sql", "UnsupportedSqlCursorError", "application_sdk.clients.sql_errors"),
    ("application_sdk.contracts.types", "CredentialRef", "application_sdk.credentials.ref"),
    ("application_sdk.handler.contracts", "AgentCredentialSpec", "application_sdk.credentials.spec"),
    ("application_sdk.observability.metrics_adaptor", "MetricType", "application_sdk.observability.models"),
    ("application_sdk.templates.sql_app", "ExtractionOutput", "application_sdk.templates.contracts.sql_metadata"),
    ("application_sdk.templates.sql_app", "TEMPORARY_PATH", "application_sdk.constants"),
]  # fmt: skip


@pytest.mark.parametrize(("module", "name", "home"), _ALIASES)
def test_an_imported_name_apps_used_is_a_deprecated_alias_of_its_home(
    module: str, name: str, home: str
) -> None:
    mod = importlib.import_module(module)
    with pytest.warns(DeprecationWarning, match=r"v4\.0\.0") as record:
        value = getattr(mod, name)
    assert value is getattr(importlib.import_module(home), name)
    assert any(f"{home}.{name}" in str(w.message) for w in record)


@pytest.mark.parametrize(("module", "name", "home"), _ALIASES)
def test_a_star_import_still_gets_the_deprecated_alias(
    module: str, name: str, home: str
) -> None:
    """Before ``__all__``, ``from module import *`` bound every public name,
    this one included. Listing the alias keeps it there, through the
    deprecation warning, until its removal in v4.0.0."""
    namespace: dict[str, object] = {}
    with pytest.warns(DeprecationWarning, match=name):
        exec(f"from {module} import *", namespace)
    assert namespace[name] is getattr(importlib.import_module(home), name)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("application_sdk.app.base_errors", "ObjectStoreNotConfiguredError"),
        ("application_sdk.app.base_errors", "SecretStoreNotConfiguredError"),
        ("application_sdk.app.entrypoint", "canonical_workflow_type"),
        ("application_sdk.contracts.storage", "FileReference"),
        ("application_sdk.contracts.types", "MaxItems"),
        ("application_sdk.credentials.ref", "AgentCredentialSpec"),
        ("application_sdk.credentials.utils", "parse_credentials_extra"),
        ("application_sdk.errors.base", "Audience"),
        ("application_sdk.errors.base", "FailureCategory"),
        ("application_sdk.errors.wire", "Audience"),
        ("application_sdk.infrastructure.secrets", "SecretStoreNotConfiguredError"),
        ("application_sdk.storage.batch", "upload_file"),
        ("application_sdk.storage.formats.format_errors", "ObjectStoreDownloadError"),
        ("application_sdk.storage.formats.format_errors", "ObjectStoreReadError"),
        ("application_sdk.storage.ops", "download_file_chunked"),
    ],
)
def test_a_reexport_apps_use_stays_declared(module: str, name: str) -> None:
    """Imported names connector apps import from here that stay declared, with no
    warning. Either the module keeps the re-export on purpose (marked in the
    source, served by its package, or pinned by a test), or a parent package
    exports the same name: B001 matches ``from <package> import X`` against a
    deprecated ``X`` in any submodule of it, so an alias here would flag the
    canonical package import in every app."""
    mod = importlib.import_module(module)
    assert name in mod.__all__
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        getattr(mod, name)
