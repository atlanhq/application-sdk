"""``atlas.read_asset_attributes``: addressing one asset by type + QN suffix.

The suffix is the load-bearing part. A run's connection carries a freshly minted
epoch, so a suite cannot write an asset's qualifiedName — only its stable tail —
and that tail has to match on a **path-segment boundary** or `"sch"` silently
also addresses `"other_sch"`. Elasticsearch does the coarse filtering and the
boundary is enforced here, so both halves are pinned below.

Real pyatlan models throughout, for the reason given in
``test_sample_attributes.py``: presence is what this reader must not lose.
"""

from __future__ import annotations

from typing import Any

from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.expectations import AssetRef
from application_sdk.testing.harness.outcome import Indeterminate, Settled
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_CONN = "default/trino/1700000000"


def _schema_hit(qualified_name: str, **attributes: Any) -> Any:
    """One Schema hit at an absolute qualifiedName."""
    from pyatlan.model.assets import Schema

    return Schema.parse_obj(
        {
            "typeName": "Schema",
            "guid": f"guid-{qualified_name}",
            "attributes": {"qualifiedName": qualified_name, **attributes},
        }
    )


def _ref(suffix: str, type_name: str = "Schema") -> AssetRef:
    return AssetRef(type_name=type_name, qualified_name_suffix=suffix)


def _client(behavior: Any) -> FakeAtlasClient:
    return FakeAtlasClient(behavior)


def _page(*hits: Any) -> Any:
    def behavior(_request: Any) -> FakeSearchResult:
        return FakeSearchResult(list(hits))

    return behavior


async def _read(behavior: Any, refs: dict[AssetRef, tuple[str, ...]]) -> Any:
    return await atlas.read_asset_attributes(_client(behavior), _CONN, refs)


# ---------------------------------------------------------------------------
# The suffix resolves to one asset
# ---------------------------------------------------------------------------


async def test_a_suffix_resolves_to_the_asset_and_reads_its_values() -> None:
    reading = await _read(
        _page(_schema_hit(f"{_CONN}/db/sch_busy", viewsCount=10)),
        {_ref("sch_busy"): ("viewsCount",)},
    )
    assert isinstance(reading, Settled)
    matched = reading.value[_ref("sch_busy")]
    assert len(matched) == 1
    assert matched[0].qualified_name == f"{_CONN}/db/sch_busy"
    assert matched[0].values == {"viewsCount": 10}


async def test_a_deep_suffix_addresses_without_spelling_the_whole_path() -> None:
    # "col" rather than "db/sch/tbl/col" — the point of a suffix over a path.
    reading = await _read(
        _page(_schema_hit(f"{_CONN}/db/sch/tbl/col", order=3)),
        {_ref("col", "Column"): ("order",)},
    )
    assert isinstance(reading, Settled)
    assert len(reading.value[_ref("col", "Column")]) == 1


async def test_a_suffix_that_is_the_whole_tail_matches() -> None:
    reading = await _read(
        _page(_schema_hit(f"{_CONN}/db", schemaCount=1)),
        {_ref("db", "Database"): ("schemaCount",)},
    )
    assert isinstance(reading, Settled)
    assert len(reading.value[_ref("db", "Database")]) == 1


async def test_a_zero_is_still_a_zero_and_an_omission_still_absent() -> None:
    reading = await _read(
        _page(_schema_hit(f"{_CONN}/db/sch_empty", viewsCount=0)),
        {_ref("sch_empty"): ("viewsCount", "tableCount")},
    )
    assert isinstance(reading, Settled)
    values = reading.value[_ref("sch_empty")][0].values
    assert values == {"viewsCount": 0}
    assert "tableCount" not in values


# ---------------------------------------------------------------------------
# The segment boundary
# ---------------------------------------------------------------------------


async def test_a_suffix_does_not_match_mid_segment() -> None:
    # ES's wildcard returns it; the boundary check is what rejects it. A suite
    # declaring the schema "sch" does not mean "anything ending in those
    # characters".
    reading = await _read(
        _page(_schema_hit(f"{_CONN}/db/other_sch", viewsCount=4)),
        {_ref("sch"): ("viewsCount",)},
    )
    assert isinstance(reading, Settled)
    assert reading.value[_ref("sch")] == []


async def test_the_boundary_check_keeps_the_genuine_match() -> None:
    # The control for the case above, in one page: only the exact segment wins.
    reading = await _read(
        _page(
            _schema_hit(f"{_CONN}/db/other_sch", viewsCount=4),
            _schema_hit(f"{_CONN}/db/sch", viewsCount=7),
        ),
        {_ref("sch"): ("viewsCount",)},
    )
    assert isinstance(reading, Settled)
    matched = reading.value[_ref("sch")]
    assert [asset.qualified_name for asset in matched] == [f"{_CONN}/db/sch"]


async def test_an_asset_outside_the_connection_is_rejected() -> None:
    reading = await _read(
        _page(_schema_hit("default/trino/9999999999/db/sch", viewsCount=4)),
        {_ref("sch"): ("viewsCount",)},
    )
    assert isinstance(reading, Settled)
    assert reading.value[_ref("sch")] == []


async def test_every_match_is_returned_so_ambiguity_reaches_the_grader() -> None:
    # Two real segment matches under different databases. The reader does not
    # pick one: which of them is a finding is the evaluator's call.
    reading = await _read(
        _page(
            _schema_hit(f"{_CONN}/db1/sch", viewsCount=4),
            _schema_hit(f"{_CONN}/db2/sch", viewsCount=9),
        ),
        {_ref("sch"): ("viewsCount",)},
    )
    assert isinstance(reading, Settled)
    assert len(reading.value[_ref("sch")]) == 2


# ---------------------------------------------------------------------------
# The request the reader builds
# ---------------------------------------------------------------------------


async def test_the_search_is_scoped_by_type_connection_and_a_wildcard() -> None:
    client = _client(_page(_schema_hit(f"{_CONN}/db/sch", viewsCount=1)))
    await atlas.read_asset_attributes(client, _CONN, {_ref("sch"): ("viewsCount",)})
    request = client.asset.requests[0]
    body = request.dsl.query.to_dict()
    rendered = str(body)
    assert "Schema" in rendered
    assert f"{_CONN}/*sch" in rendered
    assert "viewsCount" in request.attributes


async def test_wildcard_metacharacters_in_a_suffix_are_escaped() -> None:
    # A literal "*" in an asset name must address that asset, not act as a
    # pattern. The boundary check compares the unescaped literal either way.
    client = _client(_page())
    await atlas.read_asset_attributes(client, _CONN, {_ref("sch*x"): ("viewsCount",)})
    assert "\\*" in str(client.asset.requests[0].dsl.query.to_dict())


async def test_a_suffix_with_surrounding_slashes_is_normalised() -> None:
    reading = await _read(
        _page(_schema_hit(f"{_CONN}/db/sch", viewsCount=1)),
        {_ref("/sch/"): ("viewsCount",)},
    )
    assert isinstance(reading, Settled)
    assert len(reading.value[_ref("/sch/")]) == 1


async def test_one_search_per_addressed_asset() -> None:
    client = _client(_page(_schema_hit(f"{_CONN}/db/sch", viewsCount=1)))
    await atlas.read_asset_attributes(
        client,
        _CONN,
        {_ref("sch_empty"): ("viewsCount",), _ref("sch_busy"): ("viewsCount",)},
    )
    assert client.searches == 2


async def test_nothing_declared_is_a_settled_noop_with_no_search() -> None:
    client = _client(_page())
    reading = await atlas.read_asset_attributes(client, _CONN, {})
    assert isinstance(reading, Settled)
    assert reading.value == {}
    assert client.searches == 0


# ---------------------------------------------------------------------------
# A failed read is not a missing asset
# ---------------------------------------------------------------------------


async def test_a_failed_search_is_indeterminate_not_zero_matches() -> None:
    # Zero matches is "the asset did not land" — a claim about the connector.
    # An unreadable search must not be able to spell it.
    def boom(_request: Any) -> FakeSearchResult:
        raise RuntimeError("atlas is down")

    reading = await _read(boom, {_ref("sch"): ("viewsCount",)})
    assert isinstance(reading, Indeterminate)
    assert isinstance(reading.cause, RuntimeError)
    assert _CONN in reading.label


async def test_one_failing_ref_makes_the_whole_reading_unreadable() -> None:
    seen: list[int] = []

    def behavior(_request: Any) -> FakeSearchResult:
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("this ref's search failed")
        return FakeSearchResult([_schema_hit(f"{_CONN}/db/sch", viewsCount=1)])

    reading = await _read(
        behavior,
        {_ref("sch_empty"): ("viewsCount",), _ref("sch_busy"): ("viewsCount",)},
    )
    assert isinstance(reading, Indeterminate)
