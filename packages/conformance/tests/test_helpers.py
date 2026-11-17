"""Tests for the shared test helpers in ``tests/_helpers.py``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._helpers import active_ids, find_sdk_root, ids, sdk_root


@dataclass(frozen=True)
class _Finding:
    rule_id: str
    suppressed: bool = False


def test_ids_keeps_suppressed_and_active_ids_drops_them() -> None:
    findings = [_Finding("A001"), _Finding("B002", suppressed=True), _Finding("A001")]
    assert ids(findings) == ["A001", "B002", "A001"]
    assert active_ids(findings) == ["A001", "A001"]


def test_sdk_root_found_from_this_checkout() -> None:
    root = find_sdk_root()
    assert root is not None
    assert (root / "packages" / "conformance" / "tests" / "_helpers.py").is_file()
    assert sdk_root() == root


def _outside_monorepo(tmp_path: Path) -> Path:
    start = tmp_path / "packages" / "conformance" / "tests" / "_helpers.py"
    start.parent.mkdir(parents=True)
    start.touch()
    return start


def test_sdk_root_fails_under_ci_when_layout_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drift guard must not skip in CI: the miss is the layout change it guards."""
    start = _outside_monorepo(tmp_path)
    assert find_sdk_root(start) is None
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="layout changed"):
        sdk_root(start)


def test_sdk_root_skips_outside_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = _outside_monorepo(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception, match="monorepo root not found"):
        sdk_root(start)


def test_find_sdk_root_accepts_the_root_itself() -> None:
    root = find_sdk_root()
    assert root is not None
    assert find_sdk_root(root) == root
