"""Built-in auth strategies for SQL and REST API clients."""

from application_sdk.clients.auth_strategies.api_key import ApiKeyAuthStrategy
from application_sdk.clients.auth_strategies.basic import BasicAuthStrategy
from application_sdk.clients.auth_strategies.bearer import BearerTokenAuthStrategy
from application_sdk.clients.auth_strategies.iam import (
    IamRoleAuthStrategy,
    IamUserAuthStrategy,
)
from application_sdk.clients.auth_strategies.keypair import KeypairAuthStrategy
from application_sdk.clients.auth_strategies.oauth import OAuthAuthStrategy

__all__ = [
    "ApiKeyAuthStrategy",
    "BasicAuthStrategy",
    "BearerTokenAuthStrategy",
    "IamRoleAuthStrategy",
    "IamUserAuthStrategy",
    "KeypairAuthStrategy",
    "OAuthAuthStrategy",
]
