"""``atlas.sample_asset_attributes``: reading values without losing absence.

Every hit here is a **real** pyatlan model parsed from a search-shaped payload,
not a stand-in with the attributes hung on it. That is deliberate: the one thing
this reader has to get right is telling an attribute Atlas never returned from
one it returned as ``0`` or as ``null``, and that distinction lives entirely in
how the client library records which fields the wire carried. A double with a
hand-set ``tableCount`` would make every test pass and prove nothing.

The real ``FluentSearch`` builds the request throughout; only ``asset.search``
is faked, as in ``test_atlas_reads.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.expectations import AssetAttributes
from application_sdk.testing.harness.outcome import Indeterminate, Settled
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_QN = "default/trino/1700000000"


def _schema_hit(qualified_name: str, **attributes: Any) -> Any:
    """One Schema search hit, carrying exactly the attributes named.

    Args:
        qualified_name: The asset's qualifiedName.
        **attributes: Attributes the payload carries, spelled as Atlas spells
            them. An attribute NOT named here is absent from the payload, which
            is the state the reader must preserve.

    Returns:
        A real ``pyatlan`` ``Schema`` parsed from the payload.
    """
    from pyatlan.model.assets import Schema

    return Schema.parse_obj(
        {
            "typeName": "Schema",
            "guid": f"guid-{qualified_name}",
            "attributes": {"qualifiedName": qualified_name, **attributes},
        }
    )


def _client(behavior: Any) -> FakeAtlasClient:
    return FakeAtlasClient(behavior)


def _page(*hits: Any) -> Any:
    def behavior(_request: Any) -> FakeSearchResult:
        return FakeSearchResult(list(hits))

    return behavior


async def _sample(
    behavior: Any,
    type_attributes: dict[str, tuple[str, ...]] | None = None,
    **kwargs: Any,
) -> Any:
    return await atlas.sample_asset_attributes(
        _client(behavior),
        _QN,
        type_attributes or {"Schema": ("tableCount", "viewsCount")},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Present, present-but-zero, present-but-null, absent
# ---------------------------------------------------------------------------


async def test_a_value_is_read_back_under_its_atlan_name() -> None:
    reading = await _sample(_page(_schema_hit("d/s", tableCount=8, viewsCount=2)))
    assert isinstance(reading, Settled)
    assert reading.value["Schema"] == [
        AssetAttributes(qualified_name="d/s", values={"tableCount": 8, "viewsCount": 2})
    ]


async def test_a_zero_is_read_back_as_a_zero_not_dropped() -> None:
    reading = await _sample(_page(_schema_hit("d/s", tableCount=0)))
    assert isinstance(reading, Settled)
    # tableCount present and 0; viewsCount was never returned, so it is absent.
    assert reading.value["Schema"][0].values == {"tableCount": 0}


async def test_an_attribute_the_payload_omits_is_absent_not_none() -> None:
    reading = await _sample(_page(_schema_hit("d/s", tableCount=8)))
    assert isinstance(reading, Settled)
    values = reading.value["Schema"][0].values
    assert "viewsCount" not in values
    # The collapse this reader exists to avoid: .get() would answer None for
    # both an absent attribute and one Atlas returned as null.
    assert values.get("viewsCount") is None


async def test_an_explicit_null_is_present_with_none() -> None:
    reading = await _sample(_page(_schema_hit("d/s", tableCount=None)))
    assert isinstance(reading, Settled)
    values = reading.value["Schema"][0].values
    assert "tableCount" in values
    assert values["tableCount"] is None


async def test_an_attribute_the_type_does_not_carry_reads_as_absent() -> None:
    # A misspelled or non-existent attribute name: the harness can only see what
    # Atlas indexed, so this is a finding rather than a crash.
    reading = await _sample(
        _page(_schema_hit("d/s", tableCount=8)),
        {"Schema": ("noSuchAttribute",)},
    )
    assert isinstance(reading, Settled)
    assert reading.value["Schema"][0].values == {}


# ---------------------------------------------------------------------------
# The request the reader builds
# ---------------------------------------------------------------------------


async def test_each_requested_attribute_is_included_on_results() -> None:
    client = _client(_page(_schema_hit("d/s", tableCount=8)))
    await atlas.sample_asset_attributes(
        client, _QN, {"Schema": ("tableCount", "viewsCount")}
    )
    attributes = client.asset.requests[0].attributes
    assert "tableCount" in attributes
    assert "viewsCount" in attributes
    # The two the sampler always asks for, so the hit can name itself.
    assert "qualifiedName" in attributes
    assert "connectionQualifiedName" in attributes


async def test_per_type_caps_the_page_size_and_the_sample() -> None:
    hits = [_schema_hit(f"d/s{index}", tableCount=index) for index in range(5)]
    client = _client(lambda _r: FakeSearchResult(hits))
    reading = await atlas.sample_asset_attributes(
        client, _QN, {"Schema": ("tableCount",)}, per_type=2
    )
    assert isinstance(reading, Settled)
    assert len(reading.value["Schema"]) == 2
    assert client.asset.requests[0].dsl.size == 2


async def test_one_search_per_declared_type() -> None:
    client = _client(_page(_schema_hit("d/s", tableCount=8)))
    await atlas.sample_asset_attributes(
        client,
        _QN,
        {"Schema": ("tableCount",), "Database": ("schemaCount",)},
    )
    assert client.searches == 2


async def test_no_declared_types_is_a_settled_noop_with_no_search() -> None:
    client = _client(_page())
    reading = await atlas.sample_asset_attributes(client, _QN, {})
    assert isinstance(reading, Settled)
    assert reading.value == {}
    assert client.searches == 0


# ---------------------------------------------------------------------------
# A failed read is not an empty one
# ---------------------------------------------------------------------------


async def test_a_failed_search_is_indeterminate_not_an_empty_sample() -> None:
    def boom(_request: Any) -> FakeSearchResult:
        raise RuntimeError("atlas is down")

    reading = await _sample(boom)
    assert isinstance(reading, Indeterminate)
    assert isinstance(reading.cause, RuntimeError)
    assert _QN in reading.label


async def test_one_failing_type_makes_the_whole_reading_unreadable() -> None:
    seen: list[int] = []

    def behavior(_request: Any) -> FakeSearchResult:
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("this type's search failed")
        return FakeSearchResult([_schema_hit("d/s", tableCount=8)])

    reading = await atlas.sample_asset_attributes(
        _client(behavior),
        _QN,
        {"Schema": ("tableCount",), "Database": ("schemaCount",)},
    )
    assert isinstance(reading, Indeterminate)


async def test_a_type_that_landed_nothing_is_a_settled_empty_sample() -> None:
    reading = await _sample(_page())
    assert isinstance(reading, Settled)
    assert reading.value["Schema"] == []


async def test_a_model_with_no_record_of_set_fields_is_unreadable() -> None:
    """The degraded answer would be "every attribute is absent" — i.e. a report
    that the connector dropped all of them. Refusing to answer is the only safe
    direction, so the reader raises and the caller grades it ungraded."""

    class _OpaqueAttributes:
        qualified_name = "d/s"

    class _OpaqueHit:
        qualified_name = "d/s"
        attributes = _OpaqueAttributes()

    reading = await _sample(lambda _r: FakeSearchResult([_OpaqueHit()]))
    assert isinstance(reading, Indeterminate)
    assert isinstance(reading.cause, AttributeError)


async def test_a_hit_with_no_attributes_block_contributes_no_values() -> None:
    class _Bare:
        qualified_name = "d/s"
        attributes = None

    reading = await _sample(lambda _r: FakeSearchResult([_Bare()]))
    assert isinstance(reading, Settled)
    assert reading.value["Schema"] == [AssetAttributes(qualified_name="d/s", values={})]


@pytest.mark.parametrize("missing", [None, ""])
async def test_a_hit_with_no_qualified_name_still_reports_its_values(
    missing: str | None,
) -> None:
    hit = _schema_hit("d/s", tableCount=8)
    hit.attributes.qualified_name = missing
    reading = await _sample(lambda _r: FakeSearchResult([hit]))
    assert isinstance(reading, Settled)
    assert reading.value["Schema"][0].qualified_name == ""
    assert reading.value["Schema"][0].values == {"tableCount": 8}
