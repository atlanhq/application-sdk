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
    ObjectStoreCheckResult,
    _classify_access_error,
    _probe_store,
    check_object_store_access,
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
    store: Any, put_side_effect=None, head_side_effect=None
) -> str | None:
    """Run ``_probe_store`` with obstore functions replaced by async fakes."""
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock(side_effect=put_side_effect)
    fake_obstore.head_async = AsyncMock(side_effect=head_side_effect)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        return await _probe_store(store, "deployment", "objectstore")


@pytest.mark.asyncio
async def test_probe_store_success() -> None:
    """Write and read both succeed → None returned."""
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
    """Write succeeds, HEAD fails → read failure returned."""
    result = await _run_probe(
        _fake_store(),
        put_side_effect=None,
        head_side_effect=Exception("403 Forbidden"),
    )
    assert result is not None
    assert "read/head failed" in result
    assert "permission denied" in result


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

    infra = _make_infra(storage=fake_store, upstream_storage=fake_store)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        with pytest.raises(ObjectStorePreflightError) as exc_info:
            await verify_object_store_access(infra)

    msg = str(exc_info.value)
    assert "write failed" in msg
    assert "permission denied" in msg
    assert DEPLOYMENT_OBJECT_STORE_NAME in msg


# ---------------------------------------------------------------------------
# check_object_store_access — non-raising structured probe (interactive path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_returns_empty_when_sdr_disabled(monkeypatch) -> None:
    """ENABLE_ATLAN_UPLOAD=False → [] with no probing."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", False)

    infra = _make_infra(storage=_fake_store(), upstream_storage=_fake_store())
    assert await check_object_store_access(infra) == []


@pytest.mark.asyncio
async def test_check_returns_empty_when_infra_none(monkeypatch) -> None:
    """infra=None → [] even in SDR mode."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)
    assert await check_object_store_access(None) == []


@pytest.mark.asyncio
async def test_check_success_both_stores(monkeypatch) -> None:
    """Both stores healthy → one passed result each, no error fields."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    infra = _make_infra(storage=_fake_store(), upstream_storage=_fake_store())

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_object_store_access(infra)

    assert len(results) == 2
    labels = [r.label for r in results]
    assert labels == ["deployment", "upstream"]
    assert all(r.passed for r in results)
    assert all(r.error_class is None and r.cause is None for r in results)


@pytest.mark.asyncio
async def test_check_deployment_only_when_no_upstream(monkeypatch) -> None:
    """Upstream absent → only the deployment store is probed (no hard-fail here)."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    infra = _make_infra(storage=_fake_store(), upstream_storage=None)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_object_store_access(infra)

    assert len(results) == 1
    assert results[0].label == "deployment"
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_check_write_failure_returns_failed_result(monkeypatch) -> None:
    """A write failure → failed result carrying class/cause/hint and a message."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock(side_effect=Exception("403 Forbidden"))
    fake_obstore.head_async = AsyncMock()
    infra = _make_infra(storage=_fake_store(), upstream_storage=None)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_object_store_access(infra)

    assert len(results) == 1
    result = results[0]
    assert result.passed is False
    assert result.error_class == "permission denied"
    assert result.cause is not None and "403" in result.cause
    assert result.hint
    assert "access check failed" in result.message


@pytest.mark.asyncio
async def test_check_head_failure_returns_failed_result(monkeypatch) -> None:
    """Write succeeds, HEAD fails → failed result flagged read/head."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock(side_effect=Exception("401 Unauthorized"))
    infra = _make_infra(storage=_fake_store(), upstream_storage=None)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_object_store_access(infra)

    assert results[0].passed is False
    assert results[0].failed_operation == "read/head"
    assert results[0].error_class == "invalid credentials"


@pytest.mark.asyncio
async def test_check_store_none_returns_not_configured(monkeypatch) -> None:
    """A None deployment store → a failed 'not configured' result, no raise."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    infra = _make_infra(storage=None, upstream_storage=None)
    results = await check_object_store_access(infra)

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].error_class == "not configured"


@pytest.mark.asyncio
async def test_check_timeout_returns_connectivity_failure(monkeypatch) -> None:
    """A probe that times out → a connectivity failure result, no raise."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    async def _stall(*args, **kwargs):
        await asyncio.sleep(9999)

    infra = _make_infra(storage=_fake_store(), upstream_storage=None)
    with (
        patch(
            "application_sdk.storage.preflight._probe_store_structured",
            side_effect=_stall,
        ),
        patch("application_sdk.storage.preflight._PROBE_TIMEOUT_SECS", 0.01),
    ):
        results = await check_object_store_access(infra)

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].error_class == "connectivity / unknown"
    assert "timed out" in (results[0].cause or "")


def test_object_store_check_result_message_passed() -> None:
    """A passed result renders a positive, single-line message."""
    result = ObjectStoreCheckResult(
        label="deployment", binding_name="objectstore", passed=True
    )
    assert "read/write access confirmed" in result.message


def test_boot_formatter_renders_structured_result() -> None:
    """The boot wrapper formats a structured result into the historical block."""
    import application_sdk.storage.preflight as preflight_mod

    result = ObjectStoreCheckResult(
        label="deployment",
        binding_name="objectstore",
        passed=False,
        error_class="permission denied",
        cause="403 Forbidden",
        hint="grant access",
        failed_operation="write",
    )
    formatted = preflight_mod._format_boot_failure(result)
    assert "write failed [permission denied]" in formatted
    assert "403 Forbidden" in formatted


# ---------------------------------------------------------------------------
# Relocation classification + multipart-forced probe
# ---------------------------------------------------------------------------

_RELOCATION_MESSAGE = (
    "Generic GCS error: Server returned non-2xx status code: 400 Bad Request: "
    "<Error><Code>PreconditionFailed</Code><Message>Invalid precondition during "
    "a custom dual-region, or multi-region bucket relocation.</Message></Error>"
)


@pytest.mark.parametrize(
    "message,expected_class",
    [
        # The production shape: both tokens present.
        (_RELOCATION_MESSAGE, "bucket relocation in progress"),
        # Reversed token order still matches.
        (
            "bucket relocation rejected the request: precondition failed",
            "bucket relocation in progress",
        ),
        # An etag/if-match 412 carries "precondition" alone → NOT relocation.
        (
            "412 Precondition Failed: at-match condition not met",
            "connectivity / unknown",
        ),
        # "relocation" alone (no precondition) → NOT relocation.
        ("object relocation pending", "connectivity / unknown"),
    ],
)
def test_classify_relocation(message: str, expected_class: str) -> None:
    error_class, hint = _classify_access_error(Exception(message))
    assert error_class == expected_class
    assert hint


async def _run_probe_structured(
    store: Any,
    *,
    include_multipart_probe: bool,
    put_side_effects=None,
    head_side_effect=None,
):
    """Run ``_probe_store_structured`` with obstore replaced by async fakes.

    ``put_side_effects`` is a list applied across successive ``put_async``
    calls (plain write first, multipart write second).
    """
    from application_sdk.storage.preflight import _probe_store_structured

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock(side_effect=put_side_effects)
    fake_obstore.head_async = AsyncMock(side_effect=head_side_effect)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        result = await _probe_store_structured(
            store,
            "deployment",
            "objectstore",
            include_multipart_probe=include_multipart_probe,
        )
    return result, fake_obstore


@pytest.mark.asyncio
async def test_probe_multipart_disabled_by_default_single_put() -> None:
    """Without the flag, exactly one plain put runs — boot path unchanged."""
    result, fake_obstore = await _run_probe_structured(
        _fake_store(), include_multipart_probe=False
    )
    assert result.passed is True
    assert fake_obstore.put_async.await_count == 1
    assert not any(
        c.kwargs.get("use_multipart") for c in fake_obstore.put_async.await_args_list
    )


@pytest.mark.asyncio
async def test_probe_multipart_forced_second_write() -> None:
    """With the flag, a second put runs with use_multipart=True."""
    result, fake_obstore = await _run_probe_structured(
        _fake_store(), include_multipart_probe=True
    )
    assert result.passed is True
    assert fake_obstore.put_async.await_count == 2
    multipart_call = fake_obstore.put_async.await_args_list[1]
    assert multipart_call.kwargs.get("use_multipart") is True
    assert multipart_call.kwargs.get("chunk_size") == 5 * 1024 * 1024


@pytest.mark.asyncio
async def test_probe_multipart_relocation_failure_classified() -> None:
    """Plain put passes, multipart initiate rejected → relocation bucket, write-multipart phase."""
    result, _ = await _run_probe_structured(
        _fake_store(),
        include_multipart_probe=True,
        put_side_effects=[None, Exception(_RELOCATION_MESSAGE)],
    )
    assert result.passed is False
    assert result.failed_operation == "write-multipart"
    assert result.error_class == "bucket relocation in progress"
    assert "relocation" in (result.hint or "")


@pytest.mark.asyncio
async def test_interactive_check_includes_multipart_probe(monkeypatch) -> None:
    """check_object_store_access catches a store that only rejects multipart."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock(
        side_effect=[None, Exception(_RELOCATION_MESSAGE)]
    )
    fake_obstore.head_async = AsyncMock()
    infra = _make_infra(storage=_fake_store(), upstream_storage=None)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_object_store_access(infra)

    assert results[0].passed is False
    assert results[0].error_class == "bucket relocation in progress"


# ---------------------------------------------------------------------------
# check_run_storage_access — the run-path (gate) probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_check_probes_without_sdr_mode(monkeypatch) -> None:
    """Unlike the SDR checker, the run-path checker probes in-cluster too."""
    import application_sdk.constants as constants_mod
    from application_sdk.storage.preflight import check_run_storage_access

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", False)

    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    infra = _make_infra(storage=_fake_store(), upstream_storage=None)

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_run_storage_access(infra)

    assert len(results) == 1
    assert results[0].label == "deployment"
    assert results[0].passed is True
    # The run-path probe must exercise the multipart API the uploads use.
    assert any(
        c.kwargs.get("use_multipart") for c in fake_obstore.put_async.await_args_list
    )


@pytest.mark.asyncio
async def test_run_check_empty_only_without_infra_context() -> None:
    """No infrastructure context at all → [] (unit tests, direct calls)."""
    from application_sdk.storage.preflight import check_run_storage_access

    assert await check_run_storage_access(None) == []


@pytest.mark.asyncio
async def test_run_check_relocation_failure(monkeypatch) -> None:
    """A relocating bucket fails the run-path check with the relocation bucket."""
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock(
        side_effect=[None, Exception(_RELOCATION_MESSAGE)]
    )
    fake_obstore.head_async = AsyncMock()
    infra = _make_infra(storage=_fake_store(), upstream_storage=None)

    from application_sdk.storage.preflight import check_run_storage_access

    with patch.dict("sys.modules", {"obstore": fake_obstore}):
        results = await check_run_storage_access(infra)

    assert results[0].passed is False
    assert results[0].error_class == "bucket relocation in progress"
    assert results[0].failed_operation == "write-multipart"


@pytest.mark.asyncio
async def test_run_check_timeout_bounded_by_budget() -> None:
    """A stalled probe is cut at the caller's budget and reported as connectivity."""
    from application_sdk.storage.preflight import check_run_storage_access

    async def _stall(*args, **kwargs):
        await asyncio.sleep(9999)

    infra = _make_infra(storage=_fake_store(), upstream_storage=None)
    with (
        patch(
            "application_sdk.storage.preflight._probe_store_structured",
            side_effect=_stall,
        ),
        patch("application_sdk.storage.preflight._RUN_PROBE_FLOOR_SECONDS", 0.01),
    ):
        results = await check_run_storage_access(infra, timeout_seconds=0.05)

    assert results[0].passed is False
    assert results[0].error_class == "connectivity / unknown"
    assert "timed out" in (results[0].cause or "")


@pytest.mark.asyncio
async def test_run_check_missing_deployment_store_is_a_failed_check() -> None:
    """A binding that failed to parse must fail the check, not vanish.

    Mirrors check_object_store_access: storage=None on a live infra context is
    a deployment config gap — the one case preflight should certainly catch —
    not an absence of stores to probe.
    """
    from application_sdk.storage.preflight import check_run_storage_access

    results = await check_run_storage_access(
        _make_infra(storage=None, upstream_storage=None)
    )
    assert len(results) == 1
    assert results[0].label == "deployment"
    assert results[0].passed is False
    assert results[0].error_class == "not configured"


@pytest.mark.asyncio
async def test_probe_writes_with_binding_put_attributes() -> None:
    """The probe must exercise the same put-attribute path real uploads use.

    A binding declaring e.g. a Storage-Class writes every artifact with it; a
    probe writing without attributes certifies a different code path (and lands
    its object in the wrong storage class).
    """
    from application_sdk.storage.preflight import _probe_store_structured

    attrs = {"Storage-Class": "STANDARD_IA"}
    fake_obstore = MagicMock()
    fake_obstore.put_async = AsyncMock()
    fake_obstore.head_async = AsyncMock()
    with (
        patch.dict("sys.modules", {"obstore": fake_obstore}),
        patch(
            "application_sdk.storage.ops._resolve_put_attributes",
            return_value=attrs,
        ),
    ):
        result = await _probe_store_structured(
            _fake_store(), "deployment", "objectstore", include_multipart_probe=True
        )
    assert result.passed is True
    assert fake_obstore.put_async.await_count == 2
    for call in fake_obstore.put_async.await_args_list:
        assert call.kwargs.get("attributes") == attrs


def test_relocation_error_exported_from_package_root() -> None:
    """StorageBucketRelocationError is importable like its eight siblings."""
    from application_sdk.storage import StorageBucketRelocationError  # noqa: F401


# ---------------------------------------------------------------------------
# Cause sanitisation
# ---------------------------------------------------------------------------

_SIGNED_URL_ERROR = (
    "Generic S3 error: error sending request for url "
    "(https://bucket.s3.amazonaws.com/artifacts/x?X-Amz-Signature=deadbeefcafe"
    "&X-Amz-Credential=AKIAEXAMPLE%2F20260831%2Fus-east-1%2Fs3%2Faws4_request)"
)


@pytest.mark.parametrize("failing_phase", ["write", "read/head", "write-multipart"])
@pytest.mark.asyncio
async def test_probe_cause_is_sanitised(failing_phase: str) -> None:
    """No probe may put a raw object-store error into user-facing text.

    ``ObjectStoreCheckResult.cause`` flows into ``.message``, which the boot
    formatter and the gate's ``_storage_failure_details`` both render for a
    human. Object-store errors carry the request URL, and for a presigned
    request that URL carries the signature — so every phase must route its
    cause through ``sanitize_cause_repr``.
    """
    exc = Exception(_SIGNED_URL_ERROR)
    puts: list = [None, None]
    head = None
    if failing_phase == "write":
        puts = [exc]
    elif failing_phase == "read/head":
        head = exc
    else:
        puts = [None, exc]

    result, _ = await _run_probe_structured(
        _fake_store(),
        include_multipart_probe=True,
        put_side_effects=puts,
        head_side_effect=head,
    )
    assert result.passed is False
    assert result.failed_operation == failing_phase
    assert result.cause is not None
    assert "X-Amz-Signature=deadbeefcafe" not in result.cause
    assert "X-Amz-Signature=***" in result.cause
    # The rendered, user-facing string is the thing that actually leaks.
    assert "deadbeefcafe" not in result.message
