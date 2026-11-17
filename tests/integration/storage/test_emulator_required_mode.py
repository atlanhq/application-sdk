"""The storage guards fail in required mode and skip otherwise.

``emulator_unavailable`` fails under STORAGE_EMULATOR_REQUIRED=1 and
``cloud_unavailable`` under STORAGE_CLOUD_REQUIRED=1. Marked ``storage_emulator``
so it runs on every integration-tier run: with healthy emulators and a correctly
wired cloud job no other test reaches the failing branch, so without this a
regression back to skipping in required mode would go unnoticed.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from tests.integration.storage import conftest

pytestmark = pytest.mark.storage_emulator


def _assert_fails(call: Callable[[], object], match: str) -> None:
    """Assert *call* fails the test, and a regression to skipping shows red.

    ``pytest.raises(pytest.fail.Exception)`` lets a ``pytest.skip`` escape, which
    marks this test skipped (green) exactly when the guard has regressed. Catch
    every outcome and check its type instead.
    """
    with pytest.raises(BaseException, match=match) as exc_info:
        call()
    assert exc_info.type is pytest.fail.Exception, (
        f"expected a failure, got {exc_info.type.__name__}: {exc_info.value}"
    )


def test_required_mode_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_EMULATOR_REQUIRED", True)
    _assert_fails(
        lambda: conftest.emulator_unavailable("MinIO not reachable"),
        "STORAGE_EMULATOR_REQUIRED=1",
    )


def test_local_mode_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_EMULATOR_REQUIRED", False)
    with pytest.raises(pytest.skip.Exception, match="MinIO not reachable"):
        conftest.emulator_unavailable("MinIO not reachable")


def test_cloud_required_mode_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_CLOUD_REQUIRED", True)
    _assert_fails(
        lambda: conftest.cloud_unavailable(
            "Real AWS access required. Missing: S3_BUCKET"
        ),
        "STORAGE_CLOUD_REQUIRED=1",
    )


def test_cloud_local_mode_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_CLOUD_REQUIRED", False)
    with pytest.raises(pytest.skip.Exception, match="Missing: S3_BUCKET"):
        conftest.cloud_unavailable("Real AWS access required. Missing: S3_BUCKET")


def test_require_s3_guard_fails_in_required_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``require_s3`` guard routes through ``cloud_unavailable``.

    Calls the fixture body with a stand-in request whose node carries the
    ``s3_integration`` marker, so the guard's failing branch runs here without
    cloud access instead of only in the cloud job, where it never takes it.
    """
    monkeypatch.setattr(conftest, "_CLOUD_REQUIRED", True)
    monkeypatch.setattr(conftest, "S3_BUCKET", "")
    request = SimpleNamespace(
        node=SimpleNamespace(
            get_closest_marker=lambda name: (
                object() if name == "s3_integration" else None
            )
        )
    )
    _assert_fails(
        lambda: conftest.require_s3.__wrapped__(request), "Missing: S3_BUCKET"
    )
