"""Base test class and helpers for Self-Deployed Runtime (SDR) integration tests.

SDR integration tests run a connector app inside a real customer-style
docker compose stack (atlan-configurator generated, Dapr embedded,
Temporal on a CI test tenant) and exercise the full credential
resolution + workflow execution path.

See `application_sdk.testing.sdr.base.BaseSDRIntegrationTest` for the
pytest base class connectors subclass.
"""

from .base import BaseSDRIntegrationTest

__all__ = ["BaseSDRIntegrationTest"]
