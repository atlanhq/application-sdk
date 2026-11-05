"""Disk-full routing follows who owns the worker's local volume.

A hosted worker's volume is the platform's to resize; a Self-Deployed Runtime
(SDR) agent's volume is on the customer's host. The owner is carried by the
error's class, so every reader that takes ``type(exc).audience`` (the Temporal
failure log, the preflight outcome) agrees with the failure envelope.
"""

import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from application_sdk import constants
from application_sdk.common.atomic import disk_full_guard, ensure_free_space
from application_sdk.errors import DiskFullError, SdrDiskFullError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.execution._temporal.interceptors.log import (
    _extract_failure_attrs,
)
from application_sdk.infrastructure import context as infra_context
from application_sdk.infrastructure.context import InfrastructureContext
from application_sdk.storage import StorageDiskFullError, StorageError
from application_sdk.storage.errors import SdrStorageDiskFullError


@pytest.fixture(autouse=True)
def _no_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no infrastructure context, whatever ran before."""
    monkeypatch.setattr(infra_context, "_infrastructure", None)


def _set_stores(monkeypatch: pytest.MonkeyPatch, *, single: bool) -> None:
    deployment = MagicMock(name="deployment_store")
    upstream = deployment if single else MagicMock(name="upstream_store")
    monkeypatch.setattr(
        infra_context,
        "_infrastructure",
        InfrastructureContext(storage=deployment, upstream_storage=upstream),
    )


def _raise_short_write(target: Path) -> DiskFullError:
    with patch("application_sdk.common.atomic._free_bytes", return_value=548864):
        with pytest.raises(DiskFullError) as caught:
            ensure_free_space(target, 770970, operation="parquet chunk write")
    return caught.value


def test_an_sdr_worker_short_of_space_routes_the_failure_to_the_customer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", True)
    target = tmp_path / "output.parquet"

    error = _raise_short_write(target)

    assert isinstance(error, SdrDiskFullError)
    assert type(error).audience is Audience.USER
    details = error.to_failure_details()
    assert details.audience is Audience.USER
    assert details.category is FailureCategory.RESOURCE_EXHAUSTED
    assert details.code == "RESOURCE_EXHAUSTED_DISK_FULL"
    assert details.retryable is True
    assert details.evidence["required_bytes"] == 770970
    assert details.evidence["free_bytes"] == 548864
    action = details.suggested_action or ""
    assert "host volume" in action
    assert "ATLAN_TEMPORARY_PATH" in action
    assert "ephemeral-storage" not in action
    assert not target.exists()


def test_an_sdr_worker_with_a_separate_upstream_store_routes_to_the_customer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", True)
    _set_stores(monkeypatch, single=False)

    error = _raise_short_write(tmp_path / "output.parquet")

    assert type(error) is SdrDiskFullError


def test_the_upload_flag_on_a_single_store_worker_keeps_platform_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In-cluster wiring aliases one store under both names; startup only warns
    when ENABLE_ATLAN_UPLOAD is also set, so the volume is still the platform's."""
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", True)
    _set_stores(monkeypatch, single=True)

    error = _raise_short_write(tmp_path / "output.parquet")

    assert type(error) is DiskFullError
    assert error.to_failure_details().audience is Audience.PLATFORM
    assert "ephemeral-storage request/limit" in (error.suggested_action or "")


def test_the_failure_log_names_the_same_owner_as_the_envelope_on_an_sdr_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", True)

    error = _raise_short_write(tmp_path / "output.parquet")

    assert _extract_failure_attrs(error)["failure.audience"] == Audience.USER.value


def test_an_sdr_write_that_runs_out_mid_write_routes_to_the_customer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", True)

    with pytest.raises(SdrDiskFullError) as caught:
        with disk_full_guard(tmp_path / "out.json", operation="marker write"):
            raise OSError(errno.ENOSPC, "No space left on device")

    assert type(caught.value).audience is Audience.USER


def test_a_hosted_worker_short_of_space_keeps_platform_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", False)

    error = _raise_short_write(tmp_path / "output.parquet")

    assert type(error) is DiskFullError
    assert error.to_failure_details().audience is Audience.PLATFORM
    assert "ephemeral-storage request/limit" in (error.suggested_action or "")


def test_a_storage_rewrap_of_an_sdr_disk_full_keeps_the_customer_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", True)
    error = _raise_short_write(tmp_path / "output.parquet")

    wrapped = StorageDiskFullError.from_disk_full(error, key="artifacts/output")

    assert isinstance(wrapped, SdrStorageDiskFullError)
    assert isinstance(wrapped, StorageError)
    assert type(wrapped).audience is Audience.USER
    assert wrapped.to_failure_details().audience is Audience.USER
    assert wrapped.key == "artifacts/output"


def test_a_storage_rewrap_of_a_hosted_disk_full_stays_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(constants, "ENABLE_ATLAN_UPLOAD", False)
    error = _raise_short_write(tmp_path / "output.parquet")

    wrapped = StorageDiskFullError.from_disk_full(error, key="artifacts/output")

    assert type(wrapped) is StorageDiskFullError
    assert type(wrapped).audience is Audience.PLATFORM
