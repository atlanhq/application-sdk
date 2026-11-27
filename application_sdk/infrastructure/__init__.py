"""Infrastructure abstraction layer.

Protocol-based interfaces for all infrastructure services.
Dapr implementations in _dapr/, Mock classes in testing/mocks.py.

Usage:
    from application_sdk.infrastructure import StateStore, SecretStore, PubSub
    from application_sdk.infrastructure.secrets import EnvironmentSecretStore
"""

from typing import TYPE_CHECKING

from application_sdk import _deprecation
from application_sdk._install import lazy_getattr
from application_sdk.infrastructure.bindings import (
    Binding,
    BindingError,
    BindingRequest,
    BindingResponse,
    BindingUnavailableError,
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
    CredentialVaultUnavailableError,
)
from application_sdk.infrastructure.pubsub import (
    Message,
    PubSub,
    PubSubError,
    PubSubUnavailableError,
    Subscription,
    _MessageHandler,
)
from application_sdk.infrastructure.secrets import (
    AgentSecretReadError,
    EnvironmentSecretStore,
    SecretNotFoundError,
    SecretStore,
    SecretStoreError,
    SecretStoreTransientError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
    get_deployment_secret,
)
from application_sdk.infrastructure.state import (
    StateStore,
    StateStoreError,
    StateStoreUnavailableError,
)

if TYPE_CHECKING:
    from application_sdk.infrastructure._dapr import (
        DaprBinding,
        DaprCredentialVault,
        DaprPubSub,
        DaprSecretStore,
        DaprStateStore,
    )
    from application_sdk.infrastructure._dapr.http import (
        DAPR_SECRET_STORE_COMPONENT,
        AsyncDaprClient,
        retry_past_dapr_cold_start,
    )

# Lazy (FND-4260): the Dapr implementations import storage (the credential
# vault) and the secret store, while ``storage`` imports
# ``infrastructure.context``, which runs this ``__init__`` first. Loading them
# here closed an import cycle; they load on first access.
#: Names served on first access: ``name -> (module, attribute)``.
_LAZY: dict[str, tuple[str, str]] = {
    "DaprBinding": ("application_sdk.infrastructure._dapr.client", "DaprBinding"),
    "DaprCredentialVault": (
        "application_sdk.infrastructure._dapr.credential_vault",
        "DaprCredentialVault",
    ),
    "DaprPubSub": ("application_sdk.infrastructure._dapr.client", "DaprPubSub"),
    "DaprSecretStore": (
        "application_sdk.infrastructure._dapr.client",
        "DaprSecretStore",
    ),
    "DaprStateStore": (
        "application_sdk.infrastructure._dapr.client",
        "DaprStateStore",
    ),
    "DAPR_SECRET_STORE_COMPONENT": (
        "application_sdk.infrastructure._dapr.http",
        "DAPR_SECRET_STORE_COMPONENT",
    ),
    "AsyncDaprClient": ("application_sdk.infrastructure._dapr.http", "AsyncDaprClient"),
    "retry_past_dapr_cold_start": (
        "application_sdk.infrastructure._dapr.http",
        "retry_past_dapr_cold_start",
    ),
}

#: Submodules this package imported eagerly before it went lazy: still
#: attributes of the package on first access (``infrastructure._dapr``).
_EAGER_SUBMODULES = frozenset({"_dapr"})

__all__ = [
    "AgentSecretReadError",
    "AsyncDaprClient",
    "Binding",
    "BindingError",
    "BindingRequest",
    "BindingResponse",
    "BindingUnavailableError",
    "CapacityPool",
    "CredentialVault",
    "CredentialVaultError",
    "CredentialVaultUnavailableError",
    "DAPR_SECRET_STORE_COMPONENT",
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
    "OutputBinding",
    "PubSub",
    "PubSubError",
    "PubSubUnavailableError",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreTransientError",
    "SecretStoreUnavailableError",
    "SecretStoreUnreachableError",
    "StateStore",
    "StateStoreError",
    "StateStoreUnavailableError",
    "Subscription",
    "clear_infrastructure",
    "configure_capacity_pool",
    "get_capacity_pool",
    "get_deployment_secret",
    "get_infrastructure",
    "retry_past_dapr_cold_start",
    "set_infrastructure",
]

#: name -> (replacement, why). Served by ``__getattr__`` below with a warning
#: on every access (FND-4892). Not re-exported at module scope: a real binding
#: would resolve before ``__getattr__`` and the alias would never warn.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "MessageHandler": (
        "SubscriptionConfig.handler in application_sdk.handler.contracts",
        "it typed PubSub.subscribe handlers, which DaprPubSub never called",
    ),
}


__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=lambda _name: _MessageHandler,
    fallback=lambda name: lazy_getattr(
        __name__, name, _LAZY, globals(), _EAGER_SUBMODULES
    ),
)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | _EAGER_SUBMODULES)
