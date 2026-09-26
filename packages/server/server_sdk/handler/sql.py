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
registration or configuration involved. For preflight specifically the seam is
:meth:`SQLHandler.preflight_tiers`; declaring only the class attributes above
gets you the single ``SELECT 1`` probe, which for a connector whose real
preflight is a multi-tier permission check is a silent downgrade, not a port.

Tiered preflight
----------------

:meth:`SQLHandler.preflight_check` runs the stub short-circuit, then one connect
plus probe (reachability and authentication — tier 1), then hands control to
``preflight_tiers``. The default implementation returns ``None``, meaning "no
further tiers", which yields exactly the single ``connectivity`` row this base
has always returned. Override it to add authorization and advisory tiers::

    class WarehouseServerHandler(SQLHandler):
        client_class = WarehouseServerClient
        filter_metadata_sql = "SELECT catalog_name, schema_name FROM ..."

        async def preflight_tiers(
            self,
            *,
            input: PreflightInput,
            client: BaseSQLClient,
            checks: list[PreflightCheck],
            deadline: float | None,
        ) -> PreflightOutput | None:
            # `checks` already holds the tier-1 `connectivity` row — append to
            # it, do not rebuild it.
            authz = await check_catalog_access(
                client, input.connection_config.get("include-filter")
            )
            checks.append(authz)
            if not authz.passed:
                # A required tier that fails blocks: NOT_READY, carrying the
                # rows gathered so far.
                return PreflightOutput(
                    status=PreflightStatus.NOT_READY, checks=checks
                )
            advisory = await check_table_sample(client, deadline=deadline)
            checks.append(advisory)
            return PreflightOutput(
                status=(
                    PreflightStatus.READY
                    if advisory.passed
                    else PreflightStatus.PARTIAL
                ),
                checks=checks,
            )

What the hook is handed, and why each piece is there:

``input``
    The whole :class:`~server_sdk.handler.contracts.PreflightInput`.
    ``input.connection_config`` is the setup form's own state — the
    include/exclude filters an authorization tier must scope itself by, and the
    reason this hook takes ``input`` rather than just a client.
    ``input.entrypoint`` is what an entrypoint-branching connector switches on
    (e.g. a crawler tier vs. a query-history miner tier); ``input.checks_to_run``
    lets a caller request a subset.
``client``
    The already-connected, already-authenticated tier-1 client. Reusing it is
    not only cheaper: on some sources a connect is a large fraction of the whole
    preflight budget, and some permit only one session-scoped named cursor.
``checks``
    Rows produced so far, tier-1 ``connectivity`` first.
``deadline``
    The absolute ``time.monotonic()`` value the preflight must finish by, or
    ``None`` when the caller sent no ``timeout_seconds``. Derived once from
    ``input.timeout_seconds``, which on the gate path is what is *left* of the
    budget after credential resolution — so fixed per-check constants overrun
    it. Bound every probe by it.

Raising out of the hook is safe: ``preflight_check``'s boundary turns the
exception into a NOT_READY row through :meth:`SQLHandler.classify_exception`,
and still closes the client. Returning ``None`` after appending is safe too —
the rows are kept and the verdict is recomputed from them, so a failing row
still blocks. Returning an explicit ``PreflightOutput`` is what you want
whenever the status is yours to decide (``PARTIAL``, or a short-circuit that
deliberately skips later tiers).

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
from server_sdk.errors.redaction import redact_secrets
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

    Subclasses with a multi-tier permission preflight override
    :meth:`preflight_tiers` — see the module docstring for the full contract and
    a worked example. Class attributes alone give you the ``SELECT 1``
    connectivity probe and nothing else.
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
            # Redact here as well as in AuthOutput: the log line is a separate
            # sink from the wire, and a driver's str() embeds the DSN.
            detail = redact_secrets(str(e))
            logger.warning(
                "%s auth test failed: %s", type(self).__name__, detail, exc_info=True
            )
            return AuthOutput(status=AuthStatus.FAILED, message=detail)
        finally:
            if client:
                await client.close()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Stub short-circuit, then tier 1, then :meth:`preflight_tiers`.

        Overriding this method wholesale is still allowed, but a subclass that
        only needs *more* tiers should override ``preflight_tiers`` instead and
        inherit the stub handling, the single connect, and the error boundary.
        """
        client = None
        started = time.monotonic()
        # Absolute budget for the whole preflight, resolved once. On the gate
        # path timeout_seconds is what remains after credential resolution.
        deadline = started + input.timeout_seconds if input.timeout_seconds else None
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

            # Tier 1 — reachability + authentication in one connect + probe.
            client = await self._build_client(input.credentials)
            async for _ in client.run_query(self.test_authentication_sql):
                pass
            connectivity = PreflightCheck(
                name="connectivity",
                passed=True,
                message="Connected and authenticated",
                duration_ms=(time.monotonic() - started) * 1000,
            )
            checks = [connectivity]
            tiered = await self.preflight_tiers(
                input=input, client=client, checks=checks, deadline=deadline
            )
            if tiered is not None:
                return tiered
            # No further tiers. The verdict comes from the rows actually
            # present, not from "we got here": a subclass that appended a
            # FAILING row and then returned None must not read as READY. With
            # no override `checks` is the single passing connectivity row, so
            # this is READY exactly as it has always been.
            return PreflightOutput(
                status=(
                    PreflightStatus.READY
                    if all(check.passed for check in checks)
                    else PreflightStatus.NOT_READY
                ),
                checks=checks,
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

    async def preflight_tiers(
        self,
        *,
        input: PreflightInput,
        client: BaseSQLClient,
        checks: list[PreflightCheck],
        deadline: float | None,
    ) -> PreflightOutput | None:
        """Add authorization / advisory tiers after tier-1 connectivity passed.

        THE override point for a connector whose preflight is more than one
        ``SELECT 1`` — an authorization tier scoped by the setup form's include
        filter (``input.connection_config``), a tier that branches on
        ``input.entrypoint``, advisory probes, a ``PARTIAL`` verdict.

        Args:
            input: The full request. ``connection_config`` carries the form
                state an authorization tier needs; ``entrypoint`` is the
                branching key; ``checks_to_run`` a caller-requested subset.
            client: Live, authenticated tier-1 client. Reuse it — do not
                connect again.
            checks: Rows so far, ``connectivity`` first. Append to this list.
            deadline: Absolute ``time.monotonic()`` cutoff for the whole
                preflight, or ``None`` if the caller sent no timeout. Bound
                every probe by it.

        Returns:
            The complete :class:`PreflightOutput` — status *and* every row,
            including the ones already in ``checks``. Or ``None`` for "no
            further tiers", in which case the caller emits ``checks`` as they
            stand and derives the status from them (READY only if every row
            passed). Return an explicit output whenever the status is yours to
            decide — ``PARTIAL``, or a short-circuit that skips later tiers.

        Raising is safe: the caller's boundary reports NOT_READY (classified via
        :meth:`classify_exception`) and closes the client. Never return
        ``READY`` for a tier that could not be run — an unrunnable
        authorization check is not a passing one.
        """
        return None

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
