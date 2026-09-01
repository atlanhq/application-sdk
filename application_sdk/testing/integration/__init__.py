"""Integration testing for Apps-SDK.

**Connectors test their App one way: the fixture kit**
(:mod:`~application_sdk.testing.integration.fixtures`). It runs the App in
process — embedded Temporal dev server, mocked infrastructure, a real worker,
and an executor that submits through the real data converter. A connector
star-imports it into ``tests/integration/conftest.py`` and overrides the App
class and its source. See ``docs/guides/integration-fixtures.md``.

**Everything named in this module's ``__all__`` is the older**
:class:`BaseIntegrationTest` **HTTP scenario framework, kept for the narrow case
of locking the literal request/response contract of the app server's endpoints.
For new connectors, do not start there** — see the "recommended pattern" and
"Legacy: HTTP scenario tests" sections of
``docs/guides/integration-testing.md``, which this docstring follows.

The distinction is not two ways of running the App — in both cases the App runs
identically, as a worker executing workflows against Temporal. What differs is
only the submission path: the kit submits through ``AppExecutor``, the scenario
framework through an HTTP POST to a route that then submits to Temporal. So a
scenario test is not a different tier of App coverage; it is the same coverage
reached through the server, plus an assertion on the HTTP envelope. Those
routes are SDK-owned (:mod:`application_sdk.handler.service`), so a connector
re-testing them is largely re-testing SDK code. App behaviour belongs in the
kit; a genuinely *deployed* surface belongs in the ``tests/e2e`` tier.

The kit's fixtures are imported from ``.fixtures`` directly rather than
re-exported here, because pytest has to see them as module-level names in the
adopting conftest.

This module provides a declarative, data-driven approach to integration testing.
Developers define test scenarios as data, and the framework handles everything:
credential loading, server discovery, test execution, and assertion validation.

Quick Start (zero boilerplate):

    1. Set environment variables in .env:
        ATLAN_APPLICATION_NAME=postgres
        E2E_POSTGRES_USERNAME=user
        E2E_POSTGRES_PASSWORD=pass
        E2E_POSTGRES_HOST=localhost
        E2E_POSTGRES_PORT=5432

    2. Define scenarios and a test class:

        >>> from application_sdk.testing.integration import (
        ...     Scenario, BaseIntegrationTest, equals, exists, is_true, is_dict
        ... )
        >>>
        >>> class TestMyConnector(BaseIntegrationTest):
        ...     scenarios = [
        ...         Scenario(
        ...             name="auth_works",
        ...             api="auth",
        ...             assert_that={"success": equals(True)},
        ...         ),
        ...         Scenario(
        ...             name="auth_fails",
        ...             api="auth",
        ...             credentials={"username": "bad", "password": "wrong"},
        ...             assert_that={"success": equals(False)},
        ...         ),
        ...         Scenario(
        ...             name="preflight_works",
        ...             api="preflight",
        ...             metadata={"include-filter": '{"^mydb$": ["^public$"]}'},
        ...             assert_that={"success": equals(True), "data": is_dict()},
        ...         ),
        ...     ]

    3. Run: pytest tests/integration/ -v

    That's it! Credentials are auto-loaded from E2E_* env vars.
    Server URL is auto-discovered from ATLAN_APP_HTTP_HOST/PORT.
    Each scenario becomes its own pytest test.

Supported APIs:
- auth: Test authentication (/workflows/v1/auth)
- metadata: Fetch metadata (/workflows/v1/metadata)
- preflight: Preflight checks (/workflows/v1/check)
- workflow: Start workflow (/workflows/v1/{endpoint})
- config: Get/update workflow config (/workflows/v1/config/{id})

For detailed documentation, see:
    docs/guides/integration-testing.md
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The eager form, for static readers only. griffe builds
    # docs/agents/sdk-capabilities.md from this file's AST and pyright resolves
    # consumer imports the same way; neither follows ``__getattr__``, so without
    # this block every name below would vanish from the manifest and type as
    # ``object`` at each call site. Costs nothing at runtime.
    # Asset-write validation primitive (source of truth: application_sdk.validation),
    # re-exported for convenience when authoring integration tests.
    from application_sdk.validation import (
        AssetValidationFailure,
        AssetValidationReport,
        ReferentialFailure,
        validate_asset,
        validate_transformed_dir,
    )

    from .assertions import (  # Basic assertions; Collection assertions; Numeric assertions; String assertions; Type assertions; Combinators; Custom
        all_of,
        any_of,
        between,
        contains,
        custom,
        ends_with,
        equals,
        exists,
        greater_than,
        greater_than_or_equal,
        has_length,
        is_dict,
        is_empty,
        is_false,
        is_list,
        is_none,
        is_not_empty,
        is_string,
        is_true,
        is_type,
        less_than,
        less_than_or_equal,
        matches,
        none_of,
        not_contains,
        not_equals,
        not_one_of,
        one_of,
        starts_with,
    )
    from .client import IntegrationTestClient
    from .comparison import (
        AssetDiff,
        GapReport,
        compare_metadata,
        load_actual_output,
        load_expected_data,
    )
    from .corpus import (
        GOLDEN_ROOT_ENV,
        SUPPORTED_SUFFIXES,
        GoldenCorpus,
        GoldenLayout,
        read_records,
        require_golden_corpus,
    )
    from .lazy import Lazy, evaluate_if_lazy, is_lazy, lazy
    from .models import APIType, Scenario, ScenarioResult
    from .runner import (
        BaseIntegrationTest,
        generate_test_methods,
        parametrize_scenarios,
    )
    from .source import DataForgeSource
    from .validation import (
        format_validation_report,
        get_normalised_dataframe,
        get_schema_file_paths,
        validate_with_pandera,
    )

# =============================================================================
# Lazy re-exports (PEP 562)
# =============================================================================
#
# Every name below is re-exported from a submodule, and importing them eagerly
# made *any* import under this package pay for all of them — including
# ``application_sdk.validation``, whose pyatlan_v9 backbone is ~1.5s on its own.
#
# That became load-bearing when ``fixtures`` moved in here. Python imports a
# parent package before its submodule, so an adopting connector's
# ``from application_sdk.testing.integration.fixtures import *`` paid the whole
# eager block: ~2s warm and ~7s cold, per process, under
# ``pytest -n auto --dist=loadfile`` — and it coupled the canonical in-process
# fixture tier to ``BaseIntegrationTest``, the tier it replaces. It also left
# the star-import's viability resting on two accidents: ``pandera`` happens to
# be imported lazily inside ``validation``'s functions, and ``requests``
# happens to arrive transitively via ``pyatlan`` (it is in neither the core
# dependencies nor the ``tests`` extra).
#
# Deferring to attribute access keeps every documented import path working
# unchanged — ``from application_sdk.testing.integration import Scenario``
# still resolves — while ``fixtures`` costs only what ``fixtures`` imports.
_LAZY_EXPORTS: dict[str, str] = {
    "all_of": ".assertions",
    "any_of": ".assertions",
    "between": ".assertions",
    "contains": ".assertions",
    "custom": ".assertions",
    "ends_with": ".assertions",
    "equals": ".assertions",
    "exists": ".assertions",
    "greater_than": ".assertions",
    "greater_than_or_equal": ".assertions",
    "has_length": ".assertions",
    "is_dict": ".assertions",
    "is_empty": ".assertions",
    "is_false": ".assertions",
    "is_list": ".assertions",
    "is_none": ".assertions",
    "is_not_empty": ".assertions",
    "is_string": ".assertions",
    "is_true": ".assertions",
    "is_type": ".assertions",
    "less_than": ".assertions",
    "less_than_or_equal": ".assertions",
    "matches": ".assertions",
    "none_of": ".assertions",
    "not_contains": ".assertions",
    "not_equals": ".assertions",
    "not_one_of": ".assertions",
    "one_of": ".assertions",
    "starts_with": ".assertions",
    "IntegrationTestClient": ".client",
    "GOLDEN_ROOT_ENV": ".corpus",
    "SUPPORTED_SUFFIXES": ".corpus",
    "GoldenCorpus": ".corpus",
    "GoldenLayout": ".corpus",
    "read_records": ".corpus",
    "require_golden_corpus": ".corpus",
    "AssetDiff": ".comparison",
    "GapReport": ".comparison",
    "compare_metadata": ".comparison",
    "load_actual_output": ".comparison",
    "load_expected_data": ".comparison",
    "Lazy": ".lazy",
    "evaluate_if_lazy": ".lazy",
    "is_lazy": ".lazy",
    "lazy": ".lazy",
    "APIType": ".models",
    "Scenario": ".models",
    "ScenarioResult": ".models",
    "BaseIntegrationTest": ".runner",
    "generate_test_methods": ".runner",
    "parametrize_scenarios": ".runner",
    "DataForgeSource": ".source",
    "format_validation_report": ".validation",
    "get_normalised_dataframe": ".validation",
    "get_schema_file_paths": ".validation",
    "validate_with_pandera": ".validation",
    "AssetValidationFailure": "application_sdk.validation",
    "AssetValidationReport": "application_sdk.validation",
    "ReferentialFailure": "application_sdk.validation",
    "validate_asset": "application_sdk.validation",
    "validate_transformed_dir": "application_sdk.validation",
}


#: The submodules ``_LAZY_EXPORTS`` draws from, as bare attribute names.
#:
#: Importing a submodule eagerly binds it as an attribute of its package as a
#: side effect, so before the lazy conversion ``from .assertions import ...``
#: made ``integration.assertions`` work. Resolving names through
#: ``__getattr__`` does not, which silently turned ``integration.models.Scenario``
#: into an ``AttributeError`` — and worse, into one that depended on whether
#: something else had already touched a name from that submodule. Serving the
#: submodules here keeps that access working and makes it order-independent.
_LAZY_SUBMODULES = frozenset(
    module.lstrip(".") for module in _LAZY_EXPORTS.values() if module.startswith(".")
)


def __getattr__(name: str) -> object:
    """Import the submodule owning *name* on first access (PEP 562)."""
    from importlib import import_module  # noqa: PLC0415

    if name in _LAZY_SUBMODULES:
        value: object = import_module(f".{name}", __name__)
    else:
        module_name = _LAZY_EXPORTS.get(name)
        if module_name is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(import_module(module_name, __name__), name)
    # Cache on the module so repeat access skips this path entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS, *_LAZY_SUBMODULES})


# ``lazy`` is the one name that is both a lazy export (the decorator) and a
# submodule (its home). The lazy machinery cannot serve that collision: the
# import system binds the *submodule* onto this package as a side effect of any
# ``from .lazy import ...`` — ``runner`` does one — and an attribute already
# present on the package means ``__getattr__`` is never consulted. So the
# documented ``from application_sdk.testing.integration import lazy`` returned
# the module, and every ``lazy(...)`` call site died with ``TypeError: 'module'
# object is not callable`` — order-dependently, whenever anything touched a
# ``.lazy``-importing submodule first (resolving ``BaseIntegrationTest`` from
# this package is enough).
#
# Bind the callable eagerly instead. ``lazy.py`` imports nothing beyond the
# stdlib, so this costs the laziness budget nothing — and it wins permanently:
# the module-level name shadows ``__getattr__`` for good, and a later import of
# the submodule is served from ``sys.modules`` without re-binding the parent
# attribute. ``lazy`` stays in ``_LAZY_EXPORTS`` (and the ``TYPE_CHECKING``
# block) so the three parallel lists keep matching; its entry is simply never
# consulted at runtime.
from .lazy import lazy as lazy  # noqa: E402

# =============================================================================
# Public API
# =============================================================================

__all__ = [
    # Models
    "APIType",
    "Scenario",
    "ScenarioResult",
    # Lazy evaluation
    "Lazy",
    "lazy",
    "is_lazy",
    "evaluate_if_lazy",
    # Assertions - Basic
    "equals",
    "not_equals",
    "exists",
    "is_none",
    "is_true",
    "is_false",
    # Assertions - Collections
    "one_of",
    "not_one_of",
    "contains",
    "not_contains",
    "has_length",
    "is_empty",
    "is_not_empty",
    # Assertions - Numeric
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "between",
    # Assertions - String
    "matches",
    "starts_with",
    "ends_with",
    # Assertions - Type
    "is_type",
    "is_dict",
    "is_list",
    "is_string",
    # Assertions - Combinators
    "all_of",
    "any_of",
    "none_of",
    "custom",
    # Golden corpus layout and loader
    "GOLDEN_ROOT_ENV",
    "SUPPORTED_SUFFIXES",
    "GoldenCorpus",
    "GoldenLayout",
    "read_records",
    "require_golden_corpus",
    # Metadata Comparison
    "AssetDiff",
    "GapReport",
    "compare_metadata",
    "load_actual_output",
    "load_expected_data",
    # Client
    "IntegrationTestClient",
    # Runner
    "BaseIntegrationTest",
    "generate_test_methods",
    "parametrize_scenarios",
    # Source resolution (uniform DataForge / static-env credential access)
    "DataForgeSource",
    # Data Validation (Pandera)
    "validate_with_pandera",
    "format_validation_report",
    "get_normalised_dataframe",
    "get_schema_file_paths",
    # Asset-write Validation (pyatlan_v9 backbone) — canonical source is
    # application_sdk.validation; re-exported here for test-authoring convenience.
    "validate_asset",
    "validate_transformed_dir",
    "AssetValidationReport",
    "AssetValidationFailure",
    "ReferentialFailure",
]
