"""SQLHandler — the declarative base for SQL-connector server handlers.

Serves the HTTP auth / preflight / metadata surface only — extraction logic and
workflow templates live in the app's worker package, not here.

A typical connector subclasses this with a few class attributes and writes no
handler logic at all::

    class PostgresServerHandler(SQLHandler):
        client_class = PostgresServerClient
        filter_metadata_sql = (
            "SELECT catalog_name, schema_name FROM information_schema.schemata"
        )

Contract for ``filter_metadata_sql``: it MUST yield ``catalog_name`` and
``schema_name`` columns (matched case-insensitively) — alias in the SQL if the
source spells them differently. There are deliberately no column-mapping knobs;
the SQL is the single place the shape is controlled.

Connectors with richer behavior (tiered preflight, entrypoint branching, widget
metadata dispatch) override the corresponding method — plain inheritance, no
registration or configuration involved.

Stub credentials: the workflow run page speculatively POSTs
``/workflows/v1/check`` with no connection fields (only ``authType`` /
``connectorConfigName`` / ``extra``). When none of
:attr:`connection_identity_fields` are present, ``preflight_check`` returns
READY with a ``credentialsProvided`` no-op check instead of a driver error —
the real preflight runs at workflow-gate time with resolved credentials.
``test_auth`` deliberately does NOT stub: an auth test without credentials is
a real failure.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from server_sdk.clients.sql import BaseSQLClient
from server_sdk.credentials.utils import credentials_list_to_dict
from server_sdk.errors.base import AppError
from server_sdk.handler.base import Handler
from server_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class SQLHandler(Handler):
    """Generic auth / preflight / metadata implementation for SQL connectors.

    Subclasses set:
        client_class: the connector's :class:`BaseSQLClient` subclass.
        filter_metadata_sql: query yielding ``catalog_name`` + ``schema_name``.
        test_authentication_sql: connectivity probe (default ``SELECT 1``).
        connection_identity_fields: credential keys whose total absence marks a
            stub ``/check`` request (default ``("host",)``; e.g. snowflake uses
            ``("account_id",)``).
    """

    client_class: ClassVar[type[BaseSQLClient]]
    test_authentication_sql: ClassVar[str] = "SELECT 1"
    filter_metadata_sql: ClassVar[str]
    connection_identity_fields: ClassVar[tuple[str, ...]] = ("host",)

    # -- the three operations ----------------------------------------------

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        client = None
        try:
            client = await self._build_client(input.credentials)
            async for _ in client.run_query(self.test_authentication_sql):
                pass
            return AuthOutput(
                status=AuthStatus.SUCCESS, message="Authentication successful"
            )
        except Exception as e:  # noqa: BLE001 — boundary: report FAILED, never 500
            logger.warning(
                "%s auth test failed: %s", type(self).__name__, e, exc_info=True
            )
            return AuthOutput(status=AuthStatus.FAILED, message=str(e))
        finally:
            if client:
                await client.close()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        client = None
        started = time.monotonic()
        try:
            # Inside the try so a malformed credential payload becomes NOT_READY,
            # not a 500 (the "never 500" boundary the routes rely on).
            if self._is_stub_credentials(credentials_list_to_dict(input.credentials)):
                logger.info(
                    "Preflight stub detected (none of %s present) — READY no-op; "
                    "real preflight runs at workflow-gate time",
                    self.connection_identity_fields,
                )
                return PreflightOutput(
                    status=PreflightStatus.READY,
                    checks=[
                        PreflightCheck(
                            name="credentialsProvided",
                            passed=True,
                            message=(
                                "No inline credentials in /check request — real "
                                "preflight runs at workflow execution time"
                            ),
                        )
                    ],
                )

            client = await self._build_client(input.credentials)
            async for _ in client.run_query(self.test_authentication_sql):
                pass
            return PreflightOutput(
                status=PreflightStatus.READY,
                checks=[
                    PreflightCheck(
                        name="connectivity",
                        passed=True,
                        message="Connected and authenticated",
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001 — boundary: report NOT_READY, never 500
            logger.warning(
                "%s preflight failed: %s", type(self).__name__, exc, exc_info=True
            )
            classified = self.classify_exception(exc)
            check = PreflightCheck(
                name=(
                    "authentication"
                    if classified is not None and classified.category.value == "AUTH"
                    else "connectivity"
                ),
                passed=False,
                message=(classified.message if classified is not None else str(exc)),
                duration_ms=(time.monotonic() - started) * 1000,
                error=(
                    classified.to_failure_details() if classified is not None else None
                ),
            )
            return PreflightOutput(status=PreflightStatus.NOT_READY, checks=[check])
        finally:
            if client:
                await client.close()

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        client = None
        try:
            client = await self._build_client(input.credentials)
            objects: list[SqlMetadataObject] = []
            rows_seen = 0
            async for batch in client.run_query(self.filter_metadata_sql):
                for row in batch:
                    rows_seen += 1
                    ci = {str(k).lower(): v for k, v in row.items()}
                    schema = str(ci.get("schema_name") or "").strip()
                    if not schema:
                        continue
                    objects.append(
                        SqlMetadataObject(
                            TABLE_CATALOG=str(ci.get("catalog_name") or "").strip()
                            or "DEFAULT",
                            TABLE_SCHEMA=schema,
                        )
                    )
            if rows_seen and not objects:
                # Almost always a filter_metadata_sql that doesn't yield the
                # canonical catalog_name/schema_name columns.
                logger.warning(
                    "%s fetch_metadata: %d rows returned but none mapped — "
                    "filter_metadata_sql must yield catalog_name + schema_name",
                    type(self).__name__,
                    rows_seen,
                )
            return SqlMetadataOutput(objects=objects)
        except Exception as e:  # noqa: BLE001 — boundary: empty list to the UI, never 500
            logger.warning(
                "%s fetch_metadata failed: %s", type(self).__name__, e, exc_info=True
            )
            return SqlMetadataOutput(objects=[])
        finally:
            if client:
                await client.close()

    # -- seams subclasses may override ---------------------------------------

    async def _build_client(self, credentials: Any) -> BaseSQLClient:
        """Build and load the connector client from wire credentials.

        Server-side cursors are off: these endpoints run small queries, and
        some sources (e.g. Redshift) allow only one named cursor per session.
        """
        creds = credentials_list_to_dict(credentials)
        client = self.client_class(use_server_side_cursor=False)
        await client.load(creds)
        return client

    def classify_exception(self, exc: Exception) -> AppError | None:
        """Map a driver exception to a typed :class:`AppError`, or ``None``.

        Default: no classification (the check reports ``connectivity`` with the
        raw message). Connectors with a driver-error taxonomy override this to
        name the failed check ``authentication`` and attach typed
        ``FailureDetails``.
        """
        return None

    def _is_stub_credentials(self, creds: dict[str, Any]) -> bool:
        """True when no connection-identity field is present anywhere."""
        extra = creds.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        return not any(
            creds.get(field) or extra.get(field)
            for field in self.connection_identity_fields
        )
