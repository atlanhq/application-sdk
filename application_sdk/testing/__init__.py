"""Testing utilities for application_sdk Apps.

This module provides mock infrastructure implementations with call-tracking
and pytest fixtures, so tests never need to import temporalio directly.

Usage::

    from application_sdk.testing import MockStateStore, app_context, MockCredentialStore

For an HTTP source with no container to pull, :class:`HttpFakeSource` fills the
same fixture slot a testcontainer would::

    from application_sdk.testing import HttpFakeSource

Its pytest fixtures are part of the integration kit rather than this module, so
one star-import gives a connector the factory and its autouse reset together::

    from application_sdk.testing.integration.fixtures import *  # noqa: F403

To assert what the preflight gate decided, read its outcome row through the
shared capture rather than patching the gate's logger by hand — the row is
level-carried, so an ``info``-only reader goes blind when a verdict changes
level::

    from application_sdk.testing import capture_preflight_outcomes

To test an app's warmup without a real warehouse, script the source's answers
with :class:`WarmingSource` (and, for gate tests, :class:`WarmingSourceHandler`)::

    from application_sdk.testing import WarmingSource

To assert what a manifest's ``{{...}}`` placeholders resolve to, run the same
walker the e2e and SDR harnesses use rather than re-implementing it::

    from application_sdk.testing import apply_mustache_subs

Fixtures (import into conftest.py or test files)::

    from application_sdk.testing import (
        app_context,
        capture_preflight_outcomes,
        clean_app_registry,
        clean_task_registry,
        mock_binding,
        mock_credential_store,
        mock_heartbeat,
        mock_pubsub,
        mock_secret_store,
        mock_state_store,
        restore_logger_init_flags,
    )
"""

from typing import TYPE_CHECKING, Any

from application_sdk._install import lazy_getattr

if TYPE_CHECKING:
    from application_sdk.testing._mustache import apply_mustache_subs
    from application_sdk.testing.fake_source import (
        Authorizer,
        CursorPage,
        FakeRequest,
        FakeResponse,
        Handler,
        HandlerResult,
        HttpFakeSource,
        HttpFakeSourceFactory,
        cursor_page,
    )
    from application_sdk.testing.fixtures import (
        app_context,
        clean_app_registry,
        clean_task_registry,
        mock_binding,
        mock_credential_store,
        mock_heartbeat,
        mock_pubsub,
        mock_secret_store,
        mock_state_store,
        restore_logger_init_flags,
    )
    from application_sdk.testing.golden import (
        NO_TYPENAME,
        AssetMismatch,
        DiffPolicy,
        DuplicateKeyPolicy,
        FieldDiff,
        GoldenDuplicateKeyError,
        GoldenReport,
        GoldenRuleError,
        TypenameDiff,
        TypenameRule,
        assert_matches_golden,
        diff_golden,
    )
    from application_sdk.testing.mocks import (
        MockBinding,
        MockCredentialStore,
        MockHeartbeatController,
        MockPubSub,
        MockSecretStore,
        MockStateStore,
    )
    from application_sdk.testing.preflight import (
        OUTCOME_LEVELS,
        PreflightOutcomeCapture,
        capture_preflight_outcomes,
        first_outcome_or_none,
        outcome_level,
        outcome_rows,
        single_outcome,
    )
    from application_sdk.testing.volatile_fields import (
        ENVIRONMENT_SCOPED_FIELDS,
        ENVIRONMENT_SCOPED_NESTED_FIELDS,
        RUN_VOLATILE_FIELDS,
    )
    from application_sdk.testing.warming import (
        WarmingSource,
        WarmingSourceHandler,
        WarmingStep,
    )

#: Every public name, served on first access (PEP 562). Importing the package —
#: which ``import application_sdk.testing.e2e.credential`` does on the way to a
#: pydantic model — must not load pytest, Temporal or the Atlan client for the
#: names the caller never touches (FND-4658).
_LAZY: dict[str, tuple[str, str]] = {
    "apply_mustache_subs": ("application_sdk.testing._mustache", "apply_mustache_subs"),
    "Authorizer": ("application_sdk.testing.fake_source", "Authorizer"),
    "CursorPage": ("application_sdk.testing.fake_source", "CursorPage"),
    "FakeRequest": ("application_sdk.testing.fake_source", "FakeRequest"),
    "FakeResponse": ("application_sdk.testing.fake_source", "FakeResponse"),
    "Handler": ("application_sdk.testing.fake_source", "Handler"),
    "HandlerResult": ("application_sdk.testing.fake_source", "HandlerResult"),
    "HttpFakeSource": ("application_sdk.testing.fake_source", "HttpFakeSource"),
    "HttpFakeSourceFactory": (
        "application_sdk.testing.fake_source",
        "HttpFakeSourceFactory",
    ),
    "cursor_page": ("application_sdk.testing.fake_source", "cursor_page"),
    "app_context": ("application_sdk.testing.fixtures", "app_context"),
    "clean_app_registry": ("application_sdk.testing.fixtures", "clean_app_registry"),
    "clean_task_registry": ("application_sdk.testing.fixtures", "clean_task_registry"),
    "mock_binding": ("application_sdk.testing.fixtures", "mock_binding"),
    "mock_credential_store": (
        "application_sdk.testing.fixtures",
        "mock_credential_store",
    ),
    "mock_heartbeat": ("application_sdk.testing.fixtures", "mock_heartbeat"),
    "mock_pubsub": ("application_sdk.testing.fixtures", "mock_pubsub"),
    "mock_secret_store": ("application_sdk.testing.fixtures", "mock_secret_store"),
    "mock_state_store": ("application_sdk.testing.fixtures", "mock_state_store"),
    "restore_logger_init_flags": (
        "application_sdk.testing.fixtures",
        "restore_logger_init_flags",
    ),
    "NO_TYPENAME": ("application_sdk.testing.golden", "NO_TYPENAME"),
    "AssetMismatch": ("application_sdk.testing.golden", "AssetMismatch"),
    "DiffPolicy": ("application_sdk.testing.golden", "DiffPolicy"),
    "DuplicateKeyPolicy": ("application_sdk.testing.golden", "DuplicateKeyPolicy"),
    "FieldDiff": ("application_sdk.testing.golden", "FieldDiff"),
    "GoldenDuplicateKeyError": (
        "application_sdk.testing.golden",
        "GoldenDuplicateKeyError",
    ),
    "GoldenReport": ("application_sdk.testing.golden", "GoldenReport"),
    "GoldenRuleError": ("application_sdk.testing.golden", "GoldenRuleError"),
    "TypenameDiff": ("application_sdk.testing.golden", "TypenameDiff"),
    "TypenameRule": ("application_sdk.testing.golden", "TypenameRule"),
    "assert_matches_golden": (
        "application_sdk.testing.golden",
        "assert_matches_golden",
    ),
    "diff_golden": ("application_sdk.testing.golden", "diff_golden"),
    "MockBinding": ("application_sdk.testing.mocks", "MockBinding"),
    "MockCredentialStore": ("application_sdk.testing.mocks", "MockCredentialStore"),
    "MockHeartbeatController": (
        "application_sdk.testing.mocks",
        "MockHeartbeatController",
    ),
    "MockPubSub": ("application_sdk.testing.mocks", "MockPubSub"),
    "MockSecretStore": ("application_sdk.testing.mocks", "MockSecretStore"),
    "MockStateStore": ("application_sdk.testing.mocks", "MockStateStore"),
    "OUTCOME_LEVELS": ("application_sdk.testing.preflight", "OUTCOME_LEVELS"),
    "PreflightOutcomeCapture": (
        "application_sdk.testing.preflight",
        "PreflightOutcomeCapture",
    ),
    "capture_preflight_outcomes": (
        "application_sdk.testing.preflight",
        "capture_preflight_outcomes",
    ),
    "first_outcome_or_none": (
        "application_sdk.testing.preflight",
        "first_outcome_or_none",
    ),
    "outcome_level": ("application_sdk.testing.preflight", "outcome_level"),
    "outcome_rows": ("application_sdk.testing.preflight", "outcome_rows"),
    "single_outcome": ("application_sdk.testing.preflight", "single_outcome"),
    "ENVIRONMENT_SCOPED_FIELDS": (
        "application_sdk.testing.volatile_fields",
        "ENVIRONMENT_SCOPED_FIELDS",
    ),
    "ENVIRONMENT_SCOPED_NESTED_FIELDS": (
        "application_sdk.testing.volatile_fields",
        "ENVIRONMENT_SCOPED_NESTED_FIELDS",
    ),
    "RUN_VOLATILE_FIELDS": (
        "application_sdk.testing.volatile_fields",
        "RUN_VOLATILE_FIELDS",
    ),
    "WarmingSource": ("application_sdk.testing.warming", "WarmingSource"),
    "WarmingSourceHandler": ("application_sdk.testing.warming", "WarmingSourceHandler"),
    "WarmingStep": ("application_sdk.testing.warming", "WarmingStep"),
}

#: Submodules this package imported eagerly before it went lazy, so
#: ``import pkg; pkg.<submodule>`` keeps resolving them.
_EAGER_SUBMODULES: tuple[str, ...] = (
    "_mustache",
    "fake_source",
    "fixtures",
    "mocks",
    "preflight",
    "volatile_fields",
    "warming",
)

__all__ = [
    "ENVIRONMENT_SCOPED_FIELDS",
    "ENVIRONMENT_SCOPED_NESTED_FIELDS",
    "NO_TYPENAME",
    "RUN_VOLATILE_FIELDS",
    "AssetMismatch",
    "Authorizer",
    "CursorPage",
    "DiffPolicy",
    "DuplicateKeyPolicy",
    "FakeRequest",
    "FieldDiff",
    "GoldenDuplicateKeyError",
    "GoldenRuleError",
    "FakeResponse",
    "GoldenReport",
    "Handler",
    "HandlerResult",
    "HttpFakeSource",
    "HttpFakeSourceFactory",
    "MockBinding",
    "MockCredentialStore",
    "MockHeartbeatController",
    "MockPubSub",
    "MockSecretStore",
    "MockStateStore",
    "OUTCOME_LEVELS",
    "PreflightOutcomeCapture",
    "TypenameDiff",
    "TypenameRule",
    "WarmingSource",
    "WarmingSourceHandler",
    "WarmingStep",
    "app_context",
    "apply_mustache_subs",
    "assert_matches_golden",
    "capture_preflight_outcomes",
    "clean_app_registry",
    "clean_task_registry",
    "cursor_page",
    "diff_golden",
    "first_outcome_or_none",
    "mock_binding",
    "mock_credential_store",
    "mock_heartbeat",
    "mock_pubsub",
    "mock_secret_store",
    "mock_state_store",
    "outcome_level",
    "outcome_rows",
    "restore_logger_init_flags",
    "single_outcome",
]


def __getattr__(name: str) -> Any:
    return lazy_getattr(__name__, name, _LAZY, globals(), _EAGER_SUBMODULES)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
