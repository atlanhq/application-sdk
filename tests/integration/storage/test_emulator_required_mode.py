"""``emulator_unavailable`` fails under STORAGE_EMULATOR_REQUIRED=1, else skips.

Marked ``storage_emulator`` so it runs in the CI job that sets the variable:
with healthy sidecars no other test reaches the failing branch, so without this
a regression back to skipping in required mode would go unnoticed.
"""

from __future__ import annotations

import pytest

from tests.integration.storage import conftest

pytestmark = pytest.mark.storage_emulator


def test_required_mode_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_EMULATOR_REQUIRED", True)
    with pytest.raises(pytest.fail.Exception, match="STORAGE_EMULATOR_REQUIRED=1"):
        conftest.emulator_unavailable("MinIO not reachable")


def test_local_mode_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_EMULATOR_REQUIRED", False)
    with pytest.raises(pytest.skip.Exception, match="MinIO not reachable"):
        conftest.emulator_unavailable("MinIO not reachable")
