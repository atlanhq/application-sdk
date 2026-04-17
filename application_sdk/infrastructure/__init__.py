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
    "DaprBinding",
    "DaprCredentialVault",
    "DaprPubSub",
    "DaprSecretStore",
    "DaprStateStore",
    "Binding",
    "BindingError",
    "BindingRequest",
    "BindingResponse",
    "InputBinding",
    "OutputBinding",
    "CapacityPool",
    "LocalCapacityPool",
    "configure_capacity_pool",
    "get_capacity_pool",
    "CredentialVault",
    "CredentialVaultError",
    "Message",
    "MessageHandler",
    "PubSub",
    "PubSubError",
    "Subscription",
    "EnvironmentSecretStore",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreError",
    "get_deployment_secret",
    "StateStore",
    "StateStoreError",
    "InfrastructureContext",
    "get_infrastructure",
    "set_infrastructure",
]
