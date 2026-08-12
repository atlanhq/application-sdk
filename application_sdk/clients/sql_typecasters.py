"""Tolerant UTF-8 text decoders for SQL DBAPI cursors.

Long-lived warehouses accumulate query text from heterogeneous client encodings
(SSMS, Excel, Word paste, terminals, etc.). When the SDK pulls that text via a
strict UTF-8 cursor decoder, a single non-UTF-8 byte (commonly ``0x96`` —
Windows-1252 en-dash) raises ``UnicodeDecodeError`` and aborts the entire
batch, breaking query-history miners.

The intended semantics have not changed since WARE-970: decode UTF-8, and where
the bytes are not valid UTF-8 under any reading, emit ``U+FFFD`` rather than
kill the batch. What CONAT-767 changed is only the *injection point* — the
tolerance was registered at a layer psycopg2 never reaches (see
:func:`install_tolerant_connection_decoder`), so production kept crashing while
the code read as fixed. Nothing here infers or guesses an encoding: the wire
encoding is declared by the connection, and ``errors="replace"`` is a total
function over that already-fixed decoding. No correctly-encoded byte can be
dropped.

Scope (psycopg2)
----------------
The psycopg2 fix covers connections whose server reports its client encoding as
``UNICODE`` — Redshift (a PostgreSQL 8.0.2 fork, and PG 8.0 spelled UTF-8
``UNICODE``) and other pre-8.1-era forks. Connections that report literally
``UTF8`` — which is every PostgreSQL from 8.1 onward — take psycopg2's C fast
decoder (``PyUnicode_DecodeUTF8``, strict), which no Python-level hook can
intercept. **The original crash class therefore remains open for plain
PostgreSQL query-history miners on psycopg2.** Closing it would require owning
``client_encoding``, which is a separate decision (see below). psycopg3 is
covered on all servers via its loader API.

Recorded decisions
------------------
``errors="replace"``, not ``surrogateescape``. ``surrogateescape`` would
preserve the original byte and make the loss reversible, which is genuinely
attractive. It is rejected because decoded text flows on to parquet and JSON,
where lone surrogates are not serializable — a deferred hard failure in the
writer is worse than a visible ``U+FFFD`` in the data. ``replace`` is lossy on
purpose, and the log signal below is what makes the loss visible.

Not ``client_encoding=latin1``. That stops the crash but mojibakes valid UTF-8
(``0xC3 0xA9`` → ``Ã©`` instead of ``é``) — a strictly worse fidelity trade.

Not ``client_encoding=SQL_ASCII`` either, though it is the strongest
alternative and deserves an explicit rejection. Under ``SQL_ASCII`` psycopg2
maps to the ``ascii`` codec, so per-value decoding could move into a typecaster:
that would give per-value control, natural logging, and would close the ``UTF8``
gap above uniformly. The cost is that the SDK would then own *outgoing* encoding
for every non-ASCII identifier and parameter on every connector that inherits
this, on connections it did not configure — a much larger blast radius than the
defect being fixed, and one that fails in the write direction rather than the
read direction. Rejected on that basis, not on merit.

Observability
-------------
The replacement path logs a rate-limited WARNING with the offending byte, its
offset, and a redacted excerpt (identifiers and literals masked — no query text
reaches the logs). One stray byte in a large query and a column that was
Windows-1252 all along are both silent in the data; the log line is what
distinguishes them, and what makes "did this run take the tolerant path?"
answerable after the fact instead of inferred.

Tracking: CONAT-767 (injection point), WARE-970 (original intent).

References
----------
- psycopg2 typecasters: https://www.psycopg.org/docs/extensions.html#psycopg2.extensions.new_type
- psycopg3 loaders: https://www.psycopg.org/psycopg3/docs/advanced/adapt.html
"""

from __future__ import annotations

import codecs
import threading
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Name of the private codec registered by ``_ensure_tolerant_codec``. It is an
# ordinary UTF-8 codec whose *decoder* replaces invalid bytes instead of
# raising; the encoder is stock strict UTF-8, so outgoing SQL is unchanged.
_TOLERANT_CODEC_NAME = "atlan_tolerant_utf8"

# Bytes shown either side of the offending byte in the redacted log excerpt.
_EXCERPT_WINDOW = 8

# Cumulative count of *values* that needed replacement in this process, and the
# lock guarding it. Only touched on the (rare, by construction) failure path.
_replacement_total = 0
_replacement_lock = threading.Lock()


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


def _redacted_excerpt(data: bytes, offset: int) -> str:
    """Render the bytes around ``offset`` without leaking query text.

    ASCII letters collapse to ``a``/``A`` and digits to ``0``, so identifiers,
    literals and any customer-identifying value are destroyed while the *shape*
    survives. Punctuation is structural and kept. Only the bracketed offending
    byte is rendered as its exact ``\\xNN`` value — it is already unrecoverable
    from the decoded text, so revealing it leaks nothing.

    Every *other* non-ASCII byte renders as the fixed token ``\\x??``: a valid
    UTF-8 multi-byte sequence next to the bad byte is customer content (``é`` in
    a name), so its value must not reach the log — but the *count* of high bytes
    is kept, because that is the tell for a wholesale Windows-1252 column: one
    lone ``[\\x96]`` in masked ASCII is a stray byte, whereas a run of ``\\x??``
    neighbours means the column was never UTF-8.
    """
    start = max(0, offset - _EXCERPT_WINDOW)
    end = min(len(data), offset + _EXCERPT_WINDOW + 1)

    rendered: list[str] = []
    for index in range(start, end):
        byte = data[index]
        if index == offset:
            rendered.append(f"[\\x{byte:02x}]")
        elif 0x41 <= byte <= 0x5A:  # A-Z
            rendered.append("A")
        elif 0x61 <= byte <= 0x7A:  # a-z
            rendered.append("a")
        elif 0x30 <= byte <= 0x39:  # 0-9
            rendered.append("0")
        elif byte in (0x09, 0x0A, 0x0D, 0x20):  # whitespace
            rendered.append(" ")
        elif byte < 0x80:  # ASCII punctuation — structural, not content
            rendered.append(chr(byte))
        else:  # non-ASCII neighbour — value is content; only the count matters
            rendered.append("\\x??")

    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(data) else ""
    return f"{prefix}{''.join(rendered)}{suffix}"


def _note_replacement(data: bytes, error: UnicodeDecodeError, decoded: str) -> None:
    """Emit a rate-limited WARNING that the tolerant decoder dropped a byte.

    Rate limiting is per *process* on powers of two (1st, 2nd, 4th, 8th, …
    value): loud on first occurrence, so a single stray byte is never silent,
    and log-bounded when an entire column turns out to be Windows-1252. Per
    *connection* would be the nicer granularity but is not reachable here —
    psycopg2 resolves the codec by name from a module-level dict and calls the
    decoder with the bytes as the sole argument, so no connection identity
    reaches this frame.
    """
    global _replacement_total

    with _replacement_lock:
        _replacement_total += 1
        total = _replacement_total

    if total & (total - 1):  # not a power of two — stay quiet
        return

    logger.warning(
        "Tolerant UTF-8 decoder replaced invalid byte 0x%02x at offset %d of a "
        "%d-byte value with U+FFFD (%d replacement char(s) in this value; %d "
        "value(s) affected in this process so far). The source is not valid "
        "UTF-8 and the original byte is unrecoverable. Redacted context: %s",
        data[error.start],
        error.start,
        len(data),
        decoded.count("�"),
        total,
        _redacted_excerpt(data, error.start),
    )


def _tolerant_utf8_decode(data: Any, errors: str = "strict") -> tuple[str, int]:
    """Codec-protocol decoder that never raises on malformed UTF-8.

    ``errors`` is accepted for codec-protocol compatibility and deliberately
    ignored: psycopg2 invokes the connection decoder with the bytes as the only
    argument, so honouring the default ``"strict"`` is exactly the crash we are
    here to prevent.

    Decoding is attempted strictly first. That keeps the hot path byte-for-byte
    the cost of the previous strict decoder, and it is what yields the offset of
    the offending byte for :func:`_note_replacement` — the tolerant re-decode
    only runs on the rare value that actually needs it.
    """
    if isinstance(data, (memoryview, bytearray)):
        data = bytes(data)
    try:
        return codecs.utf_8_decode(data, "strict", True)
    except UnicodeDecodeError as error:
        result = codecs.utf_8_decode(data, "replace", True)
        _note_replacement(data, error, result[0])
        return result  # conformance: ignore[E007] _note_replacement emits the rate-limited WARNING for this exception before we return


# Stock strict UTF-8 encoder, tolerant decoder. psycopg2 resolves *both* the
# encoder and the decoder from the same codec name, so the encoder must stay
# byte-identical to UTF-8 or we would corrupt outgoing SQL.
_TOLERANT_CODEC_INFO = codecs.CodecInfo(
    name=_TOLERANT_CODEC_NAME,
    encode=codecs.utf_8_encode,
    decode=_tolerant_utf8_decode,
)


def _decode_tolerant_utf8(data: Any) -> str:
    """Decode a bytes-like value as UTF-8, replacing invalid bytes with ``�``.

    Accepts ``bytes``, ``bytearray``, or ``memoryview`` so it can be plugged
    into either psycopg2 or psycopg3 callbacks without further conversion.
    Shares :func:`_tolerant_utf8_decode`, so the psycopg3 path emits the same
    rate-limited WARNING on the replacement path as the psycopg2 path does.
    """
    if data is None:
        return None  # type: ignore[return-value]  # mirrors driver semantics for SQL NULL
    return _tolerant_utf8_decode(data)[0]


def _ensure_tolerant_codec() -> None:
    """Register :data:`_TOLERANT_CODEC_NAME` with the codecs registry, once.

    ``codecs.register`` cannot be undone, so the search function is installed at
    most once per process and only answers to our private codec name.
    """
    try:
        codecs.lookup(_TOLERANT_CODEC_NAME)
        return
    except LookupError:  # conformance: ignore[E002] existence probe: LookupError *is* the "not registered yet" answer, and registering is the next statement
        pass

    def _search(name: str) -> codecs.CodecInfo | None:
        return _TOLERANT_CODEC_INFO if name == _TOLERANT_CODEC_NAME else None

    codecs.register(_search)


def install_tolerant_connection_decoder() -> bool:
    """Make psycopg2's *connection-level* text decode tolerant.

    Why a typecaster cannot do this. psycopg2 decodes wire bytes to ``str``
    **before** it calls any Python typecaster — unconditionally, for every
    encoding::

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
    seam. It is the very dict the C layer reads (``psycopgmodule.c`` publishes
    its ``psycoEncodings`` as the module's ``encodings`` attribute), so it must
    be mutated **in place**; rebinding ``ext.encodings`` would have no effect.

    Redshift reports its client encoding as ``UNICODE`` (not ``UTF8``), so it
    takes exactly this path. Note the deliberate asymmetry: a connection
    reporting literally ``UTF8`` gets psycopg2's C fast decoder
    (``PyUnicode_DecodeUTF8``, strict) and bypasses ``pydecoder`` entirely, so
    remapping is a no-op there and plain PostgreSQL ≥ 8.1 stays uncovered — see
    the module docstring.

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

    # This rewrite is process-global and there is no per-connection alternative:
    # ``pydecoder`` is not settable from Python, and psycopg2 resolves it from
    # this one dict. Within a connector worker — a single-purpose process whose
    # only psycopg2 user is the SDK — that is the intended scope, and it is
    # strictly more available than the status quo: valid UTF-8 is unchanged, and
    # malformed bytes surface as a logged U+FFFD instead of killing the batch.
    rewritten = False
    for pg_name, py_name in list(encodings.items()):
        if py_name == _TOLERANT_CODEC_NAME:
            continue
        # Name prefilter before ``codecs.lookup``: psycopg2 ships 92 entries and
        # looking each one up would import every codec in the map (EUC_JP, BIG5,
        # KOI8, …) at install time. Only utf-8 spellings can pass, and the
        # lookup below still decides — this narrows work, not correctness.
        if "utf" not in py_name.lower():
            continue
        try:
            if codecs.lookup(py_name).name != "utf-8":
                continue
        except (
            LookupError,
            TypeError,
        ):  # conformance: ignore[E014] an unresolvable or non-string codec name is definitionally not utf-8, which is the only question asked here
            continue
        encodings[pg_name] = _TOLERANT_CODEC_NAME
        rewritten = True

    if rewritten:
        logger.debug(
            "Installed tolerant UTF-8 connection decoder for psycopg2 encodings"
        )
    return rewritten


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

    Only psycopg3 needs per-connection work. psycopg2 is handled once per
    process by :func:`install_tolerant_connection_decoder`, because psycopg2
    decodes wire bytes to ``str`` before dispatching to any Python typecaster —
    a per-connection typecaster over the text OIDs is unreachable for malformed
    input by construction, so this returns False for psycopg2 rather than
    registering one.

    No-ops (returning False) on any non-psycopg driver — by design, since this
    is a psycopg-family fix.

    Args:
        dbapi_connection: The raw DBAPI connection handed to the SQLAlchemy
            ``connect`` event listener (i.e. ``dbapi_connection`` arg, not the
            wrapping ``ConnectionRecord``).

    Returns:
        True if a decoder was attached, False otherwise.
    """
    module = type(dbapi_connection).__module__ or ""
    try:
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
