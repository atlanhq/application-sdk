"""The tolerant UTF-8 decoder, which was a silent no-op on psycopg2.

One malformed byte from a source kills the whole batch (CONAT-767). The
carve-out re-implemented the psycopg2 half as a per-connection typecaster —
a seam psycopg2 never reaches: `typecast.c::typecast_cast` calls `conn_decode`
first, so the bytes are already decoded (or already raised) before any Python
typecaster runs. So the hook registered cleanly, reported success, and did
nothing.

redshift imports and calls this symbol, and reports its client encoding as
UNICODE rather than UTF8 — exactly the path that goes through `pydecoder`.
"""

from __future__ import annotations

import codecs
import importlib.util

import pytest
from server_sdk.clients.typecasters import (
    _decode_tolerant_utf8,
    _ensure_tolerant_codec,
    _TOLERANT_CODEC_NAME,
    install_tolerant_connection_decoder,
)

BAD = b"ok-\xff-bytes"


# ── the codec itself ────────────────────────────────────────────────────────


def test_strict_utf8_really_does_raise_on_these_bytes() -> None:
    """The premise. If this ever stops raising, the rest is testing nothing."""
    with pytest.raises(UnicodeDecodeError):
        codecs.utf_8_decode(BAD, "strict", True)


def test_the_tolerant_codec_replaces_instead_of_raising() -> None:
    _ensure_tolerant_codec()
    decode = codecs.lookup(_TOLERANT_CODEC_NAME).decode
    assert decode(BAD)[0] == "ok-�-bytes"


def test_valid_text_is_untouched() -> None:
    _ensure_tolerant_codec()
    decode = codecs.lookup(_TOLERANT_CODEC_NAME).decode
    assert decode("héllo".encode())[0] == "héllo"


def test_the_encoder_stays_stock_utf8() -> None:
    """psycopg2 resolves BOTH directions from one codec name, so a non-UTF-8
    encoder here would corrupt outgoing SQL."""
    _ensure_tolerant_codec()
    assert codecs.lookup(_TOLERANT_CODEC_NAME).encode("héllo")[0] == "héllo".encode()


@pytest.mark.parametrize("value", [b"x", bytearray(b"x"), memoryview(b"x")])
def test_every_bytes_like_shape_decodes(value) -> None:
    assert _decode_tolerant_utf8(value) == "x"


def test_none_passes_through() -> None:
    """SQL NULL — mirrors driver semantics."""
    assert _decode_tolerant_utf8(None) is None


# ── the psycopg2 connection seam ────────────────────────────────────────────

# Scoped to these two tests ONLY. A module-level importorskip would skip the
# codec tests above as well, and a suite that skips silently reports agreement
# it never measured — which is how the no-op this file exists for survived.
requires_psycopg2 = pytest.mark.skipif(
    importlib.util.find_spec("psycopg2") is None,
    reason="the psycopg2 connection seam needs the driver installed",
)


@requires_psycopg2
def test_the_connection_encodings_map_is_rewritten_in_place() -> None:
    """It must be mutated in place: the C layer holds the same dict object, so
    rebinding psycopg2.extensions.encodings would do nothing."""
    import psycopg2.extensions as ext

    before = dict(ext.encodings)
    try:
        assert install_tolerant_connection_decoder() is True
        utf8_names = {
            pg for pg, py in before.items() if "utf" in str(py).lower()
        }
        assert utf8_names, "psycopg2 should ship at least one utf-8 mapping"
        for pg in utf8_names:
            assert ext.encodings[pg] == _TOLERANT_CODEC_NAME, pg
        # Redshift's own spelling, which is why this path matters.
        if "UNICODE" in before:
            assert ext.encodings["UNICODE"] == _TOLERANT_CODEC_NAME
    finally:
        ext.encodings.clear()
        ext.encodings.update(before)


@requires_psycopg2
def test_non_utf8_mappings_are_left_alone() -> None:
    import psycopg2.extensions as ext

    before = dict(ext.encodings)
    try:
        install_tolerant_connection_decoder()
        for pg, py in before.items():
            if "utf" not in str(py).lower():
                assert ext.encodings[pg] == py, pg
    finally:
        ext.encodings.clear()
        ext.encodings.update(before)
