import json

import pytest

from application_sdk.observability.lineage import (
    LineageObservabilityTracker,
    NoOpLineageObservabilityTracker,
    create_tracker,
    ObservabilityConfig,
)
from application_sdk.observability.lineage import ChunkedOutputHandler


@pytest.fixture
def tracker_log_false():
    """Tracker with successful lineage logging disabled."""
    return LineageObservabilityTracker(
        connector_type="test", log_successful_lineage=False
    )


@pytest.fixture
def tracker_log_true():
    """Tracker with successful lineage logging enabled."""
    return LineageObservabilityTracker(
        connector_type="test", log_successful_lineage=True
    )


@pytest.fixture
def tracker_with_config():
    """Tracker created via ObservabilityConfig."""
    config = ObservabilityConfig(enabled=True, log_successful_lineage=True)
    return LineageObservabilityTracker(connector_type="test", config=config)


# ---------------------------------------------------------------------------
# Construction & Config
# ---------------------------------------------------------------------------


def test_config_overrides_log_successful_lineage():
    """ObservabilityConfig.log_successful_lineage overrides the direct parameter."""
    config = ObservabilityConfig(enabled=True, log_successful_lineage=True)
    tracker = LineageObservabilityTracker(
        connector_type="t", config=config, log_successful_lineage=False
    )
    assert tracker.log_successful_lineage is True


def test_create_tracker_returns_noop_when_disabled():
    config = ObservabilityConfig(enabled=False)
    tracker = create_tracker(connector_type="t", config=config)
    assert isinstance(tracker, NoOpLineageObservabilityTracker)


def test_create_tracker_returns_real_when_enabled():
    config = ObservabilityConfig(enabled=True)
    tracker = create_tracker(connector_type="t", config=config)
    assert isinstance(tracker, LineageObservabilityTracker)


def test_create_tracker_returns_real_when_no_config():
    tracker = create_tracker(connector_type="t")
    assert isinstance(tracker, LineageObservabilityTracker)


# ---------------------------------------------------------------------------
# register_asset
# ---------------------------------------------------------------------------


def test_register_asset_updates_totals_and_counts(tracker_log_false):
    tracker_log_false.register_asset(
        "Datasource", "1", qualified_name="ds1", should_have_lineage=True
    )
    tracker_log_false.register_asset(
        "Datasource", "1", qualified_name="ds1", should_have_lineage=True
    )

    assert tracker_log_false.totals["totalAssets"] == 1
    assert tracker_log_false.totals["shouldHaveLineage"] == 1
    assert tracker_log_false.counts_by_type["Datasource"]["totalAssets"] == 1
    assert tracker_log_false.counts_by_type["Datasource"]["shouldHaveLineage"] == 1


def test_register_asset_updates_qualified_name_and_should_have_lineage(
    tracker_log_false,
):
    tracker_log_false.register_asset(
        "Datasource", "2", qualified_name=None, should_have_lineage=False
    )
    tracker_log_false.register_asset(
        "Datasource", "2", qualified_name="ds2", should_have_lineage=True
    )

    state = tracker_log_false.asset_index["Datasource:2"]
    assert state["qualifiedName"] == "ds2"
    assert state["shouldHaveLineage"] is True
    assert tracker_log_false.totals["shouldHaveLineage"] == 1


@pytest.mark.parametrize(
    "asset_type,asset_id",
    [
        (None, "id-1"),
        ("", "id-2"),
        ("Datasource", None),
        ("Datasource", ""),
    ],
)
def test_register_asset_ignores_invalid_inputs(
    caplog, tracker_log_false, asset_type, asset_id
):
    tracker_log_false.register_asset(
        asset_type, asset_id, qualified_name="qn", should_have_lineage=True
    )
    assert tracker_log_false.asset_index == {}
    assert any("invalid asset" in record.message.lower() for record in caplog.records)


def test_register_asset_updates_qualified_name_in_existing_details(tracker_log_true):
    tracker_log_true.register_asset(
        "Datasource", "d1", qualified_name=None, should_have_lineage=True
    )
    tracker_log_true.mark_output_lineage("Datasource", "d1")
    assert tracker_log_true.asset_details["Datasource:d1"].get("qualifiedName") is None
    tracker_log_true.register_asset(
        "Datasource", "d1", qualified_name="qn/d1", should_have_lineage=True
    )
    assert tracker_log_true.asset_details["Datasource:d1"]["qualifiedName"] == "qn/d1"


# ---------------------------------------------------------------------------
# mark_output_lineage
# ---------------------------------------------------------------------------


def test_mark_output_lineage_updates_counts_without_details_when_log_false(
    tracker_log_false,
):
    tracker_log_false.register_asset(
        "Datasource", "3", qualified_name="ds3", should_have_lineage=True
    )
    tracker_log_false.mark_output_lineage("Datasource", "3", qualified_name="ds3")

    assert tracker_log_false.totals["withLineage"] == 1
    assert tracker_log_false.counts_by_type["Datasource"]["withLineage"] == 1
    assert tracker_log_false.asset_details == {}


def test_mark_output_lineage_creates_details_when_log_true(tracker_log_true):
    tracker_log_true.register_asset(
        "Datasource", "4", qualified_name="ds4", should_have_lineage=True
    )
    tracker_log_true.mark_output_lineage(
        "Datasource",
        "4",
        qualified_name="ds4",
        source="test_source",
        source_details={"path": "sql"},
    )

    assert tracker_log_true.totals["withLineage"] == 1
    details = tracker_log_true.asset_details["Datasource:4"]
    assert details["hasLineage"] is True
    assert details["lineageSource"] == "test_source"
    assert details["lineageSourceDetails"] == {"path": "sql"}


def test_mark_output_lineage_clears_missing_counts_and_details(tracker_log_false):
    tracker_log_false.register_asset(
        "Dashboard", "11", qualified_name="dash11", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason("Dashboard", "11", reason="NO_SHEETS")
    tracker_log_false.mark_output_lineage("Dashboard", "11", qualified_name="dash11")

    assert tracker_log_false.totals["missingLineage"] == 0
    assert tracker_log_false.totals["withLineage"] == 1
    assert tracker_log_false.counts_by_type["Dashboard"]["missingLineage"] == 0
    assert tracker_log_false.counts_by_reason == {}
    assert tracker_log_false.counts_by_type_reason == {}
    assert "Dashboard:11" not in tracker_log_false.asset_details


# ---------------------------------------------------------------------------
# mark_input_lineage
# ---------------------------------------------------------------------------


def test_mark_input_lineage_sets_source(tracker_log_false):
    tracker_log_false.register_asset(
        "Datasource", "i1", qualified_name="i1", should_have_lineage=True
    )
    tracker_log_false.mark_input_lineage(
        "Datasource", "i1", qualified_name="i1", source="ds_tree"
    )

    state = tracker_log_false.asset_index["Datasource:i1"]
    assert state["lineageSource"] == "ds_tree"
    assert not state["hasLineage"]


def test_mark_input_lineage_with_log_true_stores_details(tracker_log_true):
    tracker_log_true.register_asset(
        "Datasource", "i2", qualified_name="i2", should_have_lineage=True
    )
    tracker_log_true.mark_input_lineage(
        "Datasource",
        "i2",
        qualified_name="i2",
        source="ds_tree",
        source_details={"parentId": "p1"},
    )

    details = tracker_log_true.asset_details["Datasource:i2"]
    assert details["lineageSource"] == "ds_tree"
    assert details["lineageSourceDetails"] == {"parentId": "p1"}


# ---------------------------------------------------------------------------
# record_failed_path_attempt
# ---------------------------------------------------------------------------


def test_record_failed_path_attempt_counts_and_details_when_missing(tracker_log_false):
    tracker_log_false.register_asset(
        "Datasource", "6", qualified_name="ds6", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason(
        "Datasource", "6", reason="CONNECTION_CACHE_MISS"
    )
    tracker_log_false.record_failed_path_attempt(
        "Datasource",
        "6",
        path="datasource_sql",
        reason="CONNECTION_CACHE_MISS",
        details={"tokens": ["foo", "bar"]},
    )

    assert (
        tracker_log_false.counts_by_failed_path["datasource_sql:CONNECTION_CACHE_MISS"]
        == 1
    )
    details = tracker_log_false.asset_details["Datasource:6"]
    assert details["failedPaths"][0]["path"] == "datasource_sql"


def test_record_failed_path_attempt_stores_details_before_missing_reason(
    tracker_log_false,
):
    tracker_log_false.register_asset(
        "Datasource", "7", qualified_name="ds7", should_have_lineage=True
    )
    tracker_log_false.record_failed_path_attempt(
        "Datasource",
        "7",
        path="datasource_sql",
        reason="CUSTOM_SQL_PARSER_FAILURE",
    )

    assert (
        tracker_log_false.counts_by_failed_path[
            "datasource_sql:CUSTOM_SQL_PARSER_FAILURE"
        ]
        == 1
    )
    assert "Datasource:7" in tracker_log_false.asset_details
    assert len(tracker_log_false.asset_details["Datasource:7"]["failedPaths"]) == 1


def test_record_failed_path_attempt_skips_details_for_lineaged_asset_when_log_false(
    tracker_log_false,
):
    tracker_log_false.register_asset(
        "Datasource", "7", qualified_name="ds7", should_have_lineage=True
    )
    tracker_log_false.mark_output_lineage("Datasource", "7", qualified_name="ds7")
    tracker_log_false.record_failed_path_attempt(
        "Datasource",
        "7",
        path="datasource_sql",
        reason="CUSTOM_SQL_PARSER_FAILURE",
    )

    assert (
        tracker_log_false.counts_by_failed_path[
            "datasource_sql:CUSTOM_SQL_PARSER_FAILURE"
        ]
        == 1
    )
    assert "failedPaths" not in tracker_log_false.asset_details.get("Datasource:7", {})


def test_record_failed_path_attempt_stores_details_when_log_true(tracker_log_true):
    tracker_log_true.register_asset(
        "Datasource", "fp1", qualified_name="fp1", should_have_lineage=True
    )
    tracker_log_true.record_failed_path_attempt(
        "Datasource",
        "fp1",
        path="custom_sql",
        reason="PARSER_FAILURE",
        details={"query": "select 1"},
    )

    assert tracker_log_true.counts_by_failed_path["custom_sql:PARSER_FAILURE"] == 1
    details = tracker_log_true.asset_details["Datasource:fp1"]
    assert details["failedPaths"][0]["path"] == "custom_sql"
    assert details["failedPaths"][0]["details"]["query"] == "select 1"


def test_failed_path_then_missing_reason_preserves_failed_paths(tracker_log_false):
    tracker_log_false.register_asset(
        "Dashboard", "d1", qualified_name="dash1", should_have_lineage=True
    )
    tracker_log_false.record_failed_path_attempt(
        "Dashboard",
        "d1",
        path="worksheet_to_dashboard",
        reason="UPSTREAM_NOT_FOUND",
        details={"worksheetId": "ws1"},
    )
    tracker_log_false.record_failed_path_attempt(
        "Dashboard",
        "d1",
        path="worksheet_to_dashboard",
        reason="UPSTREAM_NOT_FOUND",
        details={"worksheetId": "ws2"},
    )
    tracker_log_false.record_missing_reason(
        "Dashboard",
        "d1",
        reason="NO_VALID_WORKSHEETS",
        details={"worksheetIds": ["ws1", "ws2"]},
    )

    details = tracker_log_false.asset_details["Dashboard:d1"]
    assert details["reason"] == "NO_VALID_WORKSHEETS"
    assert len(details["failedPaths"]) == 2


# ---------------------------------------------------------------------------
# record_missing_reason
# ---------------------------------------------------------------------------


def test_record_missing_reason_tracks_reason_and_details(tracker_log_false):
    tracker_log_false.register_asset(
        "Worksheet", "5", qualified_name="ws5", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason(
        "Worksheet",
        "5",
        reason="NO_UPSTREAM",
        details={"worksheetId": "5"},
    )
    # Second call is a no-op
    tracker_log_false.record_missing_reason(
        "Worksheet",
        "5",
        reason="NO_UPSTREAM",
        details={"worksheetId": "5"},
    )

    assert tracker_log_false.totals["missingLineage"] == 1
    assert tracker_log_false.counts_by_reason["NO_UPSTREAM"] == 1
    details = tracker_log_false.asset_details["Worksheet:5"]
    assert details["reason"] == "NO_UPSTREAM"
    assert details["reasonDetails"] == {"worksheetId": "5"}


def test_record_missing_reason_skipped_when_asset_has_lineage(tracker_log_false):
    tracker_log_false.register_asset(
        "Datasource", "r1", qualified_name="r1", should_have_lineage=True
    )
    tracker_log_false.mark_output_lineage("Datasource", "r1", qualified_name="r1")
    tracker_log_false.record_missing_reason(
        "Datasource", "r1", reason="SHOULD_NOT_APPEAR"
    )

    assert tracker_log_false.totals["missingLineage"] == 0
    assert "SHOULD_NOT_APPEAR" not in tracker_log_false.counts_by_reason


def test_record_missing_reason_calls_metrics():
    events = []

    class FakeMetrics:
        def missing_lineage_event(self, reason):
            events.append(reason)

    tracker = LineageObservabilityTracker(connector_type="test", metrics=FakeMetrics())
    tracker.register_asset("Ds", "m1", should_have_lineage=True)
    tracker.record_missing_reason("Ds", "m1", reason="CACHE_MISS")

    assert events == ["CACHE_MISS"]


# ---------------------------------------------------------------------------
# apply_relationship_lineage
# ---------------------------------------------------------------------------


def test_apply_relationship_lineage_log_false(tracker_log_false):
    tracker_log_false.register_asset(
        "Workbook", "wb1", qualified_name="wb1", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason("Workbook", "wb1", reason="NO_LINEAGE")

    tracker_log_false.apply_relationship_lineage(
        "Workbook",
        "wb1",
        qualified_name="wb1",
        source_details={"dsId": "d1"},
    )

    assert tracker_log_false.totals["withLineage"] == 1
    assert tracker_log_false.totals["missingLineage"] == 0
    assert "Workbook:wb1" not in tracker_log_false.asset_details


def test_apply_relationship_lineage_log_true(tracker_log_true):
    tracker_log_true.register_asset(
        "Workbook", "wb2", qualified_name="wb2", should_have_lineage=True
    )
    tracker_log_true.record_failed_path_attempt(
        "Workbook", "wb2", path="sql", reason="MISS"
    )
    tracker_log_true.apply_relationship_lineage(
        "Workbook",
        "wb2",
        qualified_name="wb2",
        source_details={"dsId": "d2"},
    )

    assert tracker_log_true.totals["withLineage"] == 1
    details = tracker_log_true.asset_details["Workbook:wb2"]
    assert details["hasLineage"] is True
    assert details["lineageSource"] == "relationship_lineage"
    assert "failedPathAttempts" in details["lineageSourceDetails"]


def test_apply_relationship_lineage_skips_already_lineaged(tracker_log_false):
    tracker_log_false.register_asset(
        "Workbook", "wb3", qualified_name="wb3", should_have_lineage=True
    )
    tracker_log_false.mark_output_lineage("Workbook", "wb3", qualified_name="wb3")
    tracker_log_false.apply_relationship_lineage(
        "Workbook",
        "wb3",
        qualified_name="wb3",
        source_details={},
    )
    assert tracker_log_false.totals["withLineage"] == 1


# ---------------------------------------------------------------------------
# _clear_missing_state
# ---------------------------------------------------------------------------


def test_clear_missing_state_partial_reason_count(tracker_log_false):
    tracker_log_false.register_asset(
        "Datasource", "a1", qualified_name="a1", should_have_lineage=True
    )
    tracker_log_false.register_asset(
        "Datasource", "a2", qualified_name="a2", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason("Datasource", "a1", reason="CACHE_MISS")
    tracker_log_false.record_missing_reason("Datasource", "a2", reason="CACHE_MISS")

    assert tracker_log_false.counts_by_reason["CACHE_MISS"] == 2

    tracker_log_false.mark_output_lineage("Datasource", "a1", qualified_name="a1")

    assert tracker_log_false.counts_by_reason["CACHE_MISS"] == 1
    assert tracker_log_false.counts_by_type_reason["Datasource"]["CACHE_MISS"] == 1


# ---------------------------------------------------------------------------
# _build_asset_output
# ---------------------------------------------------------------------------


def test_build_asset_output_includes_lineage_source_details(tracker_log_true):
    tracker_log_true.register_asset(
        "Ds", "bo1", qualified_name="bo1", should_have_lineage=True
    )
    tracker_log_true.record_failed_path_attempt("Ds", "bo1", path="sql", reason="MISS")
    tracker_log_true.mark_output_lineage(
        "Ds",
        "bo1",
        qualified_name="bo1",
        source="test",
        source_details={"detail": "val"},
    )

    asset = tracker_log_true.asset_details["Ds:bo1"]
    output = tracker_log_true._build_asset_output(asset)
    assert output["lineageSource"] == "test"
    assert "lineageSourceDetails" in output


def test_build_asset_output_includes_reason_details():
    tracker = LineageObservabilityTracker(
        connector_type="test", log_successful_lineage=False
    )
    tracker.register_asset("Ws", "bo2", qualified_name="bo2", should_have_lineage=True)
    tracker.record_missing_reason(
        "Ws", "bo2", reason="NO_UPSTREAM", details={"count": 0}
    )

    asset = tracker.asset_details["Ws:bo2"]
    output = tracker._build_asset_output(asset)
    assert output["reason"] == "NO_UPSTREAM"
    assert output["reasonDetails"] == {"count": 0}


# ---------------------------------------------------------------------------
# write_asset_details / build_output / flush
# ---------------------------------------------------------------------------


def test_write_asset_details_streams_records(tmp_path, tracker_log_true):
    output_prefix = str(tmp_path / "assets")
    output_path = tmp_path / "assets.json"
    tracker_log_true.asset_details_handler = ChunkedOutputHandler(
        output_prefix, chunk_size=None
    )
    tracker_log_true.register_asset(
        "Datasource", "8", qualified_name="ds8", should_have_lineage=True
    )
    tracker_log_true.mark_output_lineage("Datasource", "8", qualified_name="ds8")
    tracker_log_true.record_missing_reason("Worksheet", "9", reason="NO_UPSTREAM")

    tracker_log_true.write_asset_details()

    lines = output_path.read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    asset_types = {entry["assetType"] for entry in parsed}
    assert asset_types == {"Datasource", "Worksheet"}


def test_write_asset_details_noop_without_handler(tracker_log_false):
    tracker_log_false.register_asset(
        "Ds", "wd1", qualified_name="wd1", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason("Ds", "wd1", reason="MISS")
    tracker_log_false.write_asset_details()


def test_build_output_returns_summary_without_assets(tracker_log_false):
    tracker_log_false.register_asset(
        "Datasource", "10", qualified_name="ds10", should_have_lineage=True
    )
    tracker_log_false.record_missing_reason(
        "Datasource", "10", reason="CONNECTION_CACHE_MISS"
    )

    output = tracker_log_false.build_output()
    assert "assets" not in output
    assert output["totals"]["missingLineage"] == 1
    assert output["totalsByReason"]["CONNECTION_CACHE_MISS"] == 1


def test_build_output_coverage_percentages(tracker_log_false):
    for i in range(10):
        tracker_log_false.register_asset("Ds", str(i), should_have_lineage=True)
    for i in range(7):
        tracker_log_false.mark_output_lineage("Ds", str(i))
    for i in range(7, 10):
        tracker_log_false.record_missing_reason("Ds", str(i), reason="MISS")

    output = tracker_log_false.build_output()
    assert output["coverage"]["shouldHaveLineageCoverage"] == 70.0
    assert output["coverage"]["totalLineageCoverage"] == 70.0
    assert output["totalsByType"]["Ds"]["coverage"]["shouldHaveLineageCoverage"] == 70.0


def test_flush_writes_and_returns_output(tmp_path):
    output_prefix = str(tmp_path / "assets")
    handler = ChunkedOutputHandler(output_prefix, chunk_size=None)
    tracker = LineageObservabilityTracker(
        connector_type="test",
        log_successful_lineage=True,
        asset_details_handler=handler,
    )
    tracker.register_asset("Ds", "f1", should_have_lineage=True)
    tracker.record_missing_reason("Ds", "f1", reason="MISS")

    output = tracker.flush()
    assert output["totals"]["missingLineage"] == 1

    output_path = tmp_path / "assets.json"
    lines = output_path.read_text().splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# NoOp tracker
# ---------------------------------------------------------------------------


def test_noop_tracker_methods():
    noop = NoOpLineageObservabilityTracker()
    noop.register_asset("Ds", "1")
    noop.mark_output_lineage("Ds", "1")
    noop.mark_input_lineage("Ds", "1")
    noop.record_failed_path_attempt("Ds", "1", path="p", reason="r")
    noop.record_missing_reason("Ds", "1", reason="r")
    noop.apply_relationship_lineage("Ds", "1", qualified_name="qn", source_details={})
    assert noop.build_output() == {}
    noop.write_asset_details()
    assert noop.flush() == {}


# ---------------------------------------------------------------------------
# Invalid asset warnings for all methods
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name,kwargs,expected_log_fragment",
    [
        (
            "mark_output_lineage",
            {"asset_type": "", "asset_id": "x1", "qualified_name": "qn"},
            "skipping mark_output_lineage",
        ),
        (
            "mark_input_lineage",
            {"asset_type": None, "asset_id": "x2", "qualified_name": "qn"},
            "skipping mark_input_lineage",
        ),
        (
            "record_failed_path_attempt",
            {"asset_type": "", "asset_id": "x3", "path": "p", "reason": "r"},
            "skipping record_failed_path_attempt",
        ),
        (
            "record_missing_reason",
            {"asset_type": None, "asset_id": "x4", "reason": "r"},
            "skipping record_missing_reason",
        ),
        (
            "apply_relationship_lineage",
            {
                "asset_type": "",
                "asset_id": "x5",
                "qualified_name": "qn",
                "source_details": {},
            },
            "skipping apply_relationship_lineage",
        ),
    ],
)
def test_invalid_asset_logs_skip_warning(
    caplog, tracker_log_false, method_name, kwargs, expected_log_fragment
):
    getattr(tracker_log_false, method_name)(**kwargs)
    assert any(
        expected_log_fragment in record.message.lower() for record in caplog.records
    )
