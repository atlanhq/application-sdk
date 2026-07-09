# Storage load tests (opt-in, 50 GB-class)

Prove the slow-egress transfer stack (BLDX-1513 / BLDX-1523) holds at
incident-and-beyond scale by moving **real bytes** — no mocks on the data
path — through the same public entry points production uses:

| Test | Path exercised |
|------|----------------|
| `test_single_file_upload_then_chunked_download` | `upload_file` (multipart) → `download_file_chunked` (parallel range GETs, production chunk defaults) |
| `test_interrupted_download_resumes_only_missing_ranges` | transport dies at ~60% → checkpoint survives → retry fetches **only** the missing ranges |
| `test_file_reference_materialize_single_file` | `materialize_file_reference` — the incident's exact path (HEAD-once, chunked, version-pinned, sidecar-verified) |
| `test_download_prefix_bulk` | `download_prefix` — listing-supplied size+etag threading across 4 files |

Every test asserts end-to-end **sha256 equality**, so one corrupted or
misplaced byte fails the run.

## Running

Excluded from every default and CI suite (marker `load`).

```bash
# 1 GiB default — minutes on a laptop
uv run poe load-test-storage

# 50 GiB — the incident-class single-file scenario
ATLAN_LOAD_TEST_SIZE_MIB=51200 uv run poe load-test-storage

# One scenario only
ATLAN_LOAD_TEST_SIZE_MIB=51200 uv run pytest tests/load/test_storage_load.py::test_file_reference_materialize_single_file -m load -v -s --timeout=0
```

**Disk**: budget ~2× the configured size per test (object-store copy + local
copy; the source is deleted right after upload). For 50 GiB set `TMPDIR` to a
volume with ≥ 120 GB free.

**Time**: bounded by local disk throughput (the store is an obstore
`LocalStore`); on NVMe expect a few minutes per 50 GiB scenario.

## What this deliberately does NOT cover

- **Network egress speed** — the harness validates correctness, resume, and
  version-pinning mechanics at size; it cannot reproduce a 1 MiB/s WAN. To
  exercise a real cloud store, point an app (e.g. atlan-openapi-app) at a
  bucket and run the same entry points; the `s3_integration` /
  `azure_integration` / `gcs_integration` markers in
  `tests/integration/storage/` already cover binding-level cloud access.
- **Chunk-count mechanics at 50 GiB without the disk cost** — covered
  cheaply and always-on in
  `tests/unit/storage/test_ops.py::TestResumableChunkedDownload::test_50gb_equivalent_chunk_count_mechanics`
  (3200 chunks — 50 GiB at the production 16 MiB chunk size — with
  interrupt + resume).
