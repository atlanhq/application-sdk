"""Unit tests for the zstd payload compression codec."""

from __future__ import annotations

import pytest
import zstandard
from temporalio.api.common.v1 import Payload

from application_sdk.execution._temporal.codec import ZSTD_ENCODING, ZstdPayloadCodec

_LARGE = b"x" * 8192
_SMALL = b"tiny"


def _payload(data: bytes, encoding: bytes = b"json/plain") -> Payload:
    return Payload(metadata={"encoding": encoding}, data=data)


@pytest.fixture
def enabled_codec(monkeypatch: pytest.MonkeyPatch) -> ZstdPayloadCodec:
    monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION", "zstd")
    return ZstdPayloadCodec()


@pytest.fixture
def disabled_codec(monkeypatch: pytest.MonkeyPatch) -> ZstdPayloadCodec:
    monkeypatch.delenv("ATLAN_PAYLOAD_COMPRESSION", raising=False)
    return ZstdPayloadCodec()


class TestRoundTrip:
    """encode -> decode reproduces the original payloads exactly."""

    @pytest.mark.asyncio
    async def test_round_trip_restores_original_payload(
        self, enabled_codec: ZstdPayloadCodec
    ) -> None:
        original = _payload(_LARGE)
        encoded = await enabled_codec.encode([original])
        decoded = await enabled_codec.decode(encoded)
        assert decoded == [original]

    @pytest.mark.asyncio
    async def test_round_trip_preserves_metadata(
        self, enabled_codec: ZstdPayloadCodec
    ) -> None:
        original = Payload(
            metadata={"encoding": b"json/plain", "custom-key": b"custom-value"},
            data=_LARGE,
        )
        encoded = await enabled_codec.encode([original])
        decoded = await enabled_codec.decode(encoded)
        assert decoded[0].metadata["encoding"] == b"json/plain"
        assert decoded[0].metadata["custom-key"] == b"custom-value"
        assert decoded[0].data == _LARGE

    @pytest.mark.asyncio
    async def test_encoded_payload_is_marked_and_smaller(
        self, enabled_codec: ZstdPayloadCodec
    ) -> None:
        encoded = await enabled_codec.encode([_payload(_LARGE)])
        assert encoded[0].metadata["encoding"] == ZSTD_ENCODING
        assert len(encoded[0].data) < len(_LARGE)


class TestMinBytesThreshold:
    """Payloads under the minimum size pass through unencoded."""

    @pytest.mark.asyncio
    async def test_small_payload_passes_through(
        self, enabled_codec: ZstdPayloadCodec
    ) -> None:
        original = _payload(_SMALL)
        encoded = await enabled_codec.encode([original])
        assert encoded == [original]

    @pytest.mark.asyncio
    async def test_payload_at_threshold_is_encoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION", "zstd")
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION_MIN_BYTES", "10")
        codec = ZstdPayloadCodec()
        encoded = await codec.encode([_payload(b"0123456789")])
        assert encoded[0].metadata["encoding"] == ZSTD_ENCODING

    @pytest.mark.asyncio
    async def test_payload_below_custom_threshold_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION", "zstd")
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION_MIN_BYTES", "10")
        codec = ZstdPayloadCodec()
        original = _payload(b"123456789")
        encoded = await codec.encode([original])
        assert encoded == [original]


class TestEncodeGating:
    """Encoding is gated by ATLAN_PAYLOAD_COMPRESSION; decoding never is."""

    @pytest.mark.asyncio
    async def test_encode_is_noop_when_disabled(
        self, disabled_codec: ZstdPayloadCodec
    ) -> None:
        original = _payload(_LARGE)
        encoded = await disabled_codec.encode([original])
        assert encoded == [original]

    @pytest.mark.asyncio
    async def test_encode_is_noop_for_unrecognized_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION", "gzip")
        codec = ZstdPayloadCodec()
        original = _payload(_LARGE)
        encoded = await codec.encode([original])
        assert encoded == [original]

    @pytest.mark.asyncio
    async def test_disabled_codec_still_decodes_zstd_payloads(
        self, enabled_codec: ZstdPayloadCodec, disabled_codec: ZstdPayloadCodec
    ) -> None:
        original = _payload(_LARGE)
        encoded = await enabled_codec.encode([original])
        assert encoded[0].metadata["encoding"] == ZSTD_ENCODING
        decoded = await disabled_codec.decode(encoded)
        assert decoded == [original]


class TestDecodePassthrough:
    """Non-zstd payloads pass through decode untouched."""

    @pytest.mark.asyncio
    async def test_plain_payload_passes_through_decode(
        self, disabled_codec: ZstdPayloadCodec
    ) -> None:
        original = _payload(_LARGE)
        decoded = await disabled_codec.decode([original])
        assert decoded == [original]

    @pytest.mark.asyncio
    async def test_payload_without_encoding_metadata_passes_through(
        self, disabled_codec: ZstdPayloadCodec
    ) -> None:
        original = Payload(data=_SMALL)
        decoded = await disabled_codec.decode([original])
        assert decoded == [original]


class TestMixedBatchOrdering:
    """Ordering is preserved across mixed encode/decode batches."""

    @pytest.mark.asyncio
    async def test_encode_preserves_order_in_mixed_batch(
        self, enabled_codec: ZstdPayloadCodec
    ) -> None:
        large_a = _payload(b"a" * 8192)
        small = _payload(_SMALL)
        large_b = _payload(b"b" * 8192)
        encoded = await enabled_codec.encode([large_a, small, large_b])
        assert len(encoded) == 3
        assert encoded[0].metadata["encoding"] == ZSTD_ENCODING
        assert encoded[1] == small
        assert encoded[2].metadata["encoding"] == ZSTD_ENCODING
        decoded = await enabled_codec.decode(encoded)
        assert decoded == [large_a, small, large_b]

    @pytest.mark.asyncio
    async def test_decode_preserves_order_with_interleaved_plain_payloads(
        self, enabled_codec: ZstdPayloadCodec
    ) -> None:
        large = _payload(b"c" * 8192)
        plain = _payload(b"plain-data")
        (encoded_large,) = await enabled_codec.encode([large])
        decoded = await enabled_codec.decode([plain, encoded_large, plain])
        assert decoded == [plain, large, plain]


class TestCompressionLevel:
    """ATLAN_PAYLOAD_COMPRESSION_LEVEL controls the zstd level."""

    @pytest.mark.asyncio
    async def test_custom_level_round_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION", "zstd")
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION_LEVEL", "19")
        codec = ZstdPayloadCodec()
        original = _payload(_LARGE)
        encoded = await codec.encode([original])
        assert encoded[0].metadata["encoding"] == ZSTD_ENCODING
        decoded = await codec.decode(encoded)
        assert decoded == [original]

    @pytest.mark.asyncio
    async def test_invalid_level_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION", "zstd")
        monkeypatch.setenv("ATLAN_PAYLOAD_COMPRESSION_LEVEL", "not-a-number")
        codec = ZstdPayloadCodec()
        encoded = await codec.encode([_payload(_LARGE)])
        assert encoded[0].metadata["encoding"] == ZSTD_ENCODING


class TestDecodeCompatibility:
    """Decode handles any zstd frame, independent of who compressed it."""

    @pytest.mark.asyncio
    async def test_decodes_payload_compressed_externally(
        self, disabled_codec: ZstdPayloadCodec
    ) -> None:
        original = _payload(_LARGE)
        wrapped = Payload(
            metadata={"encoding": ZSTD_ENCODING},
            data=zstandard.ZstdCompressor(level=10).compress(
                original.SerializeToString()
            ),
        )
        decoded = await disabled_codec.decode([wrapped])
        assert decoded == [original]


class TestConverterWiring:
    """The SDK data converters carry the codec for clients and workers."""

    def test_default_converter_has_codec(self) -> None:
        from application_sdk.execution._temporal.converter import create_data_converter

        converter = create_data_converter()
        assert isinstance(converter.payload_codec, ZstdPayloadCodec)

    def test_converter_with_additional_converters_has_codec(self) -> None:
        from temporalio.converter import (
            CompositePayloadConverter,
            DefaultPayloadConverter,
        )

        from application_sdk.execution._temporal.converter import create_data_converter

        extra = [
            c
            for c in DefaultPayloadConverter().converters.values()
            if c.encoding == "json/plain"
        ]
        converter = create_data_converter(additional_converters=extra)
        assert isinstance(converter.payload_codec, ZstdPayloadCodec)
        assert isinstance(converter.payload_converter, CompositePayloadConverter)

    def test_app_converter_has_codec(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.contracts.base import Input, Output
        from application_sdk.execution._temporal.converter import (
            create_data_converter_for_app,
        )

        class _CodecInput(Input):
            name: str = ""

        class _CodecOutput(Output):
            result: str = ""

        class _CodecApp(App):
            async def run(self, input: _CodecInput) -> _CodecOutput:
                return _CodecOutput()

        converter = create_data_converter_for_app(_CodecApp)
        assert isinstance(converter.payload_codec, ZstdPayloadCodec)
