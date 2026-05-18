"""Publish-preflight Temporal activity mixin for App subclasses.

Provides :class:`PublishPreflightMixin` — a base implementation that runs
user-enabled and publish-permission checks as the **first Temporal activity**
in a workflow, before any extraction starts.

Covers both manual and scheduled workflow runs (scheduled runs bypass the
Heracles HTTP-preflight call and go directly to Temporal, so this activity
is the only gate that fires for them).

Usage::

    from application_sdk.app import App, entrypoint, task
    from application_sdk.app.preflight import PublishPreflightMixin
    from dataclasses import dataclass
    from application_sdk.contracts.base import Input, Output

    @dataclass
    class MinerInput(Input):
        connection_qualified_name: str = ""
        user_id: str = ""           # injected by Heracles as "user-id" DAG arg

    class SnowflakeMinerApp(PublishPreflightMixin, App):
        @entrypoint
        async def run(self, input: MinerInput) -> MinerOutput:
            await self.run_publish_preflight(input)   # ← first activity; fails fast
            await self.extract_queries(input)
            ...
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Annotated, Any

from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import MaxItems
from application_sdk.handler.checks import (
    check_atlan_publish_permission,
    check_user_enabled,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass
class PreflightInput(Input):
    """Minimal input for the publish-preflight activity.

    Connectors typically pass their full workflow ``Input`` (which extends
    this class or carries these fields) rather than constructing a separate
    ``PreflightInput``.
    """

    user_id: str = ""
    """Keycloak user GUID of the workflow creator.

    Heracles injects this as ``user-id`` in the DAG args.  The field name
    uses an underscore so it survives Temporal/Pydantic serialisation;
    the alias ``user-id`` is accepted on deserialisation via the
    ``_warn_on_unknown_keys`` validator on the base ``Input``.
    """

    connection_qualified_name: str = ""
    """Atlan connection qualifiedName (e.g. ``default/snowflake/1234567890``)."""


@dataclass
class PreflightOutput(Output, allow_unbounded_fields=True):
    """Result of the publish-preflight activity."""

    passed: bool = True
    """True when all checks passed; False when at least one check failed."""

    checks: Annotated[list[dict[str, Any]], MaxItems(20)] = field(default_factory=list)
    """Individual check results serialised as dicts for Temporal compatibility."""

    message: str = ""
    """Human-readable summary."""


async def _get_service_token() -> str:
    """Obtain a service bearer token using the app's client_credentials.

    Reads ``ATLAN_AUTH_CLIENT_ID`` / ``ATLAN_AUTH_CLIENT_SECRET`` from the
    deployment secret store and exchanges them for a bearer token via
    ``ATLAN_AUTH_URL``.  Returns an empty string if any credential is absent
    (caller treats this as a non-fatal skip).
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path
        AUTH_URL,
        WORKFLOW_AUTH_CLIENT_ID_KEY,
        WORKFLOW_AUTH_CLIENT_SECRET_KEY,
    )
    from application_sdk.credentials.oauth import OAuthTokenService  # noqa: PLC0415
    from application_sdk.credentials.types import OAuthClientCredential  # noqa: PLC0415
    from application_sdk.infrastructure.secrets import (
        get_deployment_secret,
    )

    client_id = await get_deployment_secret(WORKFLOW_AUTH_CLIENT_ID_KEY)
    client_secret = await get_deployment_secret(WORKFLOW_AUTH_CLIENT_SECRET_KEY)
    token_url = AUTH_URL

    if not client_id or not client_secret or not token_url:
        return ""

    svc = OAuthTokenService(
        OAuthClientCredential(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
        )
    )
    return await svc.get_token()


class PublishPreflightMixin:
    """Mixin that adds a ``run_publish_preflight`` Temporal activity to an App.

    Mix this in before ``App`` in the class hierarchy::

        class MyApp(PublishPreflightMixin, App):
            ...

    Call ``run_publish_preflight`` as the **first** activity in every
    ``@entrypoint`` method.  It runs the following checks in parallel:

    - **UserEnabled** — confirms the triggering user's Keycloak account is active.
    - **AtlanPublishPermission** — confirms ENTITY_CREATE / UPDATE / DELETE on the
      target connection (requires impersonation credentials; skipped gracefully if
      not available).

    On failure the activity raises :class:`~application_sdk.errors.AppError`,
    which Temporal classifies as a non-retryable workflow failure so the error
    is surfaced immediately without retrying the full pipeline.
    """

    @task(timeout_seconds=60)
    async def run_publish_preflight(
        self,
        input: PreflightInput,
    ) -> PreflightOutput:
        """Run user-enabled and publish-permission checks before extraction starts.

        Args:
            input: Any :class:`~application_sdk.contracts.base.Input` subclass
                carrying ``user_id`` and ``connection_qualified_name`` fields.

        Returns:
            :class:`PreflightOutput` — ``passed=True`` when all checks pass.

        Raises:
            AppError: When ``user_id`` is present and any check fails.  The
                error message includes the first failing check's detail so the
                workflow failure is actionable from the Temporal UI.
        """
        from application_sdk.errors.leaves import (
            AppPermissionDeniedError as AppPermissionError,
        )

        user_id = getattr(input, "user_id", "") or ""
        connection_qname = (
            getattr(input, "connection_qualified_name", "") or ""
        )
        heracles_url = os.environ.get(
            "ATLAN_INTERNAL_HERACLES_URL",
            os.environ.get("HERACLES_URL", "http://localhost:5201"),
        )

        if not user_id:
            logger.warning(
                "run_publish_preflight: user_id not present in input — skipping checks. "
                "Add 'user_id: str = \"\"' to your workflow Input dataclass."
            )
            return PreflightOutput(
                passed=True,
                message="Preflight skipped — user_id not available in workflow input.",
            )

        bearer_token = await _get_service_token()
        if not bearer_token:
            logger.warning(
                "run_publish_preflight: service token unavailable — skipping checks."
            )
            return PreflightOutput(
                passed=True,
                message="Preflight skipped — service credentials not configured.",
            )

        token_url = os.environ.get("ATLAN_AUTH_URL", "")
        client_id_key = os.environ.get("ATLAN_AUTH_CLIENT_ID_KEY", "ATLAN_AUTH_CLIENT_ID")
        client_secret_key = os.environ.get(
            "ATLAN_AUTH_CLIENT_SECRET_KEY", "ATLAN_AUTH_CLIENT_SECRET"
        )

        from application_sdk.infrastructure.secrets import (
            get_deployment_secret,
        )

        client_id = await get_deployment_secret(client_id_key) or ""
        client_secret = await get_deployment_secret(client_secret_key) or ""

        if connection_qname:
            checks = await check_atlan_publish_permission(
                user_id=user_id,
                bearer_token=bearer_token,
                connection_qualified_name=connection_qname,
                heracles_url=heracles_url,
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
            )
        else:
            checks = [
                await check_user_enabled(
                    user_id, bearer_token, heracles_url=heracles_url
                )
            ]

        checks_as_dicts = [c.model_dump() for c in checks]
        failed = [c for c in checks if not c.passed]

        if failed:
            first_failure = failed[0].message
            logger.error(
                "run_publish_preflight FAILED user_id=%s connection=%s failed_checks=%s",
                user_id,
                connection_qname,
                [c.name for c in failed],
            )
            raise AppPermissionError(
                message=first_failure,
            )

        logger.info(
            "run_publish_preflight PASSED user_id=%s connection=%s",
            user_id,
            connection_qname,
        )
        return PreflightOutput(
            passed=True,
            checks=checks_as_dicts,
            message="All publish preflight checks passed.",
        )
