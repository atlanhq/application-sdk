"""Tolerant UTF-8 text decoders for psycopg-family DBAPI cursors.

A single non-UTF-8 byte in returned text becomes ``�`` instead of crashing the
batch (WARE-970). Connectors that build their engine directly (e.g. an IAM auth
path that bypasses ``BaseSQLClient.load``) call
:func:`install_tolerant_text_decoder_hook` to keep behavior identical. Part of
the ``[sql]`` surface — psycopg is imported lazily, so importing this module has
no hard driver dependency.
"""

from __future__ import annotations

from typing import Any

from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_PSYCOPG3_TEXT_OIDS: tuple[int, ...] = (0, 18, 19, 25, 705, 1042, 1043)


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

    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        attach_tolerant_text_decoder(dbapi_connection)

    try:
        event.listen(engine, "connect", _on_connect)
    except InvalidRequestError:
        logger.debug(
            "Skipping tolerant-decoder hook: engine %r has no 'connect' event", engine
        )
