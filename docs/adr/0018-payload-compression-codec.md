# ADR-0018: Zstd Payload Compression Codec

## Status
**Accepted**

## Context

Every Temporal payload — workflow arguments, activity inputs and outputs, query and signal payloads — is written verbatim into workflow history. The SDK configured no `PayloadCodec`, so payloads crossed the wire and landed in history uncompressed. That inflates three cost lines at once:

- **History storage**: Temporal Cloud bills on retained history size.
- **Egress**: every payload transits the Cloudflare-fronted gRPC bridge twice (write and read), and again on every replay.
- **Replay bandwidth**: worker restarts and Worker Deployment migrations re-fetch full history.

The SDK already has an answer for *large* data: `FileReference` (see [ADR-0008](0008-payload-safe-bounded-types.md) on payload-safe bounded types and [ADR-0014](0014-two-store-storage-architecture.md) on the two-store architecture) moves anything that won't fit Temporal's payload cap into object storage and passes a reference. What it doesn't cover is the **mid-size band**: payloads comfortably under the cap (tens of KB to ~2 MB of JSON) that are too small to justify an object-store round trip but large enough to dominate history size. Connector contracts in this band — column lists, typed error envelopes, chunk manifests — are JSON, which compresses extremely well.

A codec change must also be **replay-safe across a heterogeneous fleet**: workers upgrade at different times, and a worker that cannot decode a compressed payload fails the workflow task. Whatever we ship has to tolerate old and new workers reading the same history during rollout.

## Decision

We add a **zstd `PayloadCodec`** (`ZstdPayloadCodec` in `application_sdk/execution/_temporal/codec.py`), attached to every `DataConverter` the SDK builds, with **decode always on and encode gated by an env flag**.

- **Encoding** happens only when `ATLAN_PAYLOAD_COMPRESSION=zstd` *and* the payload data is at least `ATLAN_PAYLOAD_COMPRESSION_MIN_BYTES` (default 4096). The whole original `Payload` protobuf is serialized, compressed at `ATLAN_PAYLOAD_COMPRESSION_LEVEL` (default 3), and wrapped in a new payload with metadata `encoding: binary/zstd` — the standard codec composition pattern, so original metadata survives the round trip exactly.
- **Decoding** is unconditional: any payload marked `binary/zstd` is decompressed and the inner payload restored, regardless of the encode flag. Everything else passes through untouched.

The codec is wired in `create_data_converter()` (`application_sdk/execution/_temporal/converter.py`) via `dataclasses.replace(converter, payload_codec=ZstdPayloadCodec())`, and `create_temporal_client()` falls back to `create_data_converter()` when no converter is supplied. The worker is constructed from the client, so client and worker share one converter — a single wiring point covers both directions.

### Rollout choreography

The decode/encode split makes the rollout a safe two-phase flip:

1. **Ship decode-capable SDK fleet-wide.** Every worker upgrades to an SDK version carrying the codec. Nothing changes on the wire — encoding is still `off` everywhere — but every worker can now read `binary/zstd` payloads.
2. **Flip `ATLAN_PAYLOAD_COMPRESSION=zstd` per-app.** Once all workers that could replay a given app's workflows are decode-capable, enable encoding in that app's deployment. Compressed and uncompressed payloads coexist in history; both decode fine.
3. **Eventual default-on.** After the fleet has been encode-enabled without incident, change the SDK default from `off` to `zstd` and retire the per-app flag.

Flipping encode before step 1 completes would hand compressed payloads to workers that can't read them — the gate exists precisely to make that impossible by configuration rather than by hope.

## Options Considered

### Option 1: No compression (status quo, rejected)

Zero code, zero risk — and the full storage/egress/replay bill. The mid-size payload band keeps growing with connector adoption, so the cost compounds.

### Option 2: gzip (stdlib `zlib`, rejected)

No new dependency, universally understood. But gzip is measurably slower than zstd at comparable ratios and achieves a worse ratio on JSON at the levels where it's fast. Since the codec sits on every payload round trip, the encode/decode CPU delta matters more than avoiding one well-maintained dependency.

### Option 3: zstd (chosen)

Best-in-class ratio/speed trade-off; level 3 compresses JSON to roughly a quarter to a tenth of its size at negligible CPU cost. The `zstandard` package is mature, maintained, and ships wheels for all our platforms. Compressor/decompressor contexts are not thread-safe under concurrent use, so the codec creates them per call — cheap relative to compressing the ≥ 4 KiB payloads it targets.

### Future work on the same seam: encryption codec

`PayloadCodec` is also Temporal's seam for payload **encryption**. If/when at-rest encryption of payloads becomes a requirement, an encryption codec composes on the same wiring point (codecs chain), reusing the rollout machinery established here. Out of scope for this ADR.

## Rationale

1. **Cost levers stack**: compression shrinks history writes, egress through the gRPC bridge, and replay fetches simultaneously — one codec, three bills.
2. **Complements FileReference**: ADR-0008/0014 handle the data that must leave the payload path; this handles the data that legitimately stays in it.
3. **Decode-always makes rollout boring**: there is no flag value, ordering mistake, or partial deployment that produces an unreadable payload once step 1 has shipped.
4. **Composition pattern is the ecosystem standard**: wrapping the serialized original payload (as in Temporal's encryption codec samples) keeps the codec orthogonal to payload converters and preserves metadata byte-for-byte.

## Consequences

**Positive:**
- History storage, egress, and replay bandwidth drop for every payload over the threshold (JSON typically compresses 4–10x at level 3).
- Two-phase rollout is config-only and reversible — turning encode off stops new compressed payloads while old ones remain readable forever.
- The codec seam is now in place for future payload encryption.

**Negative:**
- Compressed payloads render as opaque `binary/zstd` blobs in the Temporal UI/CLI. The fix is a **codec server** (Temporal's remote-decode protocol for the UI) — future work, not blocking since operators debug primarily through logs and the observability stack.
- Marginal CPU per payload round trip (~negligible at level 3, by design).
- One new core dependency (`zstandard`).
- The flag must not be flipped ahead of fleet decode-capability; the choreography above is the contract.

## Implementation

- `application_sdk/execution/_temporal/codec.py` — `ZstdPayloadCodec`.
- `application_sdk/execution/_temporal/converter.py` — codec attached to every converter via `dataclasses.replace`.
- `application_sdk/execution/_temporal/backend.py` — client fallback converter routed through `create_data_converter()`.
- Configuration: `ATLAN_PAYLOAD_COMPRESSION` (`off` | `zstd`), `ATLAN_PAYLOAD_COMPRESSION_MIN_BYTES` (4096), `ATLAN_PAYLOAD_COMPRESSION_LEVEL` (3) — see [Configuration](../configuration.md#payload-compression).
