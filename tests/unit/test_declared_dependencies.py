"""The SDK's declared dependencies and its imports agree, in both directions.

* Every third-party module ``application_sdk/`` imports is declared — as a core
  dependency or in an extra — so an import never works only because some other
  package happens to bring it along today.
* Every core dependency has an import site, so nothing ships to every connector
  that the SDK never uses.
* Every extra requirement is imported, is a core dependency (a compatibility
  alias), or names the SDK itself.
* Every ``[tool.uv].constraint-dependencies`` entry constrains a package that is
  actually in the lock.

Imports are read statically (``ast``), including function-level ones, so the
check needs no extra installed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from functools import cache
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
SDK_DIR = ROOT / "application_sdk"
SELF = "atlan-application-sdk"

#: Import name (dotted prefix) -> the distribution that provides it, where the
#: two differ. Longest prefix wins; anything not listed maps to its top-level
#: name with ``_`` -> ``-``, or, under a namespace root, to
#: ``<root>-<second segment>`` (``azure.core`` -> ``azure-core``).
IMPORT_DIST: dict[str, str] = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "pyatlan_v9": "pyatlan",
    # opentelemetry-api's own modules. Only these map to it: other distributions
    # share the namespace (opentelemetry.proto is opentelemetry-proto), so an
    # unlisted submodule must not fall back to the api.
    "opentelemetry": "opentelemetry-api",
    "opentelemetry.trace": "opentelemetry-api",
    "opentelemetry.metrics": "opentelemetry-api",
    "opentelemetry._logs": "opentelemetry-api",
    "opentelemetry.exporter.otlp.proto.grpc": "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry.exporter.prometheus": "opentelemetry-exporter-prometheus",
    "opentelemetry.instrumentation.fastapi": "opentelemetry-instrumentation-fastapi",
    "psycopg2": "psycopg2-binary",
}

#: Namespace packages that several distributions install into.
NAMESPACE_ROOTS = frozenset({"opentelemetry", "google", "azure"})

#: Distributions that are the documented API of a declared one, so importing
#: them rests on that declaration. Not a place for "it arrives transitively":
#: typing_extensions arrived via pydantic and was declared anyway, because it is
#: not part of pydantic's API.
PROVIDED_BY: dict[str, str] = {
    "starlette": "fastapi",  # fastapi re-exports and subclasses starlette
    "pydantic-core": "pydantic",  # pydantic's own core (PydanticCustomError, ...)
    "botocore": "boto3",  # boto3's exceptions and config types are botocore's
    "azure-core": "azure-identity",  # azure-identity credentials are azure-core types
    "msgspec": "pyatlan",  # pyatlan_v9 assets are msgspec Structs
}

#: Imported only by code that runs inside the consuming app's environment,
#: which installs them itself.
CONSUMER_SUPPLIED: dict[str, str] = {
    "pytest": "application_sdk.testing runs under the app's test runner",
    "pytest-asyncio": "application_sdk.testing fixtures, under the app's test runner",
    "hypothesis": "application_sdk.testing.hypothesis strategies, for the app's tests",
    "psycopg": "sql_typecasters attaches to the Postgres driver the app ships",
    "psycopg2-binary": "sql_typecasters attaches to the Postgres driver the app ships",
}

#: Core dependencies with no import site, and why they are declared anyway.
NO_IMPORT_SITE: dict[str, str] = {
    "atlan-application-sdk-api": "ships part of application_sdk/ itself",
    "authlib": "published CVE floor on a pyatlan dependency",
    "protobuf": "published CVE floor on a temporalio / opentelemetry dependency",
    "pyjwt": "published CVE floor on an azure-identity dependency",
    "python-multipart": "fastapi imports it for File/Form parsing; carries a CVE floor",
}

#: Extra requirements with no import site, and why the extra carries them.
EXTRA_NO_IMPORT_SITE: dict[str, str] = {
    "httpx2": "[tests]: starlette's TestClient transport, for the app's own tests",
}


@cache
def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _names(reqs: list[str]) -> set[str]:
    return {canonicalize_name(Requirement(r).name) for r in reqs}


def _core() -> set[str]:
    return _names(_pyproject()["project"]["dependencies"])


def _extras() -> dict[str, set[str]]:
    extras = _pyproject()["project"].get("optional-dependencies", {})
    return {extra: _names(reqs) for extra, reqs in extras.items()}


def _type_checking_only(node: ast.If) -> bool:
    return ast.unparse(node.test).strip() in ("TYPE_CHECKING", "typing.TYPE_CHECKING")


def _module_imports(tree: ast.AST) -> list[str]:
    """Absolute imported module names, outside ``if TYPE_CHECKING:`` blocks."""
    found: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _type_checking_only(node):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _dist_for(module: str) -> str | None:
    """The distribution behind ``module``, or None for stdlib / the SDK itself."""
    top = module.split(".")[0]
    if top in sys.stdlib_module_names or top in ("application_sdk", "__future__"):
        return None
    parts = module.split(".")
    # Under a namespace root, a submodule must match at least two segments; only
    # the bare root ("from opentelemetry import trace") may match the root key.
    shortest = 2 if top in NAMESPACE_ROOTS and len(parts) > 1 else 1
    for i in range(len(parts), shortest - 1, -1):
        prefix = ".".join(parts[:i])
        if prefix in IMPORT_DIST:
            return IMPORT_DIST[prefix]
    if shortest == 2:
        return canonicalize_name(f"{top}-{parts[1]}")
    return canonicalize_name(top)


@cache
def _imported() -> dict[str, list[str]]:
    """``{distribution: [file:line, ...]}`` for every third-party import."""
    sites: dict[str, list[str]] = {}
    for path in sorted(SDK_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _module_imports(tree):
            dist = _dist_for(module)
            if dist is not None:
                sites.setdefault(dist, []).append(str(path.relative_to(ROOT)))
    return sites


def _declared() -> set[str]:
    return _core() | {name for names in _extras().values() for name in names}


def test_every_import_is_declared() -> None:
    declared = _declared()
    undeclared = {
        dist: sorted(set(files))[:3]
        for dist, files in _imported().items()
        if dist not in declared
        and dist not in PROVIDED_BY
        and dist not in CONSUMER_SUPPLIED
    }
    assert not undeclared, (
        "application_sdk/ imports packages pyproject.toml does not declare "
        "(declare them, or map the import name in IMPORT_DIST): "
        f"{undeclared}"
    )


def test_every_core_dependency_is_imported() -> None:
    imported = set(_imported())
    unused = sorted(_core() - imported - set(NO_IMPORT_SITE))
    assert not unused, (
        "core dependencies with no import site in application_sdk/ "
        f"(drop them, or record why in NO_IMPORT_SITE): {unused}"
    )


def test_every_extra_requirement_is_imported_or_an_alias() -> None:
    imported, core = set(_imported()), _core()
    unused = {
        extra: sorted(names - imported - core - {SELF} - set(EXTRA_NO_IMPORT_SITE))
        for extra, names in _extras().items()
        if names - imported - core - {SELF} - set(EXTRA_NO_IMPORT_SITE)
    }
    assert not unused, f"extra requirements nothing imports: {unused}"


def test_the_exception_tables_have_not_gone_stale() -> None:
    """Each entry still describes something true, so the tables cannot rot."""
    imported, core, declared = set(_imported()), _core(), _declared()
    assert sorted(set(NO_IMPORT_SITE) - core) == [], "not core dependencies"
    assert sorted(set(NO_IMPORT_SITE) & imported) == [], "these have an import site"
    extra_reqs = {name for names in _extras().values() for name in names}
    assert sorted(set(EXTRA_NO_IMPORT_SITE) - extra_reqs) == [], (
        "not extra requirements"
    )
    assert sorted(set(EXTRA_NO_IMPORT_SITE) & imported) == [], (
        "these have an import site"
    )
    assert sorted(set(PROVIDED_BY) - imported) == [], "PROVIDED_BY: not imported"
    assert sorted(set(PROVIDED_BY) & declared) == [], "PROVIDED_BY: now declared"
    assert sorted(set(PROVIDED_BY.values()) - declared) == []
    assert sorted(set(CONSUMER_SUPPLIED) - imported) == [], "not imported"
    assert sorted(set(IMPORT_DIST.values()) - imported) == [], "IMPORT_DIST unused"


def test_every_uv_constraint_names_a_locked_package() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {canonicalize_name(p["name"]) for p in lock["package"]}
    constraints = _pyproject()["tool"]["uv"].get("constraint-dependencies", [])
    dead = sorted(_names(constraints) - locked)
    assert not dead, f"[tool.uv].constraint-dependencies not in uv.lock: {dead}"


def test_module_imports_skip_type_checking_and_relative_imports() -> None:
    src = (
        "import yaml\n"
        "from . import sibling\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import pandas\nelse:\n    import orjson\n"
        "def f():\n    from opentelemetry.sdk.trace import TracerProvider\n"
    )
    modules = _module_imports(ast.parse(src))
    assert modules == ["yaml", "typing", "orjson", "opentelemetry.sdk.trace"]
    assert [_dist_for(m) for m in modules] == [
        "pyyaml",
        None,
        "orjson",
        "opentelemetry-sdk",
    ]


def test_namespace_submodules_map_to_their_own_distribution() -> None:
    """An unlisted opentelemetry submodule is not credited to opentelemetry-api."""
    assert _dist_for("opentelemetry") == "opentelemetry-api"
    assert _dist_for("opentelemetry.trace.span") == "opentelemetry-api"
    assert _dist_for("opentelemetry.sdk.trace") == "opentelemetry-sdk"
    proto = _dist_for("opentelemetry.proto.collector.metrics.v1")
    assert proto == "opentelemetry-proto"
    assert proto not in _declared()
    assert _dist_for("azure.core.exceptions") == "azure-core"
    assert _dist_for("google.auth.transport") == "google-auth"
