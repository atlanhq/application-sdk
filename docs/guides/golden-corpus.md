# The Golden-Corpus Contract

`application_sdk.testing.integration.corpus` declares one fixture-tree layout, one env var, and a loader for in-repo golden corpora — the sanitized fixture tree an integration suite reads when it has no live source. Pairs with the [shared integration fixtures](./integration-fixtures.md) for running the workflow and `assert_matches_golden` for comparing its output.

A golden corpus is the sanitized fixture tree an integration suite reads when it has no live source: recorded source payloads to feed the transform, and the expected records the transform should produce.

```
$E2E_GOLDEN_ROOT/            # or an in-repo default the suite passes
  [<tenant>/]                # optional — declare tenant_level=True to use it
    raw/                     # the transform's INPUT
    transformed/             # what the transform should produce
```

```python
from pathlib import Path

import pytest
from application_sdk.testing.integration.corpus import GoldenLayout, require_golden_corpus

_LAYOUT = GoldenLayout(
    stages=("raw", "processed", "transformed"),
    input_stage="processed",
    tenant_level=True,
)


@pytest.fixture(scope="session")
def corpus():
    return require_golden_corpus(
        layout=_LAYOUT,
        default_root=Path(__file__).parent / "fixtures" / "golden",
    ).for_tenant("tenant-a")


def test_transform_input_present(corpus) -> None:
    assert corpus.records(corpus.layout.input_stage)
```

Four rules, each collapsing a divergence that appeared across connector suites written independently:

- **One env var: `E2E_GOLDEN_ROOT`.** Every test-harness variable in `application_sdk/testing/` uses the `E2E_` prefix (`E2E_SOURCE_AVAILABLE`, `E2E_TENANT_DEPLOYMENT_NAME`, `E2E_WORKER_HEALTH_URL`, the `E2E_<DATASOURCE>_*` credential family). `ATLAN_*` is runtime SDK configuration read into module constants — a different contract. Not one variable per connector.
- **`raw/` means "the transform's input"**, not "untouched bytes from the source". A connector with a genuine post-processing stage declares it — `stages=(..., "processed", ...)`, `input_stage="processed"` — so which stage feeds the transform is a stated fact rather than something a reader infers from a test file. No fifth word needed.
- **The tenant level is optional**, and off by default. Connectors with no tenant axis must not invent a synthetic directory to satisfy a loader, and a corpus with one may name its tenant directories anything.
- **Missing and malformed are different failures.** No corpus configured — `E2E_GOLDEN_ROOT` unset and no default root on disk — is the declared-absent case, and `require_golden_corpus` skips, the single skip idiom for this tier, matching the [skip-not-fail contract](./integration-testing.md#markers-and-ci-tiering-the-directory-is-the-boundary). A corpus that exists but does not match its declared layout raises with the offending path named: a missing stage directory, a stage holding no files, an unparseable file. An empty stage is an error, never an empty list — a loader that silently yields nothing turns a broken fixture tree into a passing test.

## Scope boundary

This contract governs the **in-repo fixture tree** only. The upstream source buckets these corpora were captured from genuinely differ — legacy Argo writes `{extracted,transformed}-metadata`, an SDK app writes `{raw,transformed}` — and neither is ours to rename. Capture from whatever the bucket holds; commit it under the layout above.

## Formats

JSON (one record object or an array of them), NDJSON (`.ndjson` / `.jsonl`), CSV (header row required), and parquet. `read_records(path)` dispatches on the suffix and every failure names the file, and for NDJSON the line.

Parquet needs `pyarrow`, which ships in the `[sql]` and `[incremental]` extras rather than the SDK core, so it is imported lazily and its absence raises `GoldenParquetSupportError` naming the extra to install.

Comparing a run's output against the corpus's expected records is a separate concern with its own helpers; this module declares the tree and reads it.

## Comparing against the corpus

`assert_matches_golden` strips only the three `RUN_VOLATILE_FIELDS` (`lastSyncRun`, `lastSyncRunAt`, `lastSyncWorkflowName`) by default — deliberately not `guid`, `updateTime`, `createTime`, or `__timestamp`-suffixed keys, because whether those vary depends on the connector. If your first golden run fails on fields like these, that is the dial to reach for: pass `extra_ignore={"guid", "updateTime", ...}` (which adds to the canonical three) rather than `ignore=` (which replaces them).

The diff is keyed and order-independent **across records**; **within** a field it is order-sensitive — a reordered list (relationship arrays, `attributes.columns`) reports as a mismatch. Sort unordered arrays before capture and comparison, or ignore that one field; do not downgrade the typename's `DiffPolicy` for ordering noise.
