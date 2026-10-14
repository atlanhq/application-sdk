"""``atlas.count_lineage_edges``: which types a lineage process connects.

Every hit is a real pyatlan process model parsed from a search-shaped payload,
so the reader is exercised against the same ``inputs`` / ``outputs`` reference
objects a live search returns. The real ``FluentSearch`` builds the request;
only ``asset.search`` is faked.
"""

from __future__ import annotations

from typing import Any

from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.expectations import LineageEdgeCounts
from application_sdk.testing.harness.outcome import Indeterminate, Settled
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_QN = "default/sigma/1700000000123456"
_FIELD_TO_COLUMN = ("SigmaDataElementField", "SigmaDataModelColumn")
_COLUMN_TO_FIELD = ("Column", "SigmaDataElementField")


def _ref(type_name: str, name: str) -> dict[str, Any]:
    return {
        "typeName": type_name,
        "guid": f"guid-{name}",
        "uniqueAttributes": {"qualifiedName": f"{_QN}/{name}"},
    }


def _process(
    name: str,
    *,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    type_name: str = "ColumnProcess",
) -> Any:
    """One lineage process hit carrying the given input and output refs."""
    from pyatlan.model.assets import Asset

    return Asset._convert_to_real_type_(
        {
            "typeName": type_name,
            "guid": f"guid-{name}",
            "attributes": {
                "qualifiedName": f"{_QN}/{name}",
                "inputs": inputs,
                "outputs": outputs,
            },
        }
    )


def _client(*hits: Any) -> FakeAtlasClient:
    return FakeAtlasClient(lambda _request: FakeSearchResult(list(hits)))


def _value(reading: Any) -> LineageEdgeCounts:
    assert isinstance(reading, Settled)
    return reading.value


async def test_counts_each_requested_pair_from_input_and_output_types() -> None:
    hits = (
        _process(
            "p1",
            inputs=[_ref("SigmaDataElementField", "f1")],
            outputs=[_ref("SigmaDataModelColumn", "c1")],
        ),
        _process(
            "p2",
            inputs=[_ref("SigmaDataElementField", "f2"), _ref("Column", "w1")],
            outputs=[_ref("SigmaDataModelColumn", "c2")],
        ),
        _process(
            "p3",
            inputs=[_ref("Column", "w2")],
            outputs=[_ref("SigmaDataElementField", "f3")],
            type_name="BIProcess",
        ),
    )
    reading = await atlas.count_lineage_edges(
        _client(*hits), _QN, [_FIELD_TO_COLUMN, _COLUMN_TO_FIELD]
    )
    value = _value(reading)
    assert value.counts == {_FIELD_TO_COLUMN: 2, _COLUMN_TO_FIELD: 1}
    assert value.scanned == 3
    assert value.truncated is False


async def test_pairs_not_requested_are_not_counted() -> None:
    hit = _process(
        "p1",
        inputs=[_ref("Column", "w1")],
        outputs=[_ref("SigmaDataElementField", "f1")],
    )
    value = _value(
        await atlas.count_lineage_edges(_client(hit), _QN, [_FIELD_TO_COLUMN])
    )
    assert value.counts == {_FIELD_TO_COLUMN: 0}


async def test_a_target_type_only_among_inputs_does_not_count() -> None:
    # Direction is part of the claim: both types feeding the process is not a
    # link from one to the other.
    hit = _process(
        "p1",
        inputs=[
            _ref("SigmaDataElementField", "f1"),
            _ref("SigmaDataModelColumn", "c1"),
        ],
        outputs=[_ref("Table", "t1")],
    )
    value = _value(
        await atlas.count_lineage_edges(_client(hit), _QN, [_FIELD_TO_COLUMN])
    )
    assert value.counts == {_FIELD_TO_COLUMN: 0}


async def test_a_process_with_no_refs_counts_for_nothing() -> None:
    hit = _process("p1", inputs=[], outputs=[])
    value = _value(
        await atlas.count_lineage_edges(_client(hit), _QN, [_FIELD_TO_COLUMN])
    )
    assert value.counts == {_FIELD_TO_COLUMN: 0}
    assert value.scanned == 1


async def test_the_search_asks_for_inputs_outputs_and_process_types_under_the_connection() -> (
    None
):
    client = _client()
    await atlas.count_lineage_edges(client, _QN, [_FIELD_TO_COLUMN])
    (request,) = client.asset.requests
    assert {"inputs", "outputs"} <= set(request.attributes)
    query = str(request.dsl.query)
    assert f"{_QN}/" in query
    assert "ColumnProcess" in query and "BIProcess" in query
    assert "__superTypeNames" in query


async def test_a_failed_search_is_indeterminate_not_zero() -> None:
    def _boom(_request: Any) -> FakeSearchResult:
        raise RuntimeError("atlas is down")

    reading = await atlas.count_lineage_edges(
        FakeAtlasClient(_boom), _QN, [_FIELD_TO_COLUMN]
    )
    assert isinstance(reading, Indeterminate)
    assert isinstance(reading.cause, RuntimeError)


async def test_stopping_at_the_scan_cap_with_processes_unread_is_reported() -> None:
    hits = [
        _process(
            f"p{index}",
            inputs=[_ref("SigmaDataElementField", f"f{index}")],
            outputs=[_ref("SigmaDataModelColumn", f"c{index}")],
        )
        for index in range(3)
    ]
    value = _value(
        await atlas.count_lineage_edges(
            _client(*hits), _QN, [_FIELD_TO_COLUMN], scan_cap=2
        )
    )
    assert value.scanned == 2
    assert value.truncated is True
    assert value.counts == {_FIELD_TO_COLUMN: 2}


async def test_reading_exactly_the_cap_is_not_truncation() -> None:
    hits = [
        _process(
            f"p{index}",
            inputs=[_ref("SigmaDataElementField", f"f{index}")],
            outputs=[_ref("SigmaDataModelColumn", f"c{index}")],
        )
        for index in range(2)
    ]
    value = _value(
        await atlas.count_lineage_edges(
            _client(*hits), _QN, [_FIELD_TO_COLUMN], scan_cap=2
        )
    )
    assert value.truncated is False
    assert value.scanned == 2


async def test_no_pairs_makes_no_search() -> None:
    client = _client()
    value = _value(await atlas.count_lineage_edges(client, _QN, []))
    assert value.counts == {}
    assert client.searches == 0
