"""Drift guard for the contract-toolkit baseline — the forcing function for K007/K008.

The committed ``conformance/data/toolkit_baseline.json`` is what lets K007 (version
floor) and K008 (source provenance) run offline inside a consumer app repo, which
never has the SDK's ``contract-toolkit/src/PklProject`` on disk. For that to mean
anything the committed file must always equal a fresh read of the toolkit source —
otherwise the fleet would be graded against a stale "latest version".

This test makes a toolkit version/baseUri bump fail CI until the baseline is
regenerated in the same PR:

    uv run atlan-application-sdk-conformance gen-toolkit-baseline
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks._toolkit_baseline import (
    BASELINE_PATH,
    TOOLKIT_PKLPROJECT_RELPATH,
    build_baseline,
    load_baseline,
    serialize,
)


def _find_sdk_root() -> Path | None:
    """Locate the repo root containing contract-toolkit/src/PklProject."""
    for parent in Path(__file__).resolve().parents:
        if parent.joinpath(*TOOLKIT_PKLPROJECT_RELPATH).is_file():
            return parent
    return None


def test_baseline_is_committed() -> None:
    """The baseline file exists, parses, and carries both fields."""
    assert BASELINE_PATH.is_file(), (
        f"{BASELINE_PATH} is missing — run "
        "`uv run atlan-application-sdk-conformance gen-toolkit-baseline`."
    )
    baseline = load_baseline()
    assert baseline is not None
    assert baseline.canonical_base
    assert baseline.latest_version


def test_committed_baseline_matches_fresh_read() -> None:
    """The committed baseline equals a fresh read of contract-toolkit/src/PklProject.

    If this fails, the toolkit's version or baseUri changed but the baseline was
    not regenerated. Run ``gen-toolkit-baseline`` and commit the result.
    """
    sdk_root = _find_sdk_root()
    if sdk_root is None:
        pytest.skip("contract-toolkit/src/PklProject not found alongside the package")

    fresh = build_baseline(sdk_root)
    on_disk = BASELINE_PATH.read_text(encoding="utf-8")
    assert on_disk == serialize(fresh), (
        "toolkit_baseline.json is stale — regenerate with "
        "`uv run atlan-application-sdk-conformance gen-toolkit-baseline` and commit it."
    )
