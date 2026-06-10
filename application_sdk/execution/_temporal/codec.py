"""Zstandard payload compression codec for Temporal.

Temporal payloads (workflow args, activity inputs/outputs) are stored
verbatim in workflow history. Uncompressed payloads inflate history
storage, egress, and replay bandwidth. This codec compresses payloads
with zstd using the standard codec composition pattern: the original
``Payload`` protobuf is serialized whole, compressed, and wrapped in a
new ``Payload`` whose metadata marks the encoding as ``binary/zstd``.

Rollout is two-phase by design:

1. **Decode is always on.** Any worker running this SDK can read
   compressed payloads, regardless of configuration.
2. **Encode is gated** by ``ATLAN_PAYLOAD_COMPRESSION=zstd``, flipped
   per-app only after the whole fleet is decode-capable.

See ``docs/adr/0017-payload-compression-codec.md`` for the decision
record and the full rollout choreography.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import zstandard
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

ZSTD_ENCODING = b"binary/zstd"
"""Metadata ``encoding`` value identifying a zstd-wrapped payload."""

_DEFAULT_MIN_BYTES = 4096
_DEFAULT_LEVEL = 3


def _load_compression_enabled() -> bool:
    """Read ``ATLAN_PAYLOAD_COMPRESSION`` (``off`` default | ``zstd``)."""
    return os.environ.get("ATLAN_PAYLOAD_COMPRESSION", "off").strip().lower() == "zstd"


def _load_int_env(env_var: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on unset/invalid."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ZstdPayloadCodec(PayloadCodec):
    """Temporal ``PayloadCodec`` that compresses payloads with zstd.

    Encoding only happens when compression is enabled
    (``ATLAN_PAYLOAD_COMPRESSION=zstd``) and the payload data is at least
    ``ATLAN_PAYLOAD_COMPRESSION_MIN_BYTES`` (default 4096) — small
    payloads aren't worth the wrap overhead. Compression level comes from
    ``ATLAN_PAYLOAD_COMPRESSION_LEVEL`` (default 3).

    Decoding is unconditional: any payload whose metadata ``encoding`` is
    ``binary/zstd`` is decompressed back to the original payload whether
    or not encoding is enabled on this worker. This is what makes the
    two-phase rollout safe — decode-capable workers ship first, the
    encode flag flips after.

    Note: zstd compressor/decompressor contexts are not safe for
    concurrent use, so they are created per call. Construction is cheap
    relative to the compression work on the >= 4 KiB payloads this codec
    targets.
    """

    def __init__(self) -> None:
        """Load codec configuration from environment variables."""
        super().__init__()
        self._enabled = _load_compression_enabled()
        self._min_bytes = _load_int_env(
            "ATLAN_PAYLOAD_COMPRESSION_MIN_BYTES", _DEFAULT_MIN_BYTES
        )
        self._level = _load_int_env("ATLAN_PAYLOAD_COMPRESSION_LEVEL", _DEFAULT_LEVEL)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Compress eligible payloads; pass the rest through untouched.

        A payload is eligible when compression is enabled and its data is
        at least the configured minimum size. The whole original payload
        protobuf (metadata included) is serialized and compressed so that
        decode reconstructs it exactly.
        """
        if not self._enabled:
            return list(payloads)

        compressor = zstandard.ZstdCompressor(level=self._level)
        result: list[Payload] = []
        for payload in payloads:
            if len(payload.data) < self._min_bytes:
                result.append(payload)
                continue
            result.append(
                Payload(
                    metadata={"encoding": ZSTD_ENCODING},
                    data=compressor.compress(payload.SerializeToString()),
                )
            )
        return result

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        """Decompress zstd-wrapped payloads; pass the rest through untouched.

        Always active regardless of the encode flag, so a worker with
        compression off can still read history written by a worker with
        compression on.
        """
        decompressor = zstandard.ZstdDecompressor()
        result: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") != ZSTD_ENCODING:
                result.append(payload)
                continue
            inner = Payload()
            inner.ParseFromString(decompressor.decompress(payload.data))
            result.append(inner)
        return result
