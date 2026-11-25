"""``atlas.sample_located_names``: qualified names with their ancestors'.

Every hit is a real pyatlan model parsed from a search-shaped payload, as in
``test_sample_attributes.py``, so the ancestor attribute names are the ones the
client library actually reads off the wire.
"""

from __future__ import annotations

from typing import Any

from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.expectations import LocatedName
from application_sdk.testing.harness.outcome import Indeterminate, Settled
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_QN = "default/snowflake/1700000000"
_DB = f"{_QN}/DB"
_SCH = f"{_DB}/SCH"


def _hit(type_name: str, qualified_name: str, **attributes: Any) -> Any:
    """One search hit of *type_name*, carrying exactly the attributes named."""
    from pyatlan.model.assets import Asset

    return Asset._convert_to_real_type_(
        {
            "typeName": type_name,
            "guid": f"guid-{qualified_name}",
            "attributes": {"qualifiedName": qualified_name, **attributes},
        }
    )


async def _sample(*hits: Any, type_name: str = "Table") -> Any:
    client = FakeAtlasClient(lambda _request: FakeSearchResult(list(hits)))
    return await atlas.sample_located_names(client, _QN, (type_name,), per_type=3)


async def test_a_table_carries_its_database_and_schema_in_order() -> None:
    outcome = await _sample(
        _hit(
            "Table",
            f"{_SCH}/a/b",
            databaseQualifiedName=_DB,
            schemaQualifiedName=_SCH,
        )
    )
    assert isinstance(outcome, Settled)
    assert outcome.value["Table"] == [LocatedName(f"{_SCH}/a/b", (_DB, _SCH))]


async def test_a_view_column_takes_its_parent_from_view_qualified_name() -> None:
    outcome = await _sample(
        _hit(
            "Column",
            f"{_SCH}/VW/COL",
            databaseQualifiedName=_DB,
            schemaQualifiedName=_SCH,
            viewQualifiedName=f"{_SCH}/VW",
        ),
        type_name="Column",
    )
    assert isinstance(outcome, Settled)
    assert outcome.value["Column"] == [
        LocatedName(f"{_SCH}/VW/COL", (_DB, _SCH, f"{_SCH}/VW"))
    ]


async def test_a_hit_without_ancestor_attributes_has_none() -> None:
    outcome = await _sample(_hit("Table", f"{_SCH}/T"))
    assert isinstance(outcome, Settled)
    assert outcome.value["Table"] == [LocatedName(f"{_SCH}/T", ())]


async def test_the_ancestor_attributes_are_requested_in_the_same_search() -> None:
    client = FakeAtlasClient(lambda _request: FakeSearchResult([]))
    await atlas.sample_located_names(client, _QN, ("Table",), per_type=3)
    assert client.searches == 1
    request = repr(client.asset.requests[0])
    for attribute in ("databaseQualifiedName", "schemaQualifiedName"):
        assert attribute in request


async def test_a_failed_search_is_no_verdict() -> None:
    def _boom(_request: Any) -> Any:
        raise RuntimeError("search down")

    outcome = await atlas.sample_located_names(
        FakeAtlasClient(_boom), _QN, ("Table",), per_type=3
    )
    assert isinstance(outcome, Indeterminate)
