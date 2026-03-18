"""Infrastructure abstraction layer.

Protocol-based interfaces for all infrastructure services.
Dapr implementations in _dapr/, InMemory for testing.

Usage:
    from application_sdk.infrastructure import StateStore, SecretStore, PubSub
    from application_sdk.infrastructure.state import InMemoryStateStore
    from application_sdk.infrastructure.secrets import EnvironmentSecretStore
"""

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
from application_sdk.infrastructure.pubsub import (
    InMemoryPubSub,
    Message,
    MessageHandler,
    PubSub,
    PubSubError,
    Subscription,
)
from application_sdk.infrastructure.secrets import (
    EnvironmentSecretStore,
    InMemorySecretStore,
    SecretNotFoundError,
    SecretStore,
    SecretStoreError,
)
from application_sdk.infrastructure.state import (
    InMemoryStateStore,
    StateStore,
    StateStoreError,
)

__all__ = [
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
    "InMemoryPubSub",
    "Message",
    "MessageHandler",
    "PubSub",
    "PubSubError",
    "Subscription",
    "EnvironmentSecretStore",
    "InMemorySecretStore",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreError",
    "InMemoryStateStore",
    "StateStore",
    "StateStoreError",
    "InfrastructureContext",
    "get_infrastructure",
    "set_infrastructure",
]
