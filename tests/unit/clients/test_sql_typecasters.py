"""Unit tests for tolerant UTF-8 SQL text decoders (CONAT-767, WARE-970).

These tests do not spin up a real PostgreSQL/Redshift instance. They mock the
DBAPI surface (psycopg2.extensions / psycopg.adapt.Loader) just enough to
verify the registration mechanics and the decoder's tolerant behavior on the
exact byte that triggered the production failure: ``0x96`` (Windows-1252
en-dash).

Mocking the driver is what let the previous fix ship green while production kept
failing: the tests asserted the decoder primitive and that ``register_type`` was
called, never the path psycopg2 actually takes. Two guards against repeating
that here:

* :class:`TestConnectionLevelDecoder` drives the *public* hook and then decodes
  exactly as ``conn_decode`` does, so it fails on the production frame against
  pre-fix source.
* :class:`TestRealPsycopg2Premises` pins the two facts the fake stands in for
  against the real driver, so a psycopg2 upgrade that moves the seam is loud
  rather than silent.
"""

from __future__ import annotations

import codecs
import importlib.util
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.clients import sql_typecasters
from application_sdk.clients.sql_typecasters import (
    _TOLERANT_CODEC_NAME,
    _decode_tolerant_utf8,
    _redacted_excerpt,
    _tolerant_utf8_decode,
    attach_tolerant_text_decoder,
    install_tolerant_connection_decoder,
    install_tolerant_text_decoder_hook,
)

_HAS_PSYCOPG3 = importlib.util.find_spec("psycopg") is not None
_HAS_PSYCOPG2 = importlib.util.find_spec("psycopg2") is not None

# Bind the real driver modules once, at collection time. Tests below install
# fake ``psycopg2`` modules in ``sys.modules`` and pop them again, which leaves
# the real package importable but not necessarily re-bound to its C submodule —
# so ``TestRealPsycopg2Premises`` must not re-import inside a test body.
if _HAS_PSYCOPG2:
    import psycopg2._psycopg as _real_psycopg2_c
    import psycopg2.extensions as _real_psycopg2_ext
else:  # pragma: no cover — psycopg2 is a test dep; only missing without a wheel
    _real_psycopg2_c = None  # type: ignore[assignment]  # — sentinel for "driver not installed": None is not a module
    _real_psycopg2_ext = None  # type: ignore[assignment]  # — sentinel for "driver not installed": None is not a module


@pytest.fixture(autouse=True)
def _reset_replacement_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-global replacement counter between tests.

    The counter drives log rate-limiting, so leaking it across tests would make
    the observability assertions order-dependent.
    """
    monkeypatch.setattr(sql_typecasters, "_replacement_total", 0)


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
        # UnicodeDecodeError — the exact production failure this module exists
        # for. With errors='replace' we get U+FFFD instead.
        result = _decode_tolerant_utf8(b"a \x96 b")
        assert result == "a � b"
        assert "�" in result

    def test_accepts_bytearray(self) -> None:
        assert _decode_tolerant_utf8(bytearray(b"hello")) == "hello"

    def test_accepts_memoryview(self) -> None:
        assert _decode_tolerant_utf8(memoryview(b"hello")) == "hello"

    def test_passes_through_none(self) -> None:
        assert _decode_tolerant_utf8(None) is None

    def test_codec_protocol_returns_consumed_length(self) -> None:
        # The codec protocol contract: (decoded, bytes_consumed).
        decoded, consumed = _tolerant_utf8_decode(b"a \x96 b")
        assert decoded == "a � b"
        assert consumed == 5

    def test_preexisting_replacement_char_is_not_treated_as_a_failure(self) -> None:
        # U+FFFD legitimately encoded as EF BF BD is valid UTF-8: it must round
        # trip and must not trip the replacement warning.
        with patch.object(sql_typecasters, "logger") as mock_logger:
            assert _decode_tolerant_utf8(b"\xef\xbf\xbd") == "�"
        mock_logger.warning.assert_not_called()


def _warnings(mock_logger: MagicMock) -> list[str]:
    """Render each ``logger.warning(fmt, *args)`` call the way the sink would."""
    return [call.args[0] % call.args[1:] for call in mock_logger.warning.call_args_list]


class TestReplacementObservability:
    """The lossy path must not be silent (CONAT-767 review, blocking)."""

    def test_first_replacement_logs_warning_with_byte_and_offset(self) -> None:
        with patch.object(sql_typecasters, "logger") as mock_logger:
            _decode_tolerant_utf8(b"SELECT * FROM t WHERE c = 'Q1 \x96 Q2'")

        messages = _warnings(mock_logger)
        assert len(messages) == 1
        assert "0x96" in messages[0]  # which byte
        assert " at offset 30 " in messages[0]  # where in the value
        assert "[\\x96]" in messages[0]  # and it is visible in the excerpt
        assert "1 replacement char(s) in this value" in messages[0]

    def test_message_body_carries_the_context_not_kwargs(self) -> None:
        """%-style body, no structured kwargs — the SDK logging convention."""
        with patch.object(sql_typecasters, "logger") as mock_logger:
            _decode_tolerant_utf8(b"a \x96 b")

        call = mock_logger.warning.call_args_list[0]
        assert call.kwargs == {}
        assert call.args[0].count("%") == 6

    def test_valid_utf8_logs_nothing(self) -> None:
        with patch.object(sql_typecasters, "logger") as mock_logger:
            for _ in range(50):
                _decode_tolerant_utf8(b"SELECT * FROM caf\xc3\xa9")
        mock_logger.warning.assert_not_called()

    def test_repeated_replacements_are_rate_limited(self) -> None:
        # A wholesale Windows-1252 column must be visible without flooding the
        # log: powers of two only, so 100 bad values yield 1,2,4,8,16,32,64.
        with patch.object(sql_typecasters, "logger") as mock_logger:
            for _ in range(100):
                _decode_tolerant_utf8(b"a \x96 b")
        assert mock_logger.warning.call_count == 7

    def test_cumulative_count_is_reported(self) -> None:
        with patch.object(sql_typecasters, "logger") as mock_logger:
            for _ in range(4):
                _decode_tolerant_utf8(b"a \x96 b")
        # 1st, 2nd, 4th value logged; the last one names the running total.
        assert (
            "4 value(s) affected in this process so far" in _warnings(mock_logger)[-1]
        )

    def test_psycopg3_loader_path_is_also_observable(self) -> None:
        # The psycopg3 loader shares the primitive, so it must warn too.
        with patch.object(sql_typecasters, "logger") as mock_logger:
            _decode_tolerant_utf8(memoryview(b"a \x96 b"))
        assert mock_logger.warning.call_count == 1

    def test_no_query_text_reaches_the_log(self) -> None:
        with patch.object(sql_typecasters, "logger") as mock_logger:
            _decode_tolerant_utf8(b"SELECT ssn FROM patients WHERE name='Ann\x96e'")

        message = _warnings(mock_logger)[0]
        for leaked in ("ssn", "patients", "Ann", "SELECT"):
            assert leaked not in message

    def test_no_nonascii_query_text_reaches_the_log(self) -> None:
        # 'José' is valid UTF-8 (é = 0xC3 0xA9) sitting next to the bad byte —
        # real PII. Its bytes must be masked, not echoed as \xNN, or the excerpt
        # recovers the name.
        with patch.object(sql_typecasters, "logger") as mock_logger:
            _decode_tolerant_utf8(b"WHERE name='Jos\xc3\xa9 \x96 Q2'")

        message = _warnings(mock_logger)[0]
        assert "\\xc3" not in message
        assert "\\xa9" not in message
        assert "Jos" not in message
        assert "[\\x96]" in message  # the offending byte itself stays visible


class TestRedactedExcerpt:
    """The log excerpt must be diagnostic without leaking query text."""

    def test_masks_identifiers_and_literals(self) -> None:
        raw = b"FROM Orders42 WHERE name = 'Acme\x96Corp'"
        excerpt = _redacted_excerpt(raw, raw.index(b"\x96"))
        # Nothing recoverable: letters collapse to a/A, digits to 0.
        assert "Acme" not in excerpt
        assert "Corp" not in excerpt
        assert "Orders" not in excerpt
        assert "42" not in excerpt

    def test_brackets_the_offending_byte(self) -> None:
        assert "[\\x96]" in _redacted_excerpt(b"ab\x96cd", 2)

    def test_keeps_structural_punctuation(self) -> None:
        # Punctuation distinguishes "inside a string literal" from structure,
        # and carries no customer content.
        excerpt = _redacted_excerpt(b"= 'x\x96y'", 4)
        assert "'" in excerpt

    def test_masks_neighbouring_high_bytes_but_keeps_their_count(self) -> None:
        # Neighbour values are content (a valid UTF-8 sequence next to the bad
        # byte is customer data), so they must not be recoverable — but the
        # *count* of high bytes is the tell for a wholesale Windows-1252 column,
        # so each one still renders as a fixed placeholder.
        excerpt = _redacted_excerpt(b"a\x92b\x96c\x93d", 3)
        assert excerpt == "a\\x??a[\\x96]a\\x??a"
        assert "\\x92" not in excerpt
        assert "\\x93" not in excerpt

    def test_elides_around_a_long_value(self) -> None:
        raw = b"x" * 100 + b"\x96" + b"y" * 100
        excerpt = _redacted_excerpt(raw, 100)
        assert excerpt.startswith("…")
        assert excerpt.endswith("…")
        # Windowed, not the whole value.
        assert len(excerpt) < 60

    def test_handles_offending_byte_at_value_boundaries(self) -> None:
        assert _redacted_excerpt(b"\x96abc", 0) == "[\\x96]aaa"
        assert _redacted_excerpt(b"abc\x96", 3) == "aaa[\\x96]"


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
        conn.adapters = fake_adapters  # type: ignore[attr-defined]  # — attribute injected onto a dynamically-built fake connection class

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
        conn.adapters = fake_adapters  # type: ignore[attr-defined]  # — attribute injected onto a dynamically-built fake connection class

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
    def test_psycopg2_needs_no_per_connection_work(self, module_name: str) -> None:
        """psycopg2 is handled process-wide, not per connection.

        A Python typecaster over the text OIDs is unreachable for malformed
        input (``typecast_cast`` calls ``conn_decode`` first), so the dispatcher
        deliberately registers nothing here — the tolerance comes from
        ``install_tolerant_connection_decoder``.
        """
        ConnClass = type("connection", (), {})
        ConnClass.__module__ = module_name
        conn = ConnClass()
        assert attach_tolerant_text_decoder(conn) is False


class TestConnectionLevelDecoder:
    """The connection decoder, not the typecaster (CONAT-767).

    A Python typecaster over the text OIDs cannot help: psycopg2 strict-decodes
    the wire bytes to ``str`` in ``conn_decode`` *before* calling any Python
    typecaster::

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
        """Install a fake ``psycopg2.extensions`` carrying the real encodings map.

        Values are copied from psycopg2 2.9.12's ``encodings`` dict as observed,
        not from memory — ``LATIN1`` really is ``iso8859_1``, not ``latin_1``.
        :class:`TestRealPsycopg2Premises` pins the ones that matter against the
        installed driver where there is one.
        """
        self._saved_sys_modules = {
            name: sys.modules.get(name) for name in ("psycopg2", "psycopg2.extensions")
        }
        fake_pkg = types.ModuleType("psycopg2")
        fake_ext = types.ModuleType("psycopg2.extensions")
        fake_ext.encodings = {  # type: ignore[attr-defined]  # — attribute injected onto a dynamically-built fake psycopg2 module
            "UNICODE": "utf_8",
            "UTF8": "utf_8",
            "LATIN1": "iso8859_1",
            "LATIN9": "iso8859_15",
            "SQL_ASCII": "ascii",
            "SQLASCII": "ascii",
            "EUC_JP": "euc_jp",
            "BIG5": "big5",
        }
        fake_pkg.extensions = fake_ext  # type: ignore[attr-defined]  # — attribute injected onto a dynamically-built fake psycopg2 module
        sys.modules["psycopg2"] = fake_pkg
        sys.modules["psycopg2.extensions"] = fake_ext
        return fake_ext

    def teardown_method(self) -> None:
        # Restore the prior entries rather than popping: a pop leaves a hole a
        # later ``import psycopg2`` fills with a fresh module object — and a
        # fresh ``encodings`` dict — which would break the dict identity the
        # directory conftest's snapshot/restore depends on.
        for name, original in getattr(self, "_saved_sys_modules", {}).items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

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
                fake_ext.encodings,  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2
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
            fake_ext.encodings,  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2
            self._REDSHIFT_PG_ENCODING,
            self._RAW,
        )
        # The bad byte becomes U+FFFD; the surrounding valid UTF-8 survives.
        assert decoded == "SELECT * FROM café WHERE note = 'Q1 � Q2'"

    def test_installer_reports_and_rewrites_utf8_entries(self) -> None:
        fake_ext = self._install_fake_psycopg2()
        assert install_tolerant_connection_decoder() is True
        assert fake_ext.encodings["UNICODE"] == _TOLERANT_CODEC_NAME  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2
        assert fake_ext.encodings["UTF8"] == _TOLERANT_CODEC_NAME  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2

    def test_valid_utf8_is_not_mojibaked(self) -> None:
        """The latin1 workaround would yield 'Ã©'; tolerant UTF-8 must keep 'é'."""
        fake_ext = self._install_fake_psycopg2()
        install_tolerant_connection_decoder()
        assert (
            self._conn_decode(
                fake_ext.encodings,  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2
                self._REDSHIFT_PG_ENCODING,
                b"caf\xc3\xa9",
            )
            == "café"
        )

    def test_encoder_stays_byte_identical_to_utf8(self) -> None:
        """Outgoing SQL must not change: only the decoder is tolerant."""
        fake_ext = self._install_fake_psycopg2()
        install_tolerant_connection_decoder()
        encoder = codecs.getencoder(fake_ext.encodings["UNICODE"])  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2
        assert encoder("café – Q1")[0] == "café – Q1".encode()

    def test_non_utf8_mappings_are_left_alone(self) -> None:
        fake_ext = self._install_fake_psycopg2()
        install_tolerant_connection_decoder()
        encodings = fake_ext.encodings  # type: ignore[attr-defined]  # — set dynamically on the fake module by _install_fake_psycopg2
        assert encodings["LATIN1"] == "iso8859_1"
        assert encodings["LATIN9"] == "iso8859_15"
        assert encodings["SQL_ASCII"] == "ascii"
        assert encodings["EUC_JP"] == "euc_jp"
        assert encodings["BIG5"] == "big5"

    def test_non_utf8_entries_are_not_looked_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The name prefilter must keep install time off the codec registry.

        psycopg2 ships 92 entries; looking each one up would import every codec
        in the map (EUC_JP, BIG5, KOI8, …) just to install this hook.
        """
        self._install_fake_psycopg2()
        looked_up: list[str] = []
        real_lookup = codecs.lookup

        def spy(name: str) -> Any:
            looked_up.append(name)
            return real_lookup(name)

        monkeypatch.setattr(sql_typecasters.codecs, "lookup", spy)
        install_tolerant_connection_decoder()

        assert "euc_jp" not in looked_up
        assert "big5" not in looked_up
        assert "iso8859_1" not in looked_up
        assert "utf_8" in looked_up

    def test_is_idempotent(self) -> None:
        self._install_fake_psycopg2()
        assert install_tolerant_connection_decoder() is True
        assert install_tolerant_connection_decoder() is False

    def test_no_op_without_psycopg2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``None`` in sys.modules makes the import raise, which is the state we
        # want to assert on regardless of whether psycopg2 is really installed.
        monkeypatch.setitem(sys.modules, "psycopg2", None)
        monkeypatch.setitem(sys.modules, "psycopg2.extensions", None)
        assert install_tolerant_connection_decoder() is False


@pytest.mark.skipif(
    not _HAS_PSYCOPG2,
    reason="psycopg2 not installed (it is a connector-app dependency, not an SDK one)",
)
class TestRealPsycopg2Premises:
    """Pin the two facts the fakes above stand in for, against the real driver.

    Everything else in this file mocks psycopg2, which means it validates the
    SDK's rewrite logic against an *assumption* about the driver — a milder
    version of the failure mode this fix exists for. These two assertions are
    the assumption itself, so a psycopg2 upgrade that renames the map or
    re-creates it as a copy fails loudly here instead of silently reopening the
    original bug.

    psycopg2 is not an ``application-sdk`` runtime dependency — the SDK ships
    psycopg3, and psycopg2 reaches production through connector apps. It is in
    the ``test`` dependency group purely so these three assertions run in CI
    rather than skipping; the ``skipif`` is a safety net for interpreters
    without a wheel, not the expected state.

    These read the module objects bound at collection time, and the directory
    conftest restores ``encodings`` after every test, so neither the fakes above
    nor an engine built in a sibling test file can decide the outcome here.
    """

    def test_encodings_is_the_dict_the_c_layer_reads(self) -> None:
        # ``psycopgmodule.c`` publishes its ``psycoEncodings`` as this attribute
        # and reads it back on every ``conn_set_encoding``. If it ever stops
        # being the same object, the in-place mutation silently stops working.
        assert _real_psycopg2_ext.encodings is _real_psycopg2_c.encodings

    def test_unicode_maps_to_a_utf8_codec(self) -> None:
        # The Redshift path: server reports UNICODE, psycopg2 resolves a Python
        # codec by this name, and our rewrite only fires on utf-8 entries.
        assert codecs.lookup(_real_psycopg2_ext.encodings["UNICODE"]).name == "utf-8"

    def test_rewrite_takes_effect_on_the_real_map(self) -> None:
        encodings = _real_psycopg2_ext.encodings

        assert install_tolerant_connection_decoder() is True
        assert encodings["UNICODE"] == _TOLERANT_CODEC_NAME
        assert encodings["UTF8"] == _TOLERANT_CODEC_NAME
        # Non-utf-8 entries stay untouched, whatever the driver spells them.
        assert codecs.lookup(encodings["LATIN1"]).name == "iso8859-1"
        # And the real map now decodes the production byte without raising.
        decoder = codecs.getdecoder(encodings["UNICODE"])
        assert decoder(b"a \x96 b")[0] == "a � b"
