"""Testing utilities for application_sdk Apps.

This module provides mock infrastructure implementations with call-tracking
and pytest fixtures, so tests never need to import temporalio directly.

Usage::

    from application_sdk.testing import MockStateStore, app_context, MockCredentialStore

For an HTTP source with no container to pull, :class:`HttpFakeSource` fills the
same fixture slot a testcontainer would::

    from application_sdk.testing import HttpFakeSource, cursor_page

Fixtures (import into conftest.py or test files)::

    from application_sdk.testing import (
        app_context,
        clean_app_registry,
        clean_task_registry,
        mock_binding,
        mock_credential_store,
        mock_heartbeat,
        mock_pubsub,
        mock_secret_store,
        mock_state_store,
    )
"""

from application_sdk.testing.fake_source import (
    CursorPage,
    FakeRequest,
    FakeResponse,
    HttpFakeSource,
    cursor_page,
    serve,
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
)
from application_sdk.testing.mocks import (
    MockBinding,
    MockCredentialStore,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)
from application_sdk.testing.volatile_fields import (
    ENVIRONMENT_SCOPED_FIELDS,
    ENVIRONMENT_SCOPED_NESTED_FIELDS,
    RUN_VOLATILE_FIELDS,
)

__all__ = [
    "CursorPage",
    "FakeRequest",
    "FakeResponse",
    "HttpFakeSource",
    "MockBinding",
    "MockCredentialStore",
    "MockHeartbeatController",
    "MockPubSub",
    "MockSecretStore",
    "MockStateStore",
    "app_context",
    "clean_app_registry",
    "clean_task_registry",
    "cursor_page",
    "mock_binding",
    "mock_credential_store",
    "mock_heartbeat",
    "mock_pubsub",
    "mock_secret_store",
    "mock_state_store",
<<<<<<< HEAD
<<<<<<< HEAD
    "serve",
=======
    # Golden-diff assertion
    "AssetMismatch",
    "DiffPolicy",
    "GoldenReport",
    "TypenameDiff",
    "TypenameRule",
    "assert_matches_golden",
    "diff_golden",
=======
>>>>>>> 17882e8c (refactor(testing): drop the golden-diff assertion, keep the volatile-field consolidation | FND-820)
    # Canonical comparison field sets
    "ENVIRONMENT_SCOPED_FIELDS",
    "ENVIRONMENT_SCOPED_NESTED_FIELDS",
    "RUN_VOLATILE_FIELDS",
>>>>>>> 4af96461 (refactor(testing): one golden-diff assertion and one volatile-field set (FND-819, FND-820))
]
