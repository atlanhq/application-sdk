"""BaseSQLClient — the serving-path SQLAlchemy wrapper.

Builds an engine from a ``DatabaseConfig`` template + credentials (basic auth),
runs small queries (``SELECT 1``, schema-filter rows) yielding dict batches, and
closes.

Deliberately scoped to the serving path — server-side-cursor streaming,
``read_only_transaction`` snapshot pinning, IAM/role assumption, tolerant
decoder hooks, and pandas/dataframe paths are worker-side concerns and out of
scope here. SQLAlchemy is imported lazily so the base install stays free of it
(install ``atlan-server-sdk[sql]``).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

from server_sdk.clients.models import DatabaseConfig
from server_sdk.credentials.utils import parse_credentials_extra
from server_sdk.errors.leaves import InternalError, InvalidInputError
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class BaseSQLClient:
    """SQLAlchemy-backed client for the auth/preflight/metadata path.

    Subclasses set :attr:`DB_CONFIG`. Basic-auth ``load()`` covers the common
    case; connectors needing IAM/role auth override ``load()`` — that logic is
    worker-adjacent and out of scope here.
    """

    DB_CONFIG: Optional[DatabaseConfig] = None

    def __init__(self, use_server_side_cursor: bool = True, **kwargs: Any) -> None:
        self.use_server_side_cursor = use_server_side_cursor
        self.credentials: dict[str, Any] = {}
        self.engine: Any = None

    # -- connection string -------------------------------------------------

    def get_sqlalchemy_connection_string(self) -> str:
        """Format ``DB_CONFIG.template`` with credentials (+ defaults), URL-encoding values."""
        if self.DB_CONFIG is None:
            raise InternalError(
                message="DB_CONFIG is not set on this client.",
                component=type(self).__name__,
                invariant="db_config_present",
            )
        # Connector-specific fields (database, warehouse, role, account…) arrive
        # nested under ``extra``: that is how the setup form submits them and how
        # heracles forwards them on ``/workflows/v1/auth``. Resolve every key
        # top-level-first then ``extra`` — the same precedence the worker-side
        # clients use — so ``required`` and the template see what the caller
        # actually sent. Without this a connector declaring ``database`` as
        # required rejects a perfectly valid credential with "Missing required
        # credential field(s): database" while ``extra["database"]`` holds it.
        merged: dict[str, Any] = {**self.DB_CONFIG.defaults, **self.credentials}
        for key, value in parse_credentials_extra(self.credentials).items():
            if not merged.get(key):
                merged[key] = value
        missing = [k for k in self.DB_CONFIG.required if not merged.get(k)]
        if missing:
            raise InvalidInputError(
                message=f"Missing required credential field(s): {', '.join(missing)}",
                field=missing[0],
                constraint="required",
            )
        # userinfo encoding: SQLAlchemy decodes the URL's user/password with a
        # percent-only unquote, so ``quote_plus`` (space -> ``+``) corrupts
        # passwords containing spaces (CONNECT-361). ``quote(safe="")`` encodes
        # space as ``%20`` and also encodes ``/``, which round-trips correctly.
        encoded = {
            k: quote(str(v), safe="") if k in ("username", "password") else v
            for k, v in merged.items()
        }
        try:
            return self.DB_CONFIG.template.format(**encoded)
        except KeyError as exc:  # missing placeholder
            raise InvalidInputError(
                message=f"Missing credential for connection template: {exc}",
                field=str(exc).strip("'"),
                constraint="required",
            ) from exc

    # -- lifecycle ---------------------------------------------------------

    async def load(self, credentials: dict[str, Any]) -> None:
        """Create the SQLAlchemy engine and eagerly validate connectivity."""
        self.credentials = credentials or {}
        from sqlalchemy import create_engine  # noqa: PLC0415 — optional dep: [sql]

        connect_args = (self.DB_CONFIG.connect_args if self.DB_CONFIG else None) or {}
        self.engine = create_engine(
            self.get_sqlalchemy_connection_string(), connect_args=connect_args
        )

    async def run_query(
        self, query: str, batch_size: int = 100000
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Execute ``query`` and yield rows as dict batches.

        Runs execute+drain on a single worker thread (SQLAlchemy Result cursors
        are thread-affine), keeping the async-yield contract the handler expects.
        """
        if self.engine is None:
            raise InternalError(
                message="Engine is not initialized. Call load() first.",
                component=type(self).__name__,
                invariant="engine_initialized_before_run_query",
            )
        from sqlalchemy import text  # noqa: PLC0415 — optional dep: [sql]

        def _execute_and_drain() -> tuple[list[str], list[Any]]:
            with self.engine.connect() as connection:
                result = connection.execute(text(query))
                columns = list(result.keys())
                rows = result.fetchall()
            return columns, list(rows)

        loop = asyncio.get_running_loop()
        columns, all_rows = await loop.run_in_executor(None, _execute_and_drain)
        for i in range(0, len(all_rows), batch_size):
            chunk = all_rows[i : i + batch_size]
            yield [dict(zip(columns, row)) for row in chunk]

    async def close(self) -> None:
        """Dispose the engine's connection pool."""
        if self.engine is not None:
            engine, self.engine = self.engine, None
            try:
                await asyncio.get_running_loop().run_in_executor(None, engine.dispose)
            except Exception as exc:  # noqa: BLE001
                logger.warning("engine dispose failed: %s", exc, exc_info=True)
