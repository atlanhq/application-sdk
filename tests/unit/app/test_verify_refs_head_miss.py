"""Confirm a HEAD miss through the body-bearing listing before rejecting a handoff."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from obstore.store import MemoryStore

from application_sdk.contracts.storage import VerifyRefsInput
from application_sdk.contracts.types import StoreTarget
from application_sdk.storage import ops
from application_sdk.storage.errors import (
    StorageGatewayNoRouteError,
    StorageHandoffIncompleteError,
)
from tests.unit.app.test_verify_refs import PREFIX, _make_app, _ref

HEAD_404 = "Server returned non-2xx status code: 404 Not Found"
GATEWAY_404 = HEAD_404 + ': {"message":"no Route matched with those values"}'


async def test_bodiless_head_misses_in_24_file_handoff_are_confirmed_by_listing():
    """Replay 22 successful HEADs and two false misses, including a listing retry."""
    store = MemoryStore()
    app = _make_app(upstream=store)
    refs = [_ref(f"entity-{i}") for i in range(24)]
    misses = {refs[3].storage_path, refs[17].storage_path}
    attempts: dict[str, int] = {}

    async def head(_store, key):
        assert _store is store
        if key in misses:
            raise RuntimeError(HEAD_404)
        return {"size": 10}

    async def listing(_store, *, prefix):
        assert _store is store
        assert prefix in misses
        attempts[prefix] = attempts.get(prefix, 0) + 1
        if attempts[prefix] == 1:
            raise RuntimeError(GATEWAY_404)
        yield [{"path": prefix, "size": 10}]

    sleep = AsyncMock()
    with (
        patch.object(
            ops.obstore, "head_async", new_callable=AsyncMock, side_effect=head
        ) as heads,
        patch.object(ops.obstore, "list", side_effect=listing),
        patch.object(ops, "_retry_sleep", sleep),
    ):
        out = await app.verify_refs(
            VerifyRefsInput(refs=refs, prefix=PREFIX, store=StoreTarget.UPSTREAM)
        )

    assert out.verified_count == 24
    assert out.verified_file_count == 24
    assert heads.await_count == 24
    assert attempts == dict.fromkeys(misses, 2)
    assert sleep.await_count == 2


@pytest.mark.parametrize("listed", [[], ["sibling"], ["sidecar"]])
async def test_listing_must_confirm_the_exact_data_key(listed):
    """An empty listing, similarly named key or sidecar cannot prove presence."""
    ref = _ref("table")
    key = ref.storage_path
    paths = {"sibling": f"{key}.backup", "sidecar": f"{key}.sha256"}

    async def listing(_store, *, prefix):
        assert prefix == key
        yield [{"path": paths[item], "size": 10} for item in listed]

    with (
        patch.object(ops.obstore, "head_async", side_effect=RuntimeError(HEAD_404)),
        patch.object(ops.obstore, "list", side_effect=listing) as lists,
        pytest.raises(StorageHandoffIncompleteError) as exc,
    ):
        await _make_app().verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

    assert exc.value.missing_keys == [key]
    assert exc.value.declared_count == 1
    assert exc.value.effective_retryable is False
    lists.assert_called_once()


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
