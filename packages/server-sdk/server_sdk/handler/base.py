"""Handler base — the three server operations.

Subclass :class:`Handler` and implement ``test_auth`` / ``preflight_check`` /
``fetch_metadata``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from server_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)


class Handler(ABC):
    """Implement the three core server operations for an app.

    Maps to routes:
        POST /workflows/v1/auth     → test_auth
        POST /workflows/v1/check    → preflight_check
        POST /workflows/v1/metadata → fetch_metadata
    """

    @abstractmethod
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Verify the credentials can reach the source."""
        ...

    @abstractmethod
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run connectivity/permission checks; the returned status gates the run."""
        ...

    @abstractmethod
    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Return browseable metadata (e.g. catalog/schema pairs) for the UI."""
        ...


class DefaultHandler(Handler):
    """Pass-through handler — SUCCESS / READY / empty. Useful for thin apps."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(
            status=AuthStatus.SUCCESS, message="Authentication successful"
        )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.READY, message="No preflight handler registered"
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])
