"""Tolerant UTF-8 text decoders for psycopg-family DBAPI cursors.

A single non-UTF-8 byte in returned text becomes ``�`` instead of crashing the
batch (WARE-970). Connectors that build their engine directly (e.g. an IAM auth
path that bypasses ``BaseSQLClient.load``) call
:func:`install_tolerant_text_decoder_hook` to keep behavior identical. Part of
the ``[sql]`` surface — psycopg is imported lazily, so importing this module has
no hard driver dependency.
"""

from __future__ import annotations

import codecs
from typing import Any

from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_PSYCOPG3_TEXT_OIDS: tuple[int, ...] = (0, 18, 19, 25, 705, 1042, 1043)


#: Private codec name. psycopg2 resolves BOTH the encoder and decoder from one
#: name, so the encoder stays stock UTF-8 or outgoing SQL would be corrupted.
_TOLERANT_CODEC_NAME = "atlan_tolerant_utf8"


def _tolerant_utf8_decode(data: Any, errors: str = "strict") -> tuple[str, int]:
    """Codec-protocol decoder that never raises on malformed UTF-8.

    ``errors`` is accepted for protocol compatibility and deliberately ignored:
    psycopg2 calls the connection decoder with the bytes as the only argument,
    so honouring the default "strict" is exactly the crash this prevents.

    Strict first, so the hot path costs what the strict decoder cost and the
    tolerant re-decode only runs on a value that actually needs it.
    """
    if isinstance(data, (memoryview, bytearray)):
        data = bytes(data)
    try:
        return codecs.utf_8_decode(data, "strict", True)
    except UnicodeDecodeError:
        logger.warning(
            "Replacing malformed UTF-8 bytes from the source with U+FFFD "
            "(%d bytes); the value is returned rather than failing the batch.",
            len(data),
        )
        return codecs.utf_8_decode(data, "replace", True)


_TOLERANT_CODEC_INFO = codecs.CodecInfo(
    name=_TOLERANT_CODEC_NAME,
    encode=codecs.utf_8_encode,
    decode=_tolerant_utf8_decode,
)


def _ensure_tolerant_codec() -> None:
    """Register the codec once. ``codecs.register`` cannot be undone, so the
    search function answers only to our private name."""
    try:
        codecs.lookup(_TOLERANT_CODEC_NAME)
        return
    except LookupError:
        # conformance: ignore[E002] existence probe: LookupError IS the
        # "not registered yet" answer, and registering is the next statement.
        pass

    def _search(name: str) -> codecs.CodecInfo | None:
        return _TOLERANT_CODEC_INFO if name == _TOLERANT_CODEC_NAME else None

    codecs.register(_search)


def install_tolerant_connection_decoder() -> bool:
    """Make psycopg2's *connection-level* text decode tolerant.

    A Python typecaster cannot do this, which is why the previous
    implementation was a silent no-op. psycopg2 decodes wire bytes to ``str``
    BEFORE dispatching to any typecaster (``typecast.c::typecast_cast`` calls
    ``conn_decode`` first), so a typecaster over the text OIDs never sees an
    invalid byte -- ``conn_decode`` has already raised.

    ``conn_decode`` uses the connection's ``pydecoder``, resolved at connect
    time from ``psycopg2.extensions.encodings``. That dict is the only
    Python-reachable seam, and the C layer reads the same object, so it must be
    mutated IN PLACE -- rebinding ``ext.encodings`` does nothing.

    Redshift reports its client encoding as ``UNICODE`` rather than ``UTF8``, so
    it takes exactly this path; a connection reporting literally ``UTF8`` gets
    psycopg2's C fast decoder and bypasses ``pydecoder`` entirely.

    Must run BEFORE connections are opened -- a per-connection ``connect``
    listener is already too late, because ``pydecoder`` is bound during connect.

    Returns:
        True if at least one mapping was rewritten.
    """
    try:
        import psycopg2.extensions as ext  # noqa: PLC0415 — optional driver
    except ImportError:
        # conformance: ignore[E008] optional dep probe: psycopg2 absent simply
        # means this driver is not in use.
        return False

    encodings = getattr(ext, "encodings", None)
    if not isinstance(encodings, dict):
        return False

    _ensure_tolerant_codec()

    rewritten = False
    for pg_name, py_name in list(encodings.items()):
        if py_name == _TOLERANT_CODEC_NAME:
            continue
        # Name prefilter before codecs.lookup: psycopg2 ships ~92 entries and
        # looking each up would import every codec in the map at install time.
        if "utf" not in str(py_name).lower():
            continue
        try:
            if codecs.lookup(py_name).name != "utf-8":
                continue
        except (LookupError, TypeError):
            # conformance: ignore[E014] an unresolvable or non-string codec name
            # is definitionally not utf-8, the only question asked here.
            continue
        encodings[pg_name] = _TOLERANT_CODEC_NAME
        rewritten = True

    if rewritten:
        logger.debug("Installed tolerant UTF-8 connection decoder for psycopg2")
    return rewritten


def _decode_tolerant_utf8(data: Any) -> str:
    if data is None:
        return None  # type: ignore[return-value]
    if isinstance(data, (memoryview, bytearray)):
        data = bytes(data)
    return data.decode("utf-8", errors="replace")


def _attach_psycopg2(dbapi_connection: Any) -> bool:
    try:
        import psycopg2.extensions as ext  # noqa: PLC0415
    except ImportError:
        return False

    def _cast(value: Any, _cur: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    tolerant_unicode = ext.new_type(ext.UNICODE.values, "TOLERANT_UNICODE", _cast)
    tolerant_unicode_array = ext.new_array_type(
        ext.UNICODEARRAY.values, "TOLERANT_UNICODEARRAY", tolerant_unicode
    )
    ext.register_type(tolerant_unicode, dbapi_connection)
    ext.register_type(tolerant_unicode_array, dbapi_connection)
    return True


def _attach_psycopg3(dbapi_connection: Any) -> bool:
    try:
        from psycopg.adapt import Loader  # noqa: PLC0415
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


def attach_tolerant_text_decoder(dbapi_connection: Any) -> bool:
    module = type(dbapi_connection).__module__ or ""
    try:
        if module.startswith("psycopg2"):
            return _attach_psycopg2(dbapi_connection)
        if module.startswith("psycopg.") or module == "psycopg":
            return _attach_psycopg3(dbapi_connection)
    except Exception:  # noqa: BLE001 — never fail load() over this
        logger.warning(
            "Failed to attach tolerant UTF-8 decoder to %s; using driver default",
            module,
            exc_info=True,
        )
        return False
    return False


def install_tolerant_text_decoder_hook(engine: Any) -> None:
    """Register a SQLAlchemy ``connect`` listener that installs the tolerant decoder."""
    from sqlalchemy import event  # noqa: PLC0415 — optional dep: [sql]
    from sqlalchemy.exc import InvalidRequestError  # noqa: PLC0415

    # Process-global and BEFORE the listener: psycopg2 binds its pydecoder
    # during connect, so a per-connection hook cannot reach it. Without this the
    # psycopg2 half of the decoder is a silent no-op and one malformed byte
    # kills the batch (CONAT-767).
    install_tolerant_connection_decoder()

    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        attach_tolerant_text_decoder(dbapi_connection)

    try:
        event.listen(engine, "connect", _on_connect)
    except InvalidRequestError:
        logger.debug(
            "Skipping tolerant-decoder hook: engine %r has no 'connect' event", engine
        )
