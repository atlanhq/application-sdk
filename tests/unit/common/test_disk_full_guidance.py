"""Disk-full remediation follows ownership of the worker's local volume."""

from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.common.atomic import ensure_free_space
from application_sdk.errors import DiskFullError
from application_sdk.errors.categories import Audience, FailureCategory


def test_sdr_disk_full_routes_to_customer_with_volume_and_shortfall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENABLE_ATLAN_UPLOAD", "true")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "test-agent")
    monkeypatch.setenv("ATLAN_TEMPORARY_PATH", str(tmp_path))
    target = tmp_path / "output.parquet"

    with patch("application_sdk.common.atomic._free_bytes", return_value=548864):
        with pytest.raises(DiskFullError) as caught:
            ensure_free_space(target, 770970, operation="parquet chunk write")

    error = caught.value
    assert error.audience is Audience.USER
    details = error.to_failure_details()
    assert details.audience is Audience.USER
    assert details.category is FailureCategory.RESOURCE_EXHAUSTED
    assert details.code == "RESOURCE_EXHAUSTED_DISK_FULL"
    assert details.retryable is True
    assert details.evidence["path"] == str(target)
    assert details.evidence["required_bytes"] == 770970
    assert details.evidence["free_bytes"] == 548864
    action = details.suggested_action or ""
    assert "agent" in action
    assert "host volume" in action
    assert "ATLAN_TEMPORARY_PATH" in action
    assert str(tmp_path) in action
    assert "770970 bytes" in action
    assert "548864 bytes" in action
    assert "ephemeral-storage" not in action
    assert not target.exists()


def test_sdr_disk_full_without_measurements_has_actionable_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ATLAN_UPLOAD", "true")
    monkeypatch.delenv("ATLAN_TEMPORARY_PATH", raising=False)
    error = DiskFullError(message="disk quota exceeded", path="./local/tmp/output")
    details = error.to_failure_details()
    assert details.audience is Audience.USER
    assert "host volume" in (details.suggested_action or "")
    assert "./local/tmp/" in (details.suggested_action or "")
    assert "None" not in (details.suggested_action or "")


@pytest.mark.parametrize("sdr_mode", ["false", ""])
def test_deployment_name_alone_keeps_platform_disk_guidance(
    monkeypatch: pytest.MonkeyPatch, sdr_mode: str
) -> None:
    monkeypatch.setenv("ENABLE_ATLAN_UPLOAD", sdr_mode)
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "hosted-worker")
    error = DiskFullError(message="disk full")
    assert error.audience is Audience.PLATFORM
    details = error.to_failure_details()
    assert details.audience is Audience.PLATFORM
    assert "ephemeral-storage request/limit" in (details.suggested_action or "")


def test_sdr_disk_full_preserves_explicit_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ATLAN_UPLOAD", "true")
    error = DiskFullError(message="disk full", suggested_action="Check the disk quota.")
    assert error.to_failure_details().audience is Audience.USER
    assert error.suggested_action == "Check the disk quota."
