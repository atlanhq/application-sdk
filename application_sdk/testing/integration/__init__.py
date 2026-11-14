"""Integration testing for Apps-SDK.

**Connectors test their App one way: the fixture kit**
(:mod:`~application_sdk.testing.integration.fixtures`). It runs the App in
process — embedded Temporal dev server, mocked infrastructure, a real worker,
and an executor that submits through the real data converter. A connector
star-imports it into ``tests/integration/conftest.py`` and overrides the App
class and its source. See ``docs/guides/integration-fixtures.md``.

**Everything named in this module's ``__all__`` is the older**
:class:`BaseIntegrationTest` **HTTP scenario framework, deprecated in 3.56 and
removed in v4.0.0** (FND-4659). Importing ``BaseIntegrationTest``,
``generate_test_methods`` or ``parametrize_scenarios`` from this package emits a
:class:`DeprecationWarning`, and conformance B001 flags it fleet-wide. So does
importing any of the 46 names from the six modules that exist only to serve it
— ``Scenario`` and the other models, the assertion predicates (``equals``,
``one_of``, ...), ``IntegrationTestClient``, ``compare_metadata`` and the rest
of ``comparison``, the pandera validators, and ``lazy`` — which leave with it in
v4.0.0 (FND-4911). The golden-corpus loader, ``DataForgeSource`` and the
``application_sdk.validation`` re-exports are not part of it and stay. Its only
SDK subclass, :class:`~application_sdk.testing.sdr.BaseSDRIntegrationTest`, is
itself deprecated for v4.0. See the "recommended pattern" and "Legacy: HTTP
scenario tests" sections of ``docs/guides/integration-testing.md``, which this
docstring follows.

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

import warnings
from typing import TYPE_CHECKING

# The ``lazy`` submodule is loaded eagerly, unlike every other submodule,
# because ``lazy`` is both an exported function and the name of the submodule
# defining it. Python binds a submodule onto its package the first time that
# submodule is loaded, so if the first load happened anywhere other than here —
# a consumer importing ``application_sdk.testing.integration.lazy`` directly,
# say — ``integration.lazy`` would be the module, ``__getattr__`` would never
# fire for it, and ``from application_sdk.testing.integration import lazy``
# would hand back something that is not callable. Loading it here means the
# first load always happens before anyone can observe it. Later loads find the
# module in ``sys.modules`` and do not rebind it.
#
# The bindings are then deleted, so all four names (deprecated, FND-4911) fall
# through to ``__getattr__`` like every other name and warn on each access. The
# ``from`` form is kept rather than a bare submodule import because the Symbol
# Removal Check reads it statically: it is how the gate sees ``Lazy``'s methods
# at this path, and they remain reachable here until v4.0.0.
from .lazy import Lazy, evaluate_if_lazy, is_lazy, lazy

del Lazy, evaluate_if_lazy, is_lazy, lazy

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
    from .lazy import Lazy, evaluate_if_lazy, is_lazy, lazy  # noqa: F811
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


#: name -> (replacement, why). The HTTP scenario framework's entry points,
#: deprecated (FND-4659): the in-process fixture kit replaces it, and its one
#: SDK subclass, ``BaseSDRIntegrationTest``, is itself deprecated for v4.0.
#: Then the 46 names from the six modules that exist only to serve it —
#: ``assertions``, ``client``, ``comparison``, ``lazy``, ``models`` and
#: ``validation`` — deprecated with it (FND-4911): no live SDK code imports
#: them, so they leave in v4.0.0 alongside the runner.
#:
#: Literal tuples per entry rather than a shared constant: the conformance
#: extractor reads this dict statically and cannot resolve a name.
#:
#: Served by ``__getattr__`` below with a warning on every access and never
#: cached, so each import still warns. ``application_sdk.testing.sdr`` and
#: ``.runner`` import the submodules directly, so the SDK's own modules stay
#: quiet.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "BaseIntegrationTest": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "the HTTP scenario framework re-tests SDK-owned routes; app behaviour belongs in the fixture kit and deployed behaviour in tests/e2e (FND-4659)",
    ),
    "generate_test_methods": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "the HTTP scenario framework re-tests SDK-owned routes; app behaviour belongs in the fixture kit and deployed behaviour in tests/e2e (FND-4659)",
    ),
    "parametrize_scenarios": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "the HTTP scenario framework re-tests SDK-owned routes; app behaviour belongs in the fixture kit and deployed behaviour in tests/e2e (FND-4659)",
    ),
    "all_of": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "any_of": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "between": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "contains": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "custom": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "ends_with": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "equals": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "exists": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "greater_than": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "greater_than_or_equal": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "has_length": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_dict": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_empty": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_false": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_list": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_none": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_not_empty": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_string": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_true": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_type": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "less_than": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "less_than_or_equal": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "matches": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "none_of": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "not_contains": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "not_equals": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "not_one_of": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "one_of": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "starts_with": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "IntegrationTestClient": (
        "AppExecutor from application_sdk.testing.integration.fixtures to run the App in process, or BaseE2ETest from application_sdk.testing.e2e for a deployed App",
        "it drives the SDK-owned HTTP routes of a running app server, which the HTTP scenario framework existed to test, and leaves with it (FND-4911)",
    ),
    "AssetDiff": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "GapReport": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "compare_metadata": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "load_actual_output": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "load_expected_data": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "Lazy": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "evaluate_if_lazy": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "is_lazy": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "lazy": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "APIType": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "Scenario": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "ScenarioResult": (
        "the in-process fixture kit, application_sdk.testing.integration.fixtures",
        "it exists only to serve the deprecated BaseIntegrationTest HTTP scenario framework and leaves with it (FND-4911)",
    ),
    "format_validation_report": (
        "application_sdk.testing.integration.output_schema — assert_output_matches, with load_pandera_schemas to read existing pandera YAML",
        "it checks a run's output without pandas or pandera, and reads the same schema files (FND-5007); the pandera path leaves with the HTTP scenario framework (FND-4911)",
    ),
    "get_normalised_dataframe": (
        "application_sdk.testing.integration.output_schema — assert_output_matches, with load_pandera_schemas to read existing pandera YAML",
        "it checks a run's output without pandas or pandera, and reads the same schema files (FND-5007); the pandera path leaves with the HTTP scenario framework (FND-4911)",
    ),
    "get_schema_file_paths": (
        "application_sdk.testing.integration.output_schema — assert_output_matches, with load_pandera_schemas to read existing pandera YAML",
        "it checks a run's output without pandas or pandera, and reads the same schema files (FND-5007); the pandera path leaves with the HTTP scenario framework (FND-4911)",
    ),
    "validate_with_pandera": (
        "application_sdk.testing.integration.output_schema — assert_output_matches, with load_pandera_schemas to read existing pandera YAML",
        "it checks a run's output without pandas or pandera, and reads the same schema files (FND-5007); the pandera path leaves with the HTTP scenario framework (FND-4911)",
    ),
}


def __getattr__(name: str) -> object:
    """Import the submodule owning *name* on first access (PEP 562)."""
    from importlib import import_module  # noqa: PLC0415

    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is not None:
        replacement, note = entry
        warnings.warn(
            f"{name} is deprecated; use {replacement} instead — {note}. "
            "Will be removed in v4.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(import_module(_LAZY_EXPORTS[name], __name__), name)

    # Exports are checked before the submodule shortcut so that a name shared
    # by a submodule and one of its functions resolves to the function — the
    # documented import — never to the module.
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is not None:
        value: object = getattr(import_module(module_name, __name__), name)
    elif name in _LAZY_SUBMODULES:
        value = import_module(f".{name}", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # Cache on the module so repeat access skips this path entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS, *_LAZY_SUBMODULES})


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
