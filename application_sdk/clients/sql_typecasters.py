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

import codecs
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Name of the private codec registered by ``_ensure_tolerant_codec``. It is an
# ordinary UTF-8 codec whose *decoder* replaces invalid bytes instead of
# raising; the encoder is stock strict UTF-8, so outgoing SQL is unchanged.
_TOLERANT_CODEC_NAME = "atlan_tolerant_utf8"


# psycopg3 text-ish PostgreSQL OIDs we want to override. These match the OIDs
# psycopg3 itself registers ``TextLoader``/``TextBinaryLoader`` for: text(25),
# varchar(1043), bpchar(1042), name(19), char(18), unknown(705).
# Sourced from ``psycopg.postgres.types`` for the canonical names.
#
# We also register for OID 0 (``InvalidOid``). Some Postgres-compatible proxies
# — notably PgBouncer in transaction-pooling mode, Azure Flex Server's built-in
# pooler, and AWS RDS Proxy — can return OID 0 on prepared-statement metadata
# (e.g. ``pg_catalog.version()``), which psycopg3 otherwise hands to the caller
# as raw bytes. SQLAlchemy's Postgres dialect then runs a string regex against
# those bytes in ``_get_server_version_info`` and raises ``TypeError: cannot
# use a string pattern on a bytes-like object`` before any query can run.
_PSYCOPG3_TEXT_OIDS: tuple[int, ...] = (0, 18, 19, 25, 705, 1042, 1043)


def _decode_tolerant_utf8(data: Any) -> str:
    """Decode a bytes-like value as UTF-8, replacing invalid bytes with ``�``.

    Accepts ``bytes``, ``bytearray``, or ``memoryview`` so it can be plugged
    into either psycopg2 or psycopg3 callbacks without further conversion.
    """
    if data is None:
        return None  # type: ignore[return-value]  # mirrors driver semantics for SQL NULL
    if isinstance(data, memoryview) or isinstance(data, bytearray):
        data = bytes(data)
    return data.decode("utf-8", errors="replace")


def _tolerant_utf8_decode(data: Any, errors: str = "strict") -> tuple[str, int]:
    """Codec-protocol decoder that never raises on malformed UTF-8.

    ``errors`` is accepted for codec-protocol compatibility and deliberately
    ignored: psycopg2 invokes the connection decoder with the bytes as the only
    argument, so honouring the default ``"strict"`` is exactly the crash we are
    here to prevent.
    """
    if isinstance(data, (memoryview, bytearray)):
        data = bytes(data)
    return codecs.utf_8_decode(data, "replace", True)


# Stock strict UTF-8 encoder, tolerant decoder. psycopg2 resolves *both* the
# encoder and the decoder from the same codec name, so the encoder must stay
# byte-identical to UTF-8 or we would corrupt outgoing SQL.
_TOLERANT_CODEC_INFO = codecs.CodecInfo(
    name=_TOLERANT_CODEC_NAME,
    encode=codecs.utf_8_encode,
    decode=_tolerant_utf8_decode,
)


def _ensure_tolerant_codec() -> None:
    """Register :data:`_TOLERANT_CODEC_NAME` with the codecs registry, once.

    ``codecs.register`` cannot be undone, so the search function is installed at
    most once per process and only answers to our private codec name.
    """
    try:
        codecs.lookup(_TOLERANT_CODEC_NAME)
        return
    except LookupError:
        pass

    def _search(name: str) -> codecs.CodecInfo | None:
        return _TOLERANT_CODEC_INFO if name == _TOLERANT_CODEC_NAME else None

    codecs.register(_search)


def install_tolerant_connection_decoder() -> bool:
    """Make psycopg2's *connection-level* text decode tolerant.

    Why this is needed on top of :func:`_attach_psycopg2`. psycopg2 decodes wire
    bytes to ``str`` **before** it calls any Python typecaster::

        /* typecast.c :: typecast_cast */
        else if (self->pcast) {
            s = conn_decode(((cursorObject *)curs)->conn, str, len);
            res = PyObject_CallFunctionObjArgs(self->pcast, s, curs, NULL);

    so a Python typecaster registered over the text OIDs can never see an
    invalid byte — ``conn_decode`` has already raised ``UnicodeDecodeError``.
    psycopg2's own source says as much: "it is about impossible to create a
    python typecaster on a binary type."

    ``conn_decode`` uses the connection's ``pydecoder`` whenever no C fast codec
    applies, and calls it with the bytes as the sole argument (i.e. strict).
    ``pydecoder`` is resolved at connect time from
    ``psycopg2.extensions.encodings``, mapping the server-reported encoding name
    to a Python codec name — which makes that dict the only Python-reachable
    seam. Redshift reports its client encoding as ``UNICODE`` (not ``UTF8``), so
    it takes exactly this path.

    Note the deliberate asymmetry: a connection reporting literally ``UTF8``
    gets psycopg2's C fast decoder (``PyUnicode_DecodeUTF8``, strict) and
    bypasses ``pydecoder`` entirely, so remapping is a harmless no-op there. No
    Python-level hook can intervene in that case.

    Must run *before* connections are opened; a per-connection ``connect``
    listener is already too late, because ``pydecoder`` is bound during connect.

    Returns:
        True if at least one mapping was rewritten, False if psycopg2 is absent
        or every UTF-8 entry was already tolerant.
    """
    try:
        import psycopg2.extensions as ext  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:  # conformance: ignore[E008] optional dep psycopg2 not installed; driver-detection probe
        return False  # conformance: ignore[E007] driver-detection probe; ImportError means optional dep absent, not an error

    encodings = getattr(ext, "encodings", None)
    if not isinstance(encodings, dict):
        return False

    _ensure_tolerant_codec()

    rewritten = False
    for pg_name, py_name in list(encodings.items()):
        if py_name == _TOLERANT_CODEC_NAME:
            continue
        try:
            if codecs.lookup(py_name).name != "utf-8":
                continue
        except (LookupError, TypeError):
            continue
        encodings[pg_name] = _TOLERANT_CODEC_NAME
        rewritten = True

    if rewritten:
        logger.debug(
            "Installed tolerant UTF-8 connection decoder for psycopg2 encodings"
        )
    return rewritten


def _attach_psycopg2(dbapi_connection: Any) -> bool:
    """Register a tolerant UNICODE/UNICODEARRAY typecaster on a psycopg2 connection.

    Returns True on success, False if the driver isn't available.
    """
    try:
        import psycopg2.extensions as ext  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:  # conformance: ignore[E008] optional dep psycopg2 not installed; driver-detection probe
        return False  # conformance: ignore[E007] driver-detection probe; ImportError means optional dep absent, not an error

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
    except ImportError:  # conformance: ignore[E008] optional dep psycopg not installed; driver-detection probe
        return False  # conformance: ignore[E007] driver-detection probe; ImportError means optional dep absent, not an error

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

    # Runs here, not in the listener below: psycopg2 binds the connection's
    # decoder while connecting, so by the time the ``connect`` event fires the
    # strict decoder is already in place for that connection.
    install_tolerant_connection_decoder()

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
