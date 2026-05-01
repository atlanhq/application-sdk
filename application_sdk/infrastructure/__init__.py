"""Infrastructure abstraction layer.

Protocol-based interfaces for all infrastructure services.
Dapr implementations in _dapr/, Mock classes in testing/mocks.py.

Usage:
    from application_sdk.infrastructure import StateStore, SecretStore, PubSub
    from application_sdk.infrastructure.secrets import EnvironmentSecretStore
"""

from application_sdk.infrastructure._dapr import (
    DaprBinding,
    DaprCredentialVault,
    DaprPubSub,
    DaprSecretStore,
    DaprStateStore,
)
from application_sdk.infrastructure._dapr.http import AsyncDaprClient
from application_sdk.infrastructure.bindings import (
    Binding,
    BindingError,
    BindingRequest,
    BindingResponse,
    InputBinding,
    OutputBinding,
)
from application_sdk.infrastructure.capacity import (
    CapacityPool,
    LocalCapacityPool,
    configure_capacity_pool,
    get_capacity_pool,
)
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    get_infrastructure,
    set_infrastructure,
)
from application_sdk.infrastructure.credential_vault import (
    CredentialVault,
    CredentialVaultError,
)
from application_sdk.infrastructure.pubsub import (
    Message,
    MessageHandler,
    PubSub,
    PubSubError,
    Subscription,
)
from application_sdk.infrastructure.secrets import (
    EnvironmentSecretStore,
    SecretNotFoundError,
    SecretStore,
    SecretStoreError,
    get_deployment_secret,
)
from application_sdk.infrastructure.state import StateStore, StateStoreError

__all__ = [
    "AsyncDaprClient",
    "Binding",
    "BindingError",
    "BindingRequest",
    "BindingResponse",
    "CapacityPool",
    "CredentialVault",
    "CredentialVaultError",
    "DaprBinding",
    "DaprCredentialVault",
    "DaprPubSub",
    "DaprSecretStore",
    "DaprStateStore",
    "EnvironmentSecretStore",
    "InfrastructureContext",
    "InputBinding",
    "LocalCapacityPool",
    "Message",
    "MessageHandler",
    "OutputBinding",
    "PubSub",
    "PubSubError",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreError",
    "StateStore",
    "StateStoreError",
    "Subscription",
    "clear_infrastructure",
    "configure_capacity_pool",
    "get_capacity_pool",
    "get_deployment_secret",
    "get_infrastructure",
    "set_infrastructure",
]
