"""Integration test: SqlApp pre-warms SQL auth cache before parallel extract burst
(BLDX-1295 — Argo parity for v3's parallel-activity execution model).

Bug context:
    v3's ``SqlApp.run()`` fans out ``extract_databases / extract_schemas
    / extract_tables / extract_columns`` as parallel Temporal activities.
    Each activity opens its own SQL client and authenticates against
    the source — concurrently. For sources whose first-auth handshake
    is cache-cold (MySQL 8's ``caching_sha2_password`` is the canonical
    example), N parallel cold-cache auth attempts can each fail and
    stack on the per-user ``failed_login_attempts`` counter — tripping
    the customer's ``FAILED_LOGIN_ATTEMPTS`` lockout policy. The
    customer then sees scheduled runs failing with ``Access denied``
    until the lockout auto-expires, at which point a manual ``Test
    Authentication`` works briefly, the next cron run re-trips it,
    repeat.

What this test asserts:
    Running through ``app.run()`` end-to-end (with the prime fix in
    place) produces a timeline where:
    1. The serial ``prime_sql_auth`` connection completes BEFORE any
       ``extract_*`` connection starts.
    2. The four ``extract_*`` connections then run concurrently
       (overlapping wall-clock windows) — we don't want to give up
       v3's parallel-extract speed; we just want the *first*
       connection to be serial.

What this test would fail at if the prime is removed:
    The bug-repro test in ``tests/unit/templates/test_sql_app.py::
    TestPrimeSqlAuth::test_reproduces_parallel_auth_burst_when_prime_skipped``
    pins the call-order assertion. This integration test pins the
    wall-clock-time assertion against a live ``app.run()``, so a
    regression where the prime is dropped from ``run()`` is caught
    both in unit (call order) and integration (timeline overlap).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.templates.contracts.sql_metadata import ExtractionInput
from application_sdk.templates.sql_app import SqlApp


class _TimingSqlClient(BaseSQLClient):
    """In-process SQL client that records the wall-clock window of each
    connection it serves. Lets the test assert ordering and overlap
    without needing a real MySQL server.
    """

    # Each entry: (label, connect_started_at, connect_ended_at)
    _CONNECTION_LOG: ClassVar[list[tuple[str, float, float]]] = []

    # Per-connection label injected by the caller; lets us correlate
    # connection windows back to which orchestration phase opened them
    # (prime_sql_auth vs each extract_*).
    label: str = ""

    def __init__(self) -> None:
        pass  # skip BaseSQLClient.__init__

    async def load(self, credentials: dict[str, Any] | None = None) -> None:
        start = time.perf_counter()
        # Simulate a real connection + auth handshake taking some time so
        # parallel attempts can plausibly overlap on the wall clock.
        await asyncio.sleep(0.05)
        end = time.perf_counter()
        self._CONNECTION_LOG.append((self.label, start, end))

    async def close(self) -> None:
        return None

    async def run_query(self, query: str, batch_size: int = 100_000):
        # Yield zero rows — extract activities exit fast.
        if False:
            yield []
        return

    async def get_results(self, query: str):
        # prime_sql_auth issues SELECT 1 through this method; we just
        # need to return something truthy-ish — no real query execution.
        return []


class _PrimeAuthIntegrationApp(SqlApp):
    """SqlApp subclass that injects a labelled ``_TimingSqlClient`` so
    each orchestration phase's connection window is identifiable in
    the timeline.
    """

    _app_registered: ClassVar[bool] = True

    sql_client_class: ClassVar[type[BaseSQLClient] | None] = _TimingSqlClient

    fetch_database_sql: ClassVar[str] = "SELECT 1"
    fetch_schema_sql: ClassVar[str] = "SELECT 1"
    fetch_table_sql: ClassVar[str] = "SELECT 1"
    fetch_column_sql: ClassVar[str] = "SELECT 1"

    # Track which phase is calling _init_sql_client so we can label
    # the resulting connection. _init_sql_client is the single
    # choke-point through which every SQL connection in SqlApp is
    # created — see prime_sql_auth, extract_*, etc.
    def __init__(self) -> None:
        super().__init__()
        self._label_seq = iter(
            [
                "prime_sql_auth",
                "extract_databases",
                "extract_schemas",
                "extract_tables",
                "extract_columns",
            ]
        )

    async def _init_sql_client(self, input):  # type: ignore[override]
        client = self.sql_client_class()  # type: ignore[misc]
        client.label = next(self._label_seq, "unknown")
        await client.load(credentials={})
        return client

    def map_database(self, record, connection_qn):
        return {"typeName": "Database"}

    def map_schema(self, record, connection_qn):
        return {"typeName": "Schema"}

    def map_table(self, record, connection_qn):
        return {"typeName": "Table"}

    def map_column(self, record, connection_qn):
        return {"typeName": "Column"}


async def test_prime_completes_before_extracts_start(tmp_path) -> None:
    """End-to-end: drive ``SqlApp.run()`` with a real timing-instrumented
    SQL client. The prime connection must complete strictly before any
    extract connection starts."""
    _TimingSqlClient._CONNECTION_LOG.clear()

    app = _PrimeAuthIntegrationApp()
    mock_wf_info = MagicMock(workflow_id="wf-prime-1", run_id="run-prime-1")

    with (
        patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        patch(
            "application_sdk.templates.sql_app._temporal_workflow.info",
            return_value=mock_wf_info,
        ),
    ):
        await app.run(ExtractionInput(output_path=str(tmp_path)))

    log = _TimingSqlClient._CONNECTION_LOG
    by_label = {label: (start, end) for label, start, end in log}

    # Assertion 1: prime ran first
    assert (
        "prime_sql_auth" in by_label
    ), f"prime_sql_auth connection was never recorded; got {list(by_label)}"

    prime_start, prime_end = by_label["prime_sql_auth"]

    extract_labels = (
        "extract_databases",
        "extract_schemas",
        "extract_tables",
        "extract_columns",
    )

    # Assertion 2: every extract connection started AFTER prime ended.
    # This is the core BLDX-1295 invariant — prevents the parallel
    # cold-auth burst from hitting the source before the cache is primed.
    for label in extract_labels:
        assert label in by_label, f"missing connection log for {label}"
        ext_start, _ = by_label[label]
        assert ext_start >= prime_end, (
            f"{label} started at {ext_start:.4f}s but prime ended at "
            f"{prime_end:.4f}s — extract connection overlapped or preceded "
            f"the prime, defeating the BLDX-1295 fix"
        )


async def test_extracts_still_run_concurrently_after_prime(tmp_path) -> None:
    """End-to-end: the prime serializes only the FIRST connection.
    Subsequent extracts must still overlap on the wall clock —
    otherwise we've regressed v3's parallel-extract performance."""
    _TimingSqlClient._CONNECTION_LOG.clear()

    app = _PrimeAuthIntegrationApp()
    mock_wf_info = MagicMock(workflow_id="wf-concurrent-1", run_id="run-concurrent-1")

    with (
        patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        patch(
            "application_sdk.templates.sql_app._temporal_workflow.info",
            return_value=mock_wf_info,
        ),
    ):
        await app.run(ExtractionInput(output_path=str(tmp_path)))

    log = _TimingSqlClient._CONNECTION_LOG

    extract_windows = [
        (label, start, end) for label, start, end in log if label.startswith("extract_")
    ]
    assert len(extract_windows) == 4, (
        f"Expected 4 extract connections; got {len(extract_windows)}: "
        f"{[lbl for lbl, _, _ in extract_windows]}"
    )

    # Compute pairwise overlap. With ``asyncio.gather`` + 50ms sleeps,
    # extract connections must have at least one overlapping pair.
    overlapping_pairs = 0
    for i, (a_label, a_start, a_end) in enumerate(extract_windows):
        for b_label, b_start, b_end in extract_windows[i + 1 :]:
            if a_start < b_end and b_start < a_end:
                overlapping_pairs += 1

    assert overlapping_pairs > 0, (
        "Expected at least one pair of extract connections to overlap "
        "on the wall clock — they're supposed to run in parallel via "
        f"asyncio.gather. Connection windows: "
        f"{[(lbl, end - start) for lbl, start, end in extract_windows]}"
    )
