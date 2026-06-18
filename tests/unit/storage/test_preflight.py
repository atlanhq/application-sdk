"""Unit tests for the SDR boot-time object-store access preflight.

Tests ``_classify_access_error``, ``_probe_store``, and ``verify_object_store_access``
in isolation using synchronous fakes — no network, no obstore binary required.
The integration tests in ``tests/integration/storage/test_emulator_preflight.py``
cover the real obstore round-trip against MinIO.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.storage.preflight import (
    _classify_access_error,
    _probe_store,
    verify_object_store_access,
)

# ---------------------------------------------------------------------------
# _classify_access_error — parametrised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected_class",
    [
        # 403 HTTP status — word-boundary match
        ("HTTP 403 Forbidden", "permission denied"),
        # Ensure bare "403" in isolation still matches
        ("status code 403", "permission denied"),
        # Must NOT match "2403" or "4030" (false positive guard)
        ("request-id x-amz-request-id: 1234030", "connectivity / unknown"),
        # Named strings — 403 bucket
        ("AccessDenied: Access Denied", "permission denied"),
        ("access denied to resource", "permission denied"),
        ("Forbidden resource", "permission denied"),
        ("not authorized to perform this action", "permission denied"),
        ("authorization failed", "permission denied"),
        # 401 HTTP status — word-boundary match
        ("HTTP 401 Unauthorized", "invalid credentials"),
        # Ensure bare "401" in isolation still matches
        ("error code 401", "invalid credentials"),
        # Must NOT match "1401" or "4010" (false positive guard)
        ("byte count 14010", "connectivity / unknown"),
        # Named strings — 401 bucket
        (
            "InvalidAccessKeyId: The AWS Access Key Id you provided does not exist",
            "invalid credentials",
        ),
        ("invalid access key supplied", "invalid credentials"),
        ("SignatureDoesNotMatch: signature mismatch", "invalid credentials"),
        ("unauthenticated request", "invalid credentials"),
        ("invalid credentials provided", "invalid credentials"),
        # Connectivity fallback
        ("connection refused", "connectivity / unknown"),
        ("no route to host", "connectivity / unknown"),
        ("bucket does not exist", "connectivity / unknown"),
        ("name or service not known", "connectivity / unknown"),
    ],
)
def test_classify_access_error(message: str, expected_class: str) -> None:
    exc = Exception(message)
    error_class, hint = _classify_access_error(exc)
    assert (
        error_class == expected_class
    ), f"For message={message!r}: expected {expected_class!r} but got {error_class!r}"
    assert isinstance(hint, str) and len(hint) > 0


def test_classify_access_error_returns_hint_for_each_class() -> None:
    """All three classifiers return a non-empty hint."""
    for msg, expected in [
        ("403 Forbidden", "permission denied"),
        ("401 Unauthorized", "invalid credentials"),
        ("timeout", "connectivity / unknown"),
    ]:
        _, hint = _classify_access_error(Exception(msg))
        assert hint, f"Empty hint for class={expected!r}"


# ---------------------------------------------------------------------------
# _probe_store — fake store using AsyncMock
# ---------------------------------------------------------------------------


def _fake_store() -> Any:
    """Return a minimal fake store object (value unused by _probe_store directly)."""
    return MagicMock()


async def _run_probe(
    store: Any, put_side_effect=None, head_side_effect=None, delete_side_effect=None
) -> str | None:
    """Run ``_probe_store`` with obstore functions replaced by async fakes."""
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock(side_effect=put_side_effect)
    fake_obstore.head_async = AsyncMock(side_effect=head_side_effect)
    fake_obstore.delete_async = AsyncMock(side_effect=delete_side_effect)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        return await _probe_store(store, "deployment", "objectstore")


@pytest.mark.asyncio
async def test_probe_store_success() -> None:
    """All three operations succeed → None returned."""
    result = await _run_probe(_fake_store())
    assert result is None


@pytest.mark.asyncio
async def test_probe_store_write_fails_permission_denied() -> None:
    """Write raises 403 → failure message mentioning 'permission denied'."""
    result = await _run_probe(
        _fake_store(),
        put_side_effect=Exception("403 Forbidden"),
    )
    assert result is not None
    assert "write failed" in result
    assert "permission denied" in result
    assert "objectstore" in result


@pytest.mark.asyncio
async def test_probe_store_write_fails_invalid_credentials() -> None:
    """Write raises 401 → failure message mentioning 'invalid credentials'."""
    result = await _run_probe(
        _fake_store(),
        put_side_effect=Exception("401 InvalidAccessKeyId"),
    )
    assert result is not None
    assert "invalid credentials" in result


@pytest.mark.asyncio
async def test_probe_store_head_fails_after_write_succeeds() -> None:
    """Write succeeds, HEAD fails → read failure returned; delete still attempted."""
    result = await _run_probe(
        _fake_store(),
        put_side_effect=None,
        head_side_effect=Exception("403 Forbidden"),
        delete_side_effect=None,
    )
    assert result is not None
    assert "read/head failed" in result
    assert "permission denied" in result


@pytest.mark.asyncio
async def test_probe_store_delete_fails_is_nonfatal() -> None:
    """Write and HEAD succeed; delete fails → None returned (best-effort cleanup)."""
    result = await _run_probe(
        _fake_store(),
        put_side_effect=None,
        head_side_effect=None,
        delete_side_effect=Exception("403 Forbidden on delete"),
    )
    # Delete failure is logged at WARNING but must not surface as a probe failure
    assert result is None


# ---------------------------------------------------------------------------
# verify_object_store_access — integration over the full function
# ---------------------------------------------------------------------------


def _make_infra(*, storage=None, upstream_storage=None):
    """Build a minimal InfrastructureContext-like object."""
    infra = MagicMock()
    infra.storage = storage
    infra.upstream_storage = upstream_storage
    return infra


@pytest.mark.asyncio
async def test_verify_noop_when_sdr_disabled(monkeypatch) -> None:
    """ENABLE_ATLAN_UPLOAD=False → function returns immediately without probing."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", False)

    # storage=None would explode if any probe was attempted
    infra = _make_infra(storage=None, upstream_storage=None)
    await verify_object_store_access(infra)  # must not raise


@pytest.mark.asyncio
async def test_verify_fails_when_upstream_absent_in_sdr(monkeypatch) -> None:
    """SDR mode + upstream_storage=None → ObjectStorePreflightError mentioning upstream.

    Defense-in-depth check: the primary guard is the binding factory raising
    ``StorageBindingNotFoundError`` at construction time (``required=True``);
    this catches any caller that bypasses the factory and passes
    ``upstream_storage=None`` directly.
    """
    import application_sdk.constants as constants_mod
    from application_sdk.storage.errors import ObjectStorePreflightError

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_store = _fake_store()
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    fake_obstore.delete_async = AsyncMock()

    infra = _make_infra(storage=fake_store, upstream_storage=None)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        with pytest.raises(ObjectStorePreflightError) as exc_info:
            await verify_object_store_access(infra)

    err = exc_info.value
    assert err.failure_count >= 1
    msg = str(err)
    assert "upstream" in msg
    assert "ENABLE_ATLAN_UPLOAD" in msg


@pytest.mark.asyncio
async def test_verify_fails_when_store_is_none_in_sdr(monkeypatch) -> None:
    """SDR mode + deployment store is None → ObjectStorePreflightError."""
    import application_sdk.constants as constants_mod
    from application_sdk.storage.errors import ObjectStorePreflightError

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    # Upstream present but deployment is None
    fake_store = _fake_store()
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    fake_obstore.delete_async = AsyncMock()

    infra = _make_infra(storage=None, upstream_storage=fake_store)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        with pytest.raises(ObjectStorePreflightError) as exc_info:
            await verify_object_store_access(infra)

    err = exc_info.value
    assert err.failure_count >= 1
    assert "store is None" in str(err)


@pytest.mark.asyncio
async def test_verify_passes_both_stores_healthy(monkeypatch) -> None:
    """SDR mode + both stores respond → no error raised."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_store = _fake_store()
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    fake_obstore.delete_async = AsyncMock()

    infra = _make_infra(storage=fake_store, upstream_storage=fake_store)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        await verify_object_store_access(infra)  # must not raise


@pytest.mark.asyncio
async def test_verify_timeout_classified_as_connectivity(monkeypatch) -> None:
    """A probe that times out is reported as 'connectivity / unknown'."""
    import application_sdk.constants as constants_mod
    from application_sdk.storage.errors import ObjectStorePreflightError

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    # Patch _probe_store to block indefinitely; asyncio.wait_for will fire TimeoutError
    async def _stall(*args, **kwargs):
        await asyncio.sleep(9999)

    fake_upstream = _fake_store()
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    fake_obstore.delete_async = AsyncMock()

    infra = _make_infra(storage=_fake_store(), upstream_storage=fake_upstream)

    with (
        patch("application_sdk.storage.preflight._probe_store", side_effect=_stall),
        patch("application_sdk.storage.preflight._PROBE_TIMEOUT_SECS", 0.01),
        pytest.raises(ObjectStorePreflightError) as exc_info,
    ):
        await verify_object_store_access(infra)

    msg = str(exc_info.value).lower()
    assert "timed out" in msg or "connectivity" in msg


@pytest.mark.asyncio
async def test_verify_probe_failure_propagates_to_preflight_error(monkeypatch) -> None:
    """A non-None failure string from _probe_store propagates into ObjectStorePreflightError."""
    import application_sdk.constants as constants_mod
    from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME
    from application_sdk.storage.errors import ObjectStorePreflightError

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_store = _fake_store()
    fake_obstore = MagicMock()
    # put_async raises 403 → _probe_store returns a non-None failure string
    fake_obstore.put_async = AsyncMock(side_effect=Exception("403 Forbidden"))
    fake_obstore.head_async = AsyncMock()
    fake_obstore.delete_async = AsyncMock()

    infra = _make_infra(storage=fake_store, upstream_storage=fake_store)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        with pytest.raises(ObjectStorePreflightError) as exc_info:
            await verify_object_store_access(infra)

    msg = str(exc_info.value)
    assert "write failed" in msg
    assert "permission denied" in msg
    assert DEPLOYMENT_OBJECT_STORE_NAME in msg
