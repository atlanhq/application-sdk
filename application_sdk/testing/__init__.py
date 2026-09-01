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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

_GOLDEN_EXPORTS = frozenset(
    {
        "NO_TYPENAME",
        "AssetMismatch",
        "DiffPolicy",
        "DuplicateKeyPolicy",
        "FieldDiff",
        "GoldenDuplicateKeyError",
        "GoldenReport",
        "GoldenRuleError",
        "TypenameDiff",
        "TypenameRule",
        "assert_matches_golden",
        "diff_golden",
    }
)


def __getattr__(name: str) -> object:
    """Resolve the golden-diff symbols lazily (PEP 562).

    :mod:`application_sdk.testing.golden` imports the parity comparator, which
    nothing else in this package needs — loading it eagerly would tax every
    ``import application_sdk.testing`` for the one workflow that diffs against
    a golden corpus.
    """
    if name in _GOLDEN_EXPORTS:
        from application_sdk.testing import golden  # noqa: PLC0415

        return getattr(golden, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "app_context",
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
