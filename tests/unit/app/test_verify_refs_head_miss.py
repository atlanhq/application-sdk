"""Confirm a HEAD miss through the body-bearing listing before rejecting a handoff."""

from unittest.mock import AsyncMock, MagicMock, patch

import obstore
import pytest
from obstore.store import MemoryStore

from application_sdk.contracts.storage import VerifyRefsInput
from application_sdk.contracts.types import FileReference, StoreTarget
from application_sdk.storage import ops
from application_sdk.storage.errors import (
    StorageGatewayNoRouteError,
    StorageHandoffIncompleteError,
)
from tests.unit.app.test_verify_refs import PREFIX, _make_app, _ref

HEAD_404 = "Server returned non-2xx status code: 404 Not Found"
GATEWAY_404 = HEAD_404 + ': {"message":"no Route matched with those values"}'


async def _put(store, *keys):
    """Write each key with its sidecar, as the writer does."""
    for key in keys:
        await obstore.put_async(store, key, b"{}")
        await obstore.put_async(store, key + ".sha256", b"{}")


def _head_misses(missed: set[str]):
    """Patch HEAD to 404 for *missed* keys and hit the real store for the rest."""
    real_head = obstore.head_async

    async def head(store, key):
        if key in missed:
            raise FileNotFoundError(f"Object at location {key} not found")
        return await real_head(store, key)

    return patch.object(ops.obstore, "head_async", side_effect=head)


async def test_bodiless_head_misses_in_24_file_handoff_are_confirmed_by_listing():
    """Two false HEAD misses are confirmed by a real listing, which retries once each."""
    store = MemoryStore()
    refs = [_ref(f"entity-{i}") for i in range(24)]
    await _put(store, *(r.storage_path for r in refs))
    misses = {refs[3].storage_path, refs[17].storage_path}
    real_list = obstore.list
    seen: set[str] = set()
    calls = 0

    def listing(_store, *, prefix, **kwargs):
        nonlocal calls
        calls += 1
        if prefix in seen:
            return real_list(_store, prefix=prefix, **kwargs)
        seen.add(prefix)

        async def fail():
            raise RuntimeError(GATEWAY_404)
            yield []  # Make the stand-in an async listing stream.

        return fail()

    sleep = AsyncMock()
    with (
        _head_misses(misses),
        patch.object(ops.obstore, "list", side_effect=listing),
        patch.object(ops, "_retry_sleep", sleep),
    ):
        out = await _make_app(upstream=store).verify_refs(
            VerifyRefsInput(refs=refs, prefix=PREFIX, store=StoreTarget.UPSTREAM)
        )

    assert out.verified_count == 24
    assert out.verified_file_count == 24
    assert calls == 4  # one failed and one real listing per missed key
    assert sleep.await_count == 2


@pytest.mark.parametrize("extra", [None, ".backup", ".sha256"])
async def test_listing_must_confirm_the_exact_data_key(extra):
    """An empty folder, a similarly named key or a sidecar cannot prove presence."""
    ref = _ref("table")
    key = ref.storage_path
    store = MemoryStore()
    if extra:
        await obstore.put_async(store, key + extra, b"{}")

    with (
        _head_misses({key}),
        pytest.raises(StorageHandoffIncompleteError) as exc,
    ):
        await _make_app(upstream=store).verify_refs(
            VerifyRefsInput(refs=[ref], prefix=PREFIX, store=StoreTarget.UPSTREAM)
        )

    assert exc.value.missing_keys == [key]
    assert exc.value.declared_count == 1
    assert exc.value.effective_retryable is False


async def test_persistent_listing_gateway_error_remains_retryable():
    """A failed listing is not evidence that the declared object is absent."""
    ref = _ref("table")

    async def listing(_store, *, prefix):
        raise RuntimeError(GATEWAY_404)
        yield []  # Make the stand-in an async listing stream.

    sleep = AsyncMock()
    with (
        patch.object(ops.obstore, "head_async", side_effect=RuntimeError(HEAD_404)),
        patch.object(ops.obstore, "list", side_effect=listing) as lists,
        patch.object(ops, "_retry_sleep", sleep),
        pytest.raises(StorageGatewayNoRouteError) as exc,
    ):
        await _make_app().verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

    assert exc.value.effective_retryable is True
    assert lists.call_count == len(ops._GATEWAY_NO_ROUTE_RETRY_DELAYS) + 1
    assert sleep.await_count == len(ops._GATEWAY_NO_ROUTE_RETRY_DELAYS)


async def test_successful_head_does_not_list():
    """Keep the normal successful path at one request per file."""
    with (
        patch.object(
            ops.obstore, "head_async", new=AsyncMock(return_value={"size": 10})
        ),
        patch.object(ops.obstore, "list", new=MagicMock()) as listing,
    ):
        out = await _make_app().verify_refs(
            VerifyRefsInput(refs=[_ref("table")], prefix=PREFIX)
        )

    assert out.verified_count == 1
    listing.assert_not_called()


async def test_root_level_head_miss_lists_the_bucket_root():
    """A key with no parent segment is confirmed under the empty prefix, not "/"."""
    ref = FileReference(
        local_path="/tmp/leaf.json",
        storage_path="leaf.json",
        is_durable=True,
        file_count=1,
    )
    store = MemoryStore()
    await _put(store, "leaf.json")

    with _head_misses({"leaf.json"}):
        out = await _make_app(upstream=store).verify_refs(
            VerifyRefsInput(refs=[ref], prefix="", store=StoreTarget.UPSTREAM)
        )

    assert out.verified_count == 1


async def test_sibling_head_misses_share_one_parent_listing():
    """Every ref under one folder missing HEAD lists that folder once, not once per ref."""
    refs = [
        FileReference(
            local_path=f"/tmp/transformed/table/chunk-{i}.json",
            storage_path=f"{PREFIX}/table/chunk-{i}.json",
            is_durable=True,
            file_count=1,
        )
        for i in range(50)
    ]
    store = MemoryStore()
    await _put(store, *(r.storage_path for r in refs))
    real_list = obstore.list

    with (
        _head_misses({r.storage_path for r in refs}),
        patch.object(ops.obstore, "list", side_effect=real_list) as listing,
    ):
        out = await _make_app(upstream=store).verify_refs(
            VerifyRefsInput(refs=refs, prefix=PREFIX, store=StoreTarget.UPSTREAM)
        )

    assert out.verified_count == 50
    assert listing.call_count == 1
