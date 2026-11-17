"""SqlApp's transform writes the same bytes through the shared streaming helper.

The golden file was produced by the per-record loop SqlApp carried before the
loop moved to :mod:`application_sdk.storage.streaming`, run on
``sql_app_transform_raw.jsonl`` with the last-sync values pinned below. Any
change to what a connector's ``transformed/<entity>/entities.json`` contains —
line order, blank-line handling, envelope, decorations, last-sync stamping —
fails here.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

from application_sdk.common.last_sync import resolve_last_sync_details
from tests.unit.templates.test_sql_app import (
    DecoratingSqlApp,
    _connection_ref,
    _make_task_input,
)

_FIXTURES = Path(__file__).parent / "fixtures"


async def test_transform_output_is_byte_identical_to_the_per_record_loop(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw" / "table"
    raw_dir.mkdir(parents=True)
    shutil.copy(_FIXTURES / "sql_app_transform_raw.jsonl", raw_dir / "records.json")
    pinned = resolve_last_sync_details(
        run="run-0001", workflow_name="wf-0001", run_at_ms=1700000000000
    )

    with patch(
        "application_sdk.templates.sql_app.resolve_last_sync_details",
        return_value=pinned,
    ):
        result = await DecoratingSqlApp().transform_tables(
            _make_task_input(
                output_path=str(tmp_path),
                connection=_connection_ref(name="example-connection"),
            )
        )

    golden = (_FIXTURES / "sql_app_transform_golden.jsonl").read_bytes()
    written = (tmp_path / "transformed" / "table" / "entities.json").read_bytes()
    assert written == golden
    assert result.total_record_count == golden.count(b"\n") == 40
