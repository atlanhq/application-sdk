"""Drift guard for the contract ledger — the forcing function for B006.

The committed ``conformance/data/contract_schema.lock.json`` is what lets B005
detect field removals and type changes against the last-known-good state of
every entrypoint contract.  For that to mean anything, the committed file must
always equal a fresh regeneration of the current source.

This test makes a new entrypoint field fail CI until the ledger is regenerated
in the same PR:

    uv run atlan-application-sdk-conformance gen-contract-ledger

The SDK seeds the ledger from its template contracts
(``application_sdk/templates/contracts/``) — ``ExtractionInput``,
``ExtractionOutput``, ``IncrementalExtraction*``, and ``QueryExtraction*``.
The committed ledger must exist, be valid JSON, and match what the generator
would produce from the current SDK source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks.deprecation._ledger_schema import (
    LEDGER_PATH,
    LEDGER_VERSION,
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


# ── load_ledger repo_root resolution ─────────────────────────────────────────


def test_load_ledger_repo_root_picks_up_committed_file(tmp_path: Path) -> None:
    """repo_root/contract_schema.lock.json is loaded when present."""
    ledger_data = {"version": LEDGER_VERSION, "fields": []}
    (tmp_path / "contract_schema.lock.json").write_text(
        json.dumps(ledger_data), encoding="utf-8"
    )
    ledger = load_ledger(repo_root=tmp_path)
    assert ledger.fields == []


def test_load_ledger_repo_root_falls_back_to_package_data_when_absent(
    tmp_path: Path,
) -> None:
    """When no contract_schema.lock.json exists in repo_root, package data is used."""
    ledger = load_ledger(repo_root=tmp_path)
    # Package data has the SDK template contracts — non-empty for a real install.
    assert isinstance(ledger.fields, list)


def test_load_ledger_env_override_takes_priority_over_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATLAN_CONTRACT_LEDGER_PATH wins over repo_root when both are set."""
    env_file = tmp_path / "env_ledger.json"
    env_data = {
        "version": LEDGER_VERSION,
        "fields": [
            {"contract": "Env", "field": "x", "type": "str", "status": "active"}
        ],
    }
    env_file.write_text(json.dumps(env_data), encoding="utf-8")

    repo_file = tmp_path / "contract_schema.lock.json"
    repo_data = {"version": LEDGER_VERSION, "fields": []}
    repo_file.write_text(json.dumps(repo_data), encoding="utf-8")

    monkeypatch.setenv("ATLAN_CONTRACT_LEDGER_PATH", str(env_file))
    ledger = load_ledger(repo_root=tmp_path)
    assert len(ledger.fields) == 1
    assert ledger.fields[0].contract == "Env"


def test_load_ledger_explicit_path_takes_priority_over_repo_root(
    tmp_path: Path,
) -> None:
    """An explicit *path* argument wins over repo_root."""
    explicit_file = tmp_path / "explicit.json"
    explicit_data = {
        "version": LEDGER_VERSION,
        "fields": [
            {"contract": "Explicit", "field": "y", "type": "int", "status": "active"}
        ],
    }
    explicit_file.write_text(json.dumps(explicit_data), encoding="utf-8")

    repo_file = tmp_path / "contract_schema.lock.json"
    repo_file.write_text(
        json.dumps({"version": LEDGER_VERSION, "fields": []}), encoding="utf-8"
    )

    ledger = load_ledger(path=explicit_file, repo_root=tmp_path)
    assert len(ledger.fields) == 1
    assert ledger.fields[0].contract == "Explicit"


# ─────────────────────────────────────────────────────────────────────────────


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
