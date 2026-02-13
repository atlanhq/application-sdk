"""Protocol implementations for credential authentication.

This module provides the 6 fundamental protocol implementations
plus a custom protocol base for escape hatch scenarios.
"""

from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.protocols.certificate import CertificateProtocol
from application_sdk.credentials.protocols.connection import ConnectionProtocol
from application_sdk.credentials.protocols.custom import CustomProtocol
from application_sdk.credentials.protocols.identity_pair import IdentityPairProtocol
from application_sdk.credentials.protocols.request_signing import RequestSigningProtocol
from application_sdk.credentials.protocols.static_secret import StaticSecretProtocol
from application_sdk.credentials.protocols.token_exchange import TokenExchangeProtocol

__all__ = [
    "BaseProtocol",
    "StaticSecretProtocol",
    "IdentityPairProtocol",
    "TokenExchangeProtocol",
    "RequestSigningProtocol",
    "CertificateProtocol",
    "ConnectionProtocol",
    "CustomProtocol",
]
