"""Unit tests for tolerant UTF-8 SQL text decoders (WARE-970).

These tests do not spin up a real PostgreSQL/Redshift instance. They mock the
DBAPI surface (psycopg2.extensions / psycopg.adapt.Loader) just enough to
verify the registration mechanics and the decoder's tolerant behavior on the
exact byte that triggered WARE-837 in production: ``0x96`` (Windows-1252
en-dash).
"""

from __future__ import annotations

import codecs
import importlib.util
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from application_sdk.clients.sql_typecasters import (
    _TOLERANT_CODEC_NAME,
    _decode_tolerant_utf8,
    attach_tolerant_text_decoder,
    install_tolerant_connection_decoder,
    install_tolerant_text_decoder_hook,
)

_HAS_PSYCOPG3 = importlib.util.find_spec("psycopg") is not None


class TestDecodeTolerantUtf8:
    """Direct tests of the decoder primitive."""

    def test_decodes_ascii_unchanged(self) -> None:
        assert _decode_tolerant_utf8(b"SELECT 1") == "SELECT 1"

    def test_decodes_valid_utf8_correctly(self) -> None:
        # 0xC3 0xA9 is UTF-8 for 'é'. The latin1 workaround would mojibake
        # this to 'Ã©'; tolerant UTF-8 must preserve it.
        assert _decode_tolerant_utf8(b"caf\xc3\xa9") == "café"

    def test_decodes_modern_en_dash_correctly(self) -> None:
        # 0xE2 0x80 0x93 is UTF-8 for '–' (U+2013 en-dash).
        assert _decode_tolerant_utf8(b"a \xe2\x80\x93 b") == "a – b"

    def test_replaces_lone_0x96_with_replacement_char(self) -> None:
        # 0x96 is the Windows-1252 en-dash. In strict UTF-8 it raises
        # UnicodeDecodeError — the exact production failure in WARE-837.
        # With errors='replace' we get U+FFFD instead.
        result = _decode_tolerant_utf8(b"a \x96 b")
        assert result == "a � b"
        assert "�" in result

    def test_accepts_bytearray(self) -> None:
        assert _decode_tolerant_utf8(bytearray(b"hello")) == "hello"

    def test_accepts_memoryview(self) -> None:
        assert _decode_tolerant_utf8(memoryview(b"hello")) == "hello"

    def test_passes_through_none(self) -> None:
        assert _decode_tolerant_utf8(None) is None


class TestAttachPsycopg2:
    """Verify the psycopg2 path registers tolerant typecasters on the connection."""

    def _install_fake_psycopg2(
        self,
    ) -> tuple[MagicMock, types.ModuleType, types.ModuleType]:
        """Install a fake ``psycopg2.extensions`` module in sys.modules.

        Mirrors enough of the real API for ``_attach_psycopg2`` to import and
        call: ``UNICODE.values``, ``UNICODEARRAY.values``, ``new_type``,
        ``new_array_type``, ``register_type``.
        """
        fake_pkg = types.ModuleType("psycopg2")
        fake_ext = types.ModuleType("psycopg2.extensions")

        fake_ext.UNICODE = MagicMock(values=(25, 1042, 1043))
        fake_ext.UNICODEARRAY = MagicMock(values=(1009, 1014, 1015))
        fake_ext.new_type = MagicMock(
            side_effect=lambda oids, name, cb: ("type", name, cb)
        )
        fake_ext.new_array_type = MagicMock(
            side_effect=lambda oids, name, base: ("array_type", name, base)
        )
        fake_ext.register_type = MagicMock()

        fake_pkg.extensions = fake_ext  # type: ignore[attr-defined]
        sys.modules["psycopg2"] = fake_pkg
        sys.modules["psycopg2.extensions"] = fake_ext

        return fake_ext.register_type, fake_pkg, fake_ext

    def teardown_method(self) -> None:
        sys.modules.pop("psycopg2", None)
        sys.modules.pop("psycopg2.extensions", None)

    def test_registers_tolerant_unicode_and_array_types_on_connection(self) -> None:
        register_mock, _, fake_ext = self._install_fake_psycopg2()

        # Build a fake connection whose class' module starts with 'psycopg2'
        ConnClass = type("connection", (), {})
        ConnClass.__module__ = "psycopg2.extensions"
        conn = ConnClass()

        attached = attach_tolerant_text_decoder(conn)
        assert attached is True

        # Both the scalar and array tolerant types must be registered, scoped
        # to *this* connection (second arg = conn).
        assert register_mock.call_count == 2
        for call in register_mock.call_args_list:
            args, _kwargs = call
            assert args[1] is conn

        # And the names should make their purpose grep-able in stack dumps.
        new_type_calls = fake_ext.new_type.call_args_list
        assert any(c.args[1] == "TOLERANT_UNICODE" for c in new_type_calls)

    def test_callback_replaces_invalid_byte_in_bytes_payload(self) -> None:
        _, _, fake_ext = self._install_fake_psycopg2()

        ConnClass = type("connection", (), {})
        ConnClass.__module__ = "psycopg2.extensions"
        conn = ConnClass()

        attach_tolerant_text_decoder(conn)

        # Capture the callback handed to new_type for UNICODE
        cb = next(
            c.args[2]
            for c in fake_ext.new_type.call_args_list
            if c.args[1] == "TOLERANT_UNICODE"
        )

        # Bytes path: the WARE-837 byte must not raise and must yield U+FFFD.
        assert cb(b"a \x96 b", MagicMock()) == "a � b"
        # str path: should pass through unchanged.
        assert cb("café", MagicMock()) == "café"
        # NULL passthrough.
        assert cb(None, MagicMock()) is None


@pytest.mark.skipif(
    not _HAS_PSYCOPG3,
    reason="psycopg3 not installed in this interpreter (e.g. no Python 3.14 wheel yet)",
)
class TestAttachPsycopg3:
    """Verify the psycopg3 path registers a tolerant Loader on the connection."""

    def test_registers_loader_for_text_oids(self) -> None:
        # psycopg is already a SDK dep; use the real Loader base class.
        from psycopg.adapt import Loader

        fake_adapters = MagicMock()
        ConnClass = type("Connection", (), {})
        ConnClass.__module__ = "psycopg.connection"
        conn = ConnClass()
        conn.adapters = fake_adapters  # type: ignore[attr-defined]

        attached = attach_tolerant_text_decoder(conn)
        assert attached is True

        # Loaders must be registered for all the text-ish OIDs we care about.
        registered_oids = [
            c.args[0] for c in fake_adapters.register_loader.call_args_list
        ]
        # Hardcode the canonical psycopg postgres OIDs; if these change upstream
        # we want a loud test failure. OID 0 is ``InvalidOid`` — see the module
        # docstring for the PgBouncer transaction-pooling rationale.
        for oid in (0, 18, 19, 25, 705, 1042, 1043):
            assert oid in registered_oids, f"missing loader for OID {oid}"

        # Every registered class must subclass Loader.
        for c in fake_adapters.register_loader.call_args_list:
            cls = c.args[1]
            assert issubclass(cls, Loader)

    def test_loader_decodes_tolerantly(self) -> None:
        fake_adapters = MagicMock()
        ConnClass = type("Connection", (), {})
        ConnClass.__module__ = "psycopg.connection"
        conn = ConnClass()
        conn.adapters = fake_adapters  # type: ignore[attr-defined]

        attach_tolerant_text_decoder(conn)
        loader_cls = fake_adapters.register_loader.call_args_list[0].args[1]

        # Loader.__init__ wants (oid, context); context can be None.
        loader = loader_cls(25, None)
        assert loader.load(b"a \x96 b") == "a � b"
        assert loader.load(b"caf\xc3\xa9") == "café"


class TestAttachTolerantTextDecoderDispatch:
    """High-level dispatcher behavior."""

    def test_returns_false_for_unknown_driver(self) -> None:
        # e.g. duckdb, sqlite — out of scope for this fix, must no-op.
        ConnClass = type("Connection", (), {})
        ConnClass.__module__ = "duckdb"
        conn = ConnClass()
        assert attach_tolerant_text_decoder(conn) is False

    def test_returns_false_when_psycopg3_connection_lacks_adapters(self) -> None:
        ConnClass = type("Connection", (), {})
        ConnClass.__module__ = "psycopg.connection"
        conn = ConnClass()
        # No `.adapters` attribute → defensive False.
        assert attach_tolerant_text_decoder(conn) is False

    @pytest.mark.parametrize(
        "module_name",
        ["psycopg2", "psycopg2.extensions", "psycopg2.pool"],
    )
    def test_dispatches_psycopg2_module_variants(
        self, module_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: dict[str, Any] = {}

        def fake_attach_psycopg2(conn: Any) -> bool:
            called["conn"] = conn
            return True

        monkeypatch.setattr(
            "application_sdk.clients.sql_typecasters._attach_psycopg2",
            fake_attach_psycopg2,
        )

        ConnClass = type("connection", (), {})
        ConnClass.__module__ = module_name
        conn = ConnClass()
        assert attach_tolerant_text_decoder(conn) is True
        assert called["conn"] is conn


class TestConnectionLevelDecoder:
    """The connection decoder, not the typecaster (CONAT-767).

    ``_attach_psycopg2`` registers a Python typecaster over the text OIDs, but
    psycopg2 strict-decodes the wire bytes to ``str`` in ``conn_decode`` *before*
    calling any Python typecaster::

        /* typecast.c :: typecast_cast */
        else if (self->pcast) {
            s = conn_decode(((cursorObject *)curs)->conn, str, len);
            res = PyObject_CallFunctionObjArgs(self->pcast, s, curs, NULL);

    so the typecaster never sees a malformed byte — ``conn_decode`` has already
    raised. These tests pin the seam that actually carries the production
    failure: a Redshift miner run died with ``'utf-8' codec can't decode byte
    0x96 in position 184: invalid start byte`` while the typecaster fix was
    installed and active.
    """

    # Redshift reports its client encoding as UNICODE rather than UTF8, so it
    # takes psycopg2's ``pydecoder`` path instead of the strict C fast codec.
    _REDSHIFT_PG_ENCODING = "UNICODE"

    # A query-history row shaped like the failing one: valid UTF-8 on both sides
    # of a lone Windows-1252 en-dash (0x96).
    _RAW = b"SELECT * FROM caf\xc3\xa9 WHERE note = 'Q1 \x96 Q2'"

    def _install_fake_psycopg2(self) -> types.ModuleType:
        """Install a fake ``psycopg2.extensions`` carrying the real encodings map."""
        fake_pkg = types.ModuleType("psycopg2")
        fake_ext = types.ModuleType("psycopg2.extensions")
        # The entries that matter, spelled as psycopg2 ships them.
        fake_ext.encodings = {  # type: ignore[attr-defined]
            "UNICODE": "utf_8",
            "UTF8": "utf_8",
            "LATIN1": "latin_1",
            "SQL_ASCII": "ascii",
        }
        fake_pkg.extensions = fake_ext  # type: ignore[attr-defined]
        sys.modules["psycopg2"] = fake_pkg
        sys.modules["psycopg2.extensions"] = fake_ext
        return fake_ext

    def teardown_method(self) -> None:
        sys.modules.pop("psycopg2", None)
        sys.modules.pop("psycopg2.extensions", None)

    @staticmethod
    def _conn_decode(encodings: dict[str, str], pg_encoding: str, raw: bytes) -> str:
        """Mirror psycopg2's ``conn_decode`` pydecoder branch.

        ``conn_decode`` resolves the codec from the encodings map and calls its
        decoder with the bytes as the *only* argument, so the codec's own default
        error handling decides whether a bad byte crashes the fetch.
        """
        decoder = codecs.getdecoder(encodings[pg_encoding])
        return decoder(raw)[0]

    def test_stock_mapping_raises_on_the_production_byte(self) -> None:
        """Premise guard: unpatched, this is exactly the production crash."""
        fake_ext = self._install_fake_psycopg2()
        with pytest.raises(UnicodeDecodeError) as exc:
            self._conn_decode(
                fake_ext.encodings,  # type: ignore[attr-defined]
                self._REDSHIFT_PG_ENCODING,
                self._RAW,
            )
        assert "0x96" in str(exc.value)

    def test_public_hook_makes_redshift_decode_tolerant(self) -> None:
        """The regression: the public entry point must fix the decode path.

        Fails before the fix — the hook only registered the (unreachable)
        typecaster and left the connection decoder strict.
        """
        fake_ext = self._install_fake_psycopg2()

        install_tolerant_text_decoder_hook(MagicMock())

        decoded = self._conn_decode(
            fake_ext.encodings,  # type: ignore[attr-defined]
            self._REDSHIFT_PG_ENCODING,
            self._RAW,
        )
        # The bad byte becomes U+FFFD; the surrounding valid UTF-8 survives.
        assert decoded == "SELECT * FROM café WHERE note = 'Q1 � Q2'"

    def test_installer_reports_and_rewrites_utf8_entries(self) -> None:
        fake_ext = self._install_fake_psycopg2()
        assert install_tolerant_connection_decoder() is True
        assert fake_ext.encodings["UNICODE"] == _TOLERANT_CODEC_NAME  # type: ignore[attr-defined]
        assert fake_ext.encodings["UTF8"] == _TOLERANT_CODEC_NAME  # type: ignore[attr-defined]

    def test_valid_utf8_is_not_mojibaked(self) -> None:
        """The latin1 workaround would yield 'Ã©'; tolerant UTF-8 must keep 'é'."""
        fake_ext = self._install_fake_psycopg2()
        install_tolerant_connection_decoder()
        assert (
            self._conn_decode(
                fake_ext.encodings,  # type: ignore[attr-defined]
                self._REDSHIFT_PG_ENCODING,
                b"caf\xc3\xa9",
            )
            == "café"
        )

    def test_encoder_stays_byte_identical_to_utf8(self) -> None:
        """Outgoing SQL must not change: only the decoder is tolerant."""
        fake_ext = self._install_fake_psycopg2()
        install_tolerant_connection_decoder()
        encoder = codecs.getencoder(fake_ext.encodings["UNICODE"])  # type: ignore[attr-defined]
        assert encoder("café – Q1")[0] == "café – Q1".encode()

    def test_non_utf8_mappings_are_left_alone(self) -> None:
        fake_ext = self._install_fake_psycopg2()
        install_tolerant_connection_decoder()
        assert fake_ext.encodings["LATIN1"] == "latin_1"  # type: ignore[attr-defined]
        assert fake_ext.encodings["SQL_ASCII"] == "ascii"  # type: ignore[attr-defined]

    def test_is_idempotent(self) -> None:
        self._install_fake_psycopg2()
        assert install_tolerant_connection_decoder() is True
        assert install_tolerant_connection_decoder() is False

    def test_no_op_without_psycopg2(self) -> None:
        sys.modules.pop("psycopg2", None)
        sys.modules.pop("psycopg2.extensions", None)
        assert install_tolerant_connection_decoder() is False
