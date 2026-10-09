"""Unit tests for the read-only object-store assertion workflow (FND-3571)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import obstore
import pydantic
import pytest
from obstore.store import MemoryStore

from application_sdk.execution._temporal import store_assert
from application_sdk.execution._temporal.store_assert import (
    MAX_STORE_EXPECTATIONS,
    StoreAssertInput,
    StoreExpectation,
    StoreExpectationKind,
    evaluate_expectation,
    resolve_assert_prefix,
    store_assert_activity,
)

ROOT = "persistent-artifacts/default/postgres/run-1"


def _store(**objects: bytes) -> MemoryStore:
    store = MemoryStore()
    for key, data in objects.items():
        obstore.put(store, key, data)
    return store


def _put(store: MemoryStore, key: str, data: bytes = b"x") -> None:
    obstore.put(store, key, data)


def _absent(prefix: str = ROOT) -> StoreExpectation:
    return StoreExpectation(prefix=prefix, kind=StoreExpectationKind.ABSENT)


def _count(n: int, prefix: str = ROOT) -> StoreExpectation:
    return StoreExpectation(prefix=prefix, kind=StoreExpectationKind.COUNT, count=n)


# ---------------------------------------------------------------------------
# Prefix allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        pytest.param("artifacts/apps/x/y", "artifacts/apps/x/y/", id="apps"),
        pytest.param(
            "persistent-artifacts/a", "persistent-artifacts/a/", id="persistent"
        ),
        pytest.param("connection-cache/a/b", "connection-cache/a/b/", id="cache"),
        pytest.param(
            "/connection-cache/a/", "connection-cache/a/", id="slashes-trimmed"
        ),
        pytest.param(
            "./local/tmp/artifacts/apps/x", "artifacts/apps/x/", id="workflow-path"
        ),
    ],
)
def test_allowed_prefixes_resolve_with_a_trailing_slash(
    prefix: str, expected: str
) -> None:
    assert resolve_assert_prefix(prefix) == expected


@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param("", id="empty"),
        pytest.param("persistent-artifacts", id="root-itself"),
        pytest.param("artifacts/apps", id="nested-root-itself"),
        pytest.param("artifacts/other", id="sibling-of-nested-root"),
        pytest.param("artifacts", id="parent-of-root"),
        pytest.param("persistent-artifactsX/a", id="bleeds-into-sibling"),
        pytest.param("secrets/a", id="outside"),
        pytest.param("persistent-artifacts/../secrets", id="dotdot"),
        pytest.param("persistent-artifacts/a//b", id="empty-segment"),
    ],
)
def test_prefixes_outside_the_allowlist_are_rejected(prefix: str) -> None:
    with pytest.raises(ValueError):
        resolve_assert_prefix(prefix)


async def test_rejected_prefix_fails_without_listing(monkeypatch) -> None:
    def _must_not_list(*a: Any, **k: Any) -> Any:
        raise AssertionError("listed a rejected prefix")

    monkeypatch.setattr(store_assert, "obstore", SimpleNamespace(list=_must_not_list))
    observation = await evaluate_expectation(_absent("secrets/a"), MemoryStore())
    assert not observation.passed
    assert "strictly below" in observation.problem
    assert observation.objects_all is None


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


async def test_absent_passes_on_an_empty_prefix() -> None:
    store = _store(**{"persistent-artifacts/default/postgres/run-2/k": b"x"})
    observation = await evaluate_expectation(_absent(), store)
    assert observation.passed
    assert observation.objects_all == 0


async def test_absent_fails_on_a_childless_marker() -> None:
    """A leftover zero-byte folder marker is not 'absent' (GCS / ADLS HNS)."""
    store = _store(**{f"{ROOT}/folder": b""})
    observation = await evaluate_expectation(_absent(), store)
    assert not observation.passed
    assert observation.objects_all == 1


async def test_absent_fails_on_the_prefix_root_marker() -> None:
    """obstore stores a ``ROOT/`` marker as ``ROOT``, which no listing under
    ``ROOT/`` returns; delete_prefix removes it, so ABSENT must see it."""
    store = _store(**{ROOT: b""})
    assert [k for b in obstore.list(store, prefix=f"{ROOT}/") for k in b] == []
    observation = await evaluate_expectation(_absent(), store)
    assert not observation.passed
    assert observation.objects_all == 1


async def test_root_marker_probe_failure_is_reported(monkeypatch) -> None:
    async def _boom(*a: Any, **k: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(
        store_assert,
        "obstore",
        SimpleNamespace(list=obstore.list, head_async=_boom),
    )
    observation = await evaluate_expectation(_absent(), MemoryStore())
    assert not observation.passed
    assert observation.problem == "root-marker probe failed: PermissionError"


async def test_absent_does_not_bleed_into_a_sibling_prefix() -> None:
    store = _store(**{f"{ROOT}0/k": b"x"})
    assert (await evaluate_expectation(_absent(), store)).passed


async def test_present_and_count_use_the_data_view() -> None:
    store = _store(
        **{
            f"{ROOT}/dir": b"",  # marker with a child: dropped from the data view
            f"{ROOT}/dir/a.json": b"x",
            f"{ROOT}/b.json": b"x",
        }
    )
    present = await evaluate_expectation(
        StoreExpectation(prefix=ROOT, kind=StoreExpectationKind.PRESENT), store
    )
    assert present.passed
    assert (present.objects_all, present.objects_data) == (3, 2)

    assert (await evaluate_expectation(_count(2), store)).passed
    miss = await evaluate_expectation(_count(3), store)
    assert not miss.passed
    assert miss.objects_data == 2


async def test_present_fails_on_an_empty_prefix() -> None:
    observation = await evaluate_expectation(
        StoreExpectation(prefix=ROOT, kind=StoreExpectationKind.PRESENT), MemoryStore()
    )
    assert not observation.passed


async def test_count_over_the_scan_cap_is_truncated_and_fails(monkeypatch) -> None:
    monkeypatch.setattr(store_assert, "MAX_KEYS_SCANNED", 3)
    store = MemoryStore()
    for i in range(5):
        _put(store, f"{ROOT}/k{i}")
    observation = await evaluate_expectation(_count(3), store)
    assert observation.truncated
    assert not observation.passed
    assert "cannot be graded" in observation.problem


@pytest.mark.parametrize(
    "expectation",
    [
        pytest.param(
            StoreExpectation(prefix=ROOT, kind=StoreExpectationKind.COUNT),
            id="count-without-n",
        ),
        pytest.param(
            StoreExpectation(prefix=ROOT, kind=StoreExpectationKind.ABSENT, count=0),
            id="n-on-absent",
        ),
    ],
)
async def test_malformed_expectations_fail(expectation: StoreExpectation) -> None:
    observation = await evaluate_expectation(expectation, MemoryStore())
    assert not observation.passed
    assert observation.problem


def test_negative_count_is_rejected_at_construction() -> None:
    with pytest.raises(pydantic.ValidationError):
        _count(-1)


def test_expectation_count_is_capped() -> None:
    with pytest.raises(pydantic.ValidationError):
        StoreAssertInput(expectations=[_absent()] * (MAX_STORE_EXPECTATIONS + 1))


# ---------------------------------------------------------------------------
# Read-only, failure handling, activity
# ---------------------------------------------------------------------------


async def test_only_list_and_head_are_ever_called(monkeypatch) -> None:
    """The module's whole view of obstore is ``list`` + ``head_async``: no get,
    put or delete exists on it, so any such call would raise."""
    store = _store(**{f"{ROOT}/a": b"x"})
    monkeypatch.setattr(
        store_assert,
        "obstore",
        SimpleNamespace(list=obstore.list, head_async=obstore.head_async),
    )
    assert (await evaluate_expectation(_absent(f"{ROOT}-none"), store)).passed
    for expectation in (
        _absent(),
        _count(1),
        StoreExpectation(prefix=ROOT, kind=StoreExpectationKind.PRESENT),
    ):
        await evaluate_expectation(expectation, store)


async def test_listing_failure_is_reported_not_raised(monkeypatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("s3://internal-bucket/secret-detail")

    monkeypatch.setattr(store_assert, "obstore", SimpleNamespace(list=_boom))
    observation = await evaluate_expectation(_absent(), MemoryStore())
    assert not observation.passed
    # The exception type only: the message can carry store internals.
    assert observation.problem == "listing failed: RuntimeError"


async def test_activity_aggregates_in_input_order(monkeypatch) -> None:
    store = _store(**{f"{ROOT}/a": b"x"})
    monkeypatch.setattr(store_assert, "_resolve_store", lambda _: store)

    passing = await store_assert_activity(
        StoreAssertInput(expectations=[_count(1), _absent(f"{ROOT}-other")])
    )
    assert passing.passed
    assert [o.prefix for o in passing.observations] == [ROOT, f"{ROOT}-other"]

    failing = await store_assert_activity(
        StoreAssertInput(expectations=[_count(1), _absent()])
    )
    assert not failing.passed
    assert [o.passed for o in failing.observations] == [True, False]


async def test_no_expectations_is_not_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(store_assert, "_resolve_store", lambda _: MemoryStore())
    assert not (await store_assert_activity(StoreAssertInput())).passed


def test_ae_dispatch_extras_are_accepted() -> None:
    """AE adds correlation_id / workflow_slug / workflow_id to child args."""
    parsed = StoreAssertInput.model_validate(
        {
            "expectations": [{"prefix": ROOT, "kind": "absent"}],
            "correlation_id": "c",
            "workflow_slug": "s",
            "workflow_id": "w",
        }
    )
    assert parsed.expectations[0].kind is StoreExpectationKind.ABSENT
