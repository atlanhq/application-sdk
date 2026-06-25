"""Drift guard for the contract ledger — the forcing function for B006.

The committed ``conformance/data/contract_schema.lock.json`` is what lets B005
detect field removals and type changes against the last-known-good state of
every entrypoint contract.  For that to mean anything, the committed file must
always equal a fresh regeneration of the current source.

This test makes a new entrypoint field fail CI until the ledger is regenerated
in the same PR:

    uv run atlan-application-sdk-conformance gen-contract-ledger

The SDK itself has no entrypoint contracts (it is a library), so the committed
ledger is empty — but it still must exist, be valid JSON, and match what the
generator would produce from the SDK source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.deprecation._ledger_schema import (
    LEDGER_PATH,
    load_ledger,
    serialize,
)
from conformance.tools.generate_contract_ledger import build_ledger


def _find_repo_root() -> Path | None:
    """Locate the repo root containing ``pyproject.toml`` from this test file."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def test_ledger_is_committed() -> None:
    """The ledger file exists and parses without error."""
    assert LEDGER_PATH.is_file(), (
        f"{LEDGER_PATH} is missing — run "
        "`uv run atlan-application-sdk-conformance gen-contract-ledger`."
    )
    ledger = load_ledger()
    # Empty ledger is valid; SDK has no app entrypoints
    assert isinstance(ledger.fields, list)


def test_committed_ledger_matches_fresh_scan() -> None:
    """The committed ledger equals a fresh scan of the repo.

    If this fails, an entrypoint contract was added or changed without
    regenerating the ledger.  Run ``gen-contract-ledger`` and commit the result
    in the same PR.
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        pytest.skip("pyproject.toml not found alongside the conformance package")

    existing = load_ledger()
    fresh = build_ledger(repo_root, existing)
    on_disk = LEDGER_PATH.read_text(encoding="utf-8")
    assert on_disk == serialize(fresh), (
        "contract_schema.lock.json is stale — regenerate with "
        "`uv run atlan-application-sdk-conformance gen-contract-ledger` and commit it."
    )
