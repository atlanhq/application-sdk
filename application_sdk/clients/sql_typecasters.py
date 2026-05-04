"""Tolerant UTF-8 text decoders for SQL DBAPI cursors.

Long-lived warehouses accumulate query text from heterogeneous client encodings
(SSMS, Excel, Word paste, terminals, etc.). When the SDK pulls that text via a
strict UTF-8 cursor decoder, a single non-UTF-8 byte (commonly ``0x96`` —
Windows-1252 en-dash) raises ``UnicodeDecodeError`` and aborts the entire
batch, breaking query-history miners (Redshift, Postgres, etc.).

This module installs a *tolerant* UTF-8 decoder
(``bytes.decode("utf-8", errors="replace")``) on the DBAPI connection so the
99% of well-encoded bytes round-trip correctly and the rare genuinely-broken
byte becomes a visible replacement character (``�``) instead of crashing
the run.

Important: we deliberately do *not* change ``client_encoding`` to ``latin1``.
That stops the crash but mojibakes valid UTF-8 (``0xC3 0xA9`` → ``Ã©`` instead
of ``é``) — a strictly worse data-fidelity trade.

The two supported drivers (psycopg2 and psycopg / psycopg3) have different
adapter APIs, so we dispatch on the DBAPI connection's module at runtime.
``BaseSQLClient`` wires this in via a SQLAlchemy ``connect`` event so every
connector that goes through the SDK's engine inherits the fix.

Tracking: WARE-970 (production stack trace: WARE-837 on Mercury Redshift).

References
----------
- psycopg2 typecasters: https://www.psycopg.org/docs/extensions.html#psycopg2.extensions.new_type
- psycopg3 loaders: https://www.psycopg.org/psycopg3/docs/advanced/adapt.html
"""

from __future__ import annotations

from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# psycopg3 text-ish PostgreSQL OIDs we want to override. These match the OIDs
# psycopg3 itself registers ``TextLoader``/``TextBinaryLoader`` for: text(25),
# varchar(1043), bpchar(1042), name(19), char(18), unknown(705).
# Sourced from ``psycopg.postgres.types`` for the canonical names.
_PSYCOPG3_TEXT_OIDS: tuple[int, ...] = (18, 19, 25, 705, 1042, 1043)


def _decode_tolerant_utf8(data: Any) -> str:
    """Decode a bytes-like value as UTF-8, replacing invalid bytes with ``�``.

    Accepts ``bytes``, ``bytearray``, or ``memoryview`` so it can be plugged
    into either psycopg2 or psycopg3 callbacks without further conversion.
    """
    if data is None:
        return None  # type: ignore[return-value]  # mirrors driver semantics for SQL NULL
    if isinstance(data, memoryview):
        data = bytes(data)
    elif isinstance(data, bytearray):
        data = bytes(data)
    return data.decode("utf-8", errors="replace")


def _attach_psycopg2(dbapi_connection: Any) -> bool:
    """Register a tolerant UNICODE/UNICODEARRAY typecaster on a psycopg2 connection.

    Returns True on success, False if the driver isn't available.
    """
    try:
        import psycopg2.extensions as ext  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return False

    def _cast(value: Any, _cur: Any) -> Any:
        if value is None:
            return None
        # psycopg2 hands us either a str (already decoded by libpq+client_encoding)
        # or bytes (e.g. when client_encoding is SQL_ASCII). For the str path the
        # damage may already have been done at the libpq layer, but for the bytes
        # path this is exactly where we want to intervene.
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    tolerant_unicode = ext.new_type(ext.UNICODE.values, "TOLERANT_UNICODE", _cast)
    tolerant_unicode_array = ext.new_array_type(
        ext.UNICODEARRAY.values, "TOLERANT_UNICODEARRAY", tolerant_unicode
    )
    # Scope to this connection only — global registration would mutate state for
    # every other psycopg2 user in the process.
    ext.register_type(tolerant_unicode, dbapi_connection)
    ext.register_type(tolerant_unicode_array, dbapi_connection)
    return True


def _attach_psycopg3(dbapi_connection: Any) -> bool:
    """Register a tolerant text Loader on a psycopg (v3) connection.

    Returns True on success, False if the driver isn't available.
    """
    try:
        from psycopg.adapt import (  # type: ignore[import-not-found]  # noqa: PLC0415
            Loader,
        )
    except ImportError:
        return False

    class _TolerantTextLoader(Loader):
        def load(self, data: Any) -> str:
            return _decode_tolerant_utf8(data)

    adapters = getattr(dbapi_connection, "adapters", None)
    if adapters is None:
        return False
    for oid in _PSYCOPG3_TEXT_OIDS:
        adapters.register_loader(oid, _TolerantTextLoader)
    return True


def install_tolerant_text_decoder_hook(engine: Any) -> None:
    """Wire :func:`attach_tolerant_text_decoder` into a SQLAlchemy engine.

    Registers a ``connect`` event listener on ``engine`` (sync or
    ``AsyncEngine.sync_engine``) so every newly-checked-out DBAPI connection
    gets a tolerant UTF-8 text decoder installed. This is the canonical
    integration point for ``BaseSQLClient`` / ``AsyncBaseSQLClient``.

    Designed to be a single, easily-monkeypatchable seam so unit tests that
    mock ``create_engine`` to return a ``MagicMock`` aren't forced to also mock
    SQLAlchemy's event registry.

    Args:
        engine: A SQLAlchemy ``Engine`` (for sync clients) or the
            ``sync_engine`` exposed by ``AsyncEngine`` (for async clients).
    """
    from sqlalchemy import event  # noqa: PLC0415 — optional dep: sqlalchemy
    from sqlalchemy.exc import (  # noqa: PLC0415 — optional dep: sqlalchemy
        InvalidRequestError,
    )

    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        attach_tolerant_text_decoder(dbapi_connection)

    try:
        event.listen(engine, "connect", _on_connect)
    except InvalidRequestError:
        # The engine target doesn't support the ``connect`` event — typically a
        # MagicMock in unit tests. Worst case is fall-back to the driver's
        # default strict decoder, which is the pre-WARE-970 behavior.
        logger.debug(
            "Skipping tolerant-decoder hook: engine %r doesn't expose 'connect' event",
            engine,
        )


def attach_tolerant_text_decoder(dbapi_connection: Any) -> bool:
    """Attach a tolerant UTF-8 text decoder to a DBAPI connection.

    Detects whether the connection is backed by psycopg2 or psycopg3 by
    inspecting its class' module name and dispatches to the matching
    registration helper. No-ops (returning False) on any other driver — by
    design, since this is a psycopg-family fix.

    Args:
        dbapi_connection: The raw DBAPI connection handed to the SQLAlchemy
            ``connect`` event listener (i.e. ``dbapi_connection`` arg, not the
            wrapping ``ConnectionRecord``).

    Returns:
        True if a decoder was attached, False otherwise.
    """
    module = type(dbapi_connection).__module__ or ""
    try:
        if module.startswith("psycopg2"):
            return _attach_psycopg2(dbapi_connection)
        if module.startswith("psycopg.") or module == "psycopg":
            return _attach_psycopg3(dbapi_connection)
    except Exception:  # pragma: no cover — defensive: never fail load() over this
        logger.warning(
            "Failed to attach tolerant UTF-8 decoder to %s; falling back to driver default",
            module,
            exc_info=True,
        )
        return False
    return False
