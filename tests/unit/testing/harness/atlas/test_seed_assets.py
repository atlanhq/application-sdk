"""The lineage-parent seed: what gets written, in what order, under what name.

Three claims carry the feature and each is pinned tenant-free here.

**Exactness is the contract.** Atlas resolves a lineage ref by exact-match
qualified name and exact type, so the QNs and type names the seed writes are the
whole of its value: a case-folded segment or a ``Table`` where the connector's
refs say ``View`` seeds a tree that resolves nothing. The tests below assert the
composed QNs byte for byte, mixed case included.

**Parents strictly before children.** Atlas rejects a child whose parent ref
does not resolve, and the seed's batching cuts one flat list into consecutive
chunks — so the ordering invariant is on the flat list, and that is what gets
pinned rather than any one batch boundary.

**The first batch is the policy-window probe.** A fresh Connection 403s child
writes until its access policies go live and no API reports when that has
happened, so the first chunk is retried on the same reading-not-error shape
``BaseE2ETest._retry_seed_probe_async`` uses, and every later chunk is written
once.

The real pyatlan ``creator`` constructors run throughout; only ``asset.save``
and ``asset.search`` are faked.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.atlas import seed
from application_sdk.testing.harness.atlas._errors import (
    SeedConnectionNotSearchableError,
)
from application_sdk.testing.harness.budgets import Budget
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_CONNECTION_QN = "default/snowflake/1700000000-abc123"


def _budget(**overrides: Any) -> Budget:
    base: dict[str, Any] = {
        "timeout": timedelta(seconds=300),
        "poll_interval": timedelta(seconds=30),
        "heartbeat": None,
    }
    return Budget(**{**base, **overrides})


def _spec(databases: tuple[seed.DatabaseSpec, ...]) -> seed.SeedSpec:
    return seed.SeedSpec(
        connector_type="snowflake",
        qualified_name=_CONNECTION_QN,
        display_name="snowflake-seed",
        admin_roles=("role-guid",),
        databases=databases,
    )


def _client() -> FakeAtlasClient:
    # Every search answers "one hit": the connection poll settles on its first
    # probe, which keeps these tests about the writes.
    return FakeAtlasClient(lambda _request: FakeSearchResult(count=1))


async def _seed(client: FakeAtlasClient, spec: seed.SeedSpec) -> seed.SeededConnection:
    with fake_clock():
        return await seed.seed_assets(
            client,
            spec,
            connection_budget=_budget(
                start_grace=timedelta(seconds=90), max_transient_failures=4
            ),
            probe_budget=_budget(),
        )


def _written(client: FakeAtlasClient) -> list[Any]:
    """Flatten the recorded saves to the skeleton assets, in write order.

    The first save is :func:`create_connection`'s single Connection; every
    later one is a batch (a list). Retried first batches appear once per
    attempt, so callers that need one copy de-duplicate deliberately.
    """
    flattened: list[Any] = []
    for saved in client.saved[1:]:
        flattened.extend(saved)
    return flattened


class TestExactnessAndOrder:
    """The composed QNs and type names are the connector's, byte for byte."""

    _TREE = (
        seed.DatabaseSpec(
            name="ANALYTICS",
            schemas=(
                seed.SchemaSpec(
                    name="Public_v2",
                    tables=(
                        seed.TableSpec(name="ORDERS", columns=("ID", "amount")),
                        seed.TableSpec(name="orders_view", type_name="View"),
                    ),
                ),
            ),
        ),
    )

    async def test_qualified_names_preserve_every_segment_byte_for_byte(self) -> None:
        client = _client()
        await _seed(client, _spec(self._TREE))
        qns = [asset.qualified_name for asset in _written(client)]
        assert qns == [
            f"{_CONNECTION_QN}/ANALYTICS",
            f"{_CONNECTION_QN}/ANALYTICS/Public_v2",
            f"{_CONNECTION_QN}/ANALYTICS/Public_v2/ORDERS",
            f"{_CONNECTION_QN}/ANALYTICS/Public_v2/ORDERS/ID",
            f"{_CONNECTION_QN}/ANALYTICS/Public_v2/ORDERS/amount",
            f"{_CONNECTION_QN}/ANALYTICS/Public_v2/orders_view",
        ]

    async def test_type_names_are_type_strict(self) -> None:
        """A ``View`` ref is not resolved by a ``Table`` — the seed must not
        flatten the distinction the resolver enforces."""
        client = _client()
        await _seed(client, _spec(self._TREE))
        type_names = [asset.type_name for asset in _written(client)]
        assert type_names == ["Database", "Schema", "Table", "Column", "Column", "View"]

    async def test_parents_precede_children_in_the_flat_write_order(self) -> None:
        client = _client()
        await _seed(client, _spec(self._TREE))
        seen: set[str] = {_CONNECTION_QN}
        for asset in _written(client):
            qn = asset.qualified_name
            parent = qn.rsplit("/", 1)[0]
            assert parent in seen, f"{qn} written before its parent {parent}"
            seen.add(qn)

    async def test_column_order_is_the_specs_one_based_position(self) -> None:
        client = _client()
        await _seed(client, _spec(self._TREE))
        orders = [
            asset.order for asset in _written(client) if asset.type_name == "Column"
        ]
        assert orders == [1, 2]

    async def test_the_report_counts_what_was_written_per_type(self) -> None:
        client = _client()
        report = await _seed(client, _spec(self._TREE))
        assert report.qualified_name == _CONNECTION_QN
        assert dict(report.created) == {
            "Database": 1,
            "Schema": 1,
            "Table": 1,
            "View": 1,
            "Column": 2,
        }


class TestBatchingAndProbe:
    """The first chunk is the policy-window probe; the rest are written once."""

    _WIDE = (
        seed.DatabaseSpec(
            name="DB",
            schemas=(
                seed.SchemaSpec(
                    name="S",
                    tables=tuple(seed.TableSpec(name=f"T{i:02d}") for i in range(30)),
                ),
            ),
        ),
    )

    async def test_writes_are_chunked_at_the_batch_size(self) -> None:
        client = _client()
        await _seed(client, _spec(self._WIDE))
        # 1 Connection save + 32 skeleton assets in ceil(32/20) = 2 batches.
        batches = client.saved[1:]
        assert [len(batch) for batch in batches] == [seed.SEED_BATCH_SIZE, 12]

    async def test_a_refused_first_write_is_retried_until_permitted(self) -> None:
        """The policy-window shape: a 403 is the expected answer while the fresh
        connection's policies provision, so it is a reading to wait out — never
        a transient-error streak to exhaust."""
        client = _client()
        refusals = {"left": 2}
        real_save = client.asset.save

        async def _flaky_save(assets: Any) -> None:
            if isinstance(assets, list) and refusals["left"] > 0:
                refusals["left"] -= 1
                raise PermissionError("ATLAS-403-00-001: policies not live yet")
            await real_save(assets)

        client.asset.save = _flaky_save  # type: ignore[method-assign]
        report = await _seed(client, _spec(self._WIDE))
        assert refusals["left"] == 0
        assert sum(report.created.values()) == 32
        # Both batches landed exactly once despite the two refused attempts.
        assert [len(batch) for batch in client.saved[1:]] == [seed.SEED_BATCH_SIZE, 12]

    async def test_a_first_write_that_never_becomes_permitted_reraises_it(
        self,
    ) -> None:
        """The suite has to see the 403 itself, not a harness paraphrase."""
        client = _client()
        rejection = PermissionError("ATLAS-403-00-001: still refused")

        async def _refuse(assets: Any) -> None:
            if isinstance(assets, list):
                raise rejection

        client.asset.save = _refuse  # type: ignore[method-assign]
        with pytest.raises(PermissionError) as excinfo:
            await _seed(client, _spec(self._WIDE))
        assert excinfo.value is rejection

    async def test_an_empty_tree_seeds_only_the_connection(self) -> None:
        client = _client()
        report = await _seed(client, _spec(()))
        assert client.saved[1:] == []
        assert dict(report.created) == {}


class TestConnectionGate:
    """Nothing is written beneath a connection Atlas cannot see."""

    async def test_an_unsearchable_connection_fails_before_any_child_write(
        self,
    ) -> None:
        client = FakeAtlasClient(lambda _request: FakeSearchResult(count=0))
        with pytest.raises(SeedConnectionNotSearchableError):
            with fake_clock():
                await seed.seed_assets(
                    client,
                    _spec(TestExactnessAndOrder._TREE),
                    connection_budget=_budget(
                        start_grace=timedelta(seconds=90),
                        max_transient_failures=4,
                    ),
                    probe_budget=_budget(),
                )
        # Only the Connection itself was ever saved.
        assert len(client.saved) == 1
