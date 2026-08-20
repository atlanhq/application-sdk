"""Tests for .github/scripts/renovate_contract_ledger.py.

Pure-unit: no network, no uvx invocation. What needs regression cover is the
decision logic around the generator call — which version it pins, and the two
no-op conditions — because both failure directions are silent. Pinning the wrong
version regenerates a ledger the repo will not resolve after merge; creating a
ledger in a repo that has none hands a dependency bump ownership of a file
``bootstrap`` owns.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import renovate_contract_ledger as driver

LOCK = """\
version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "atlan-application-sdk"
version = "3.28.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "atlan-application-sdk-conformance"
version = "0.21.0"
source = { registry = "https://pypi.org/simple" }
"""

LOCK_WITHOUT_CONFORMANCE = """\
version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "atlan-application-sdk"
version = "3.28.1"
source = { registry = "https://pypi.org/simple" }
"""


def _repo(tmp_path: Path, *, lock: str | None = LOCK, ledger: bool = True) -> Path:
    if lock is not None:
        (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    if ledger:
        (tmp_path / "contract_schema.lock.json").write_text(
            '{"version": 1, "fields": []}\n', encoding="utf-8"
        )
    return tmp_path


# ── Version resolution ────────────────────────────────────────────────────────


def test_locked_version_reads_the_conformance_entry() -> None:
    assert driver.locked_version(LOCK) == "0.21.0"


def test_locked_version_is_none_when_the_package_is_absent() -> None:
    assert driver.locked_version(LOCK_WITHOUT_CONFORMANCE) is None


def test_locked_version_rejects_a_malformed_lock() -> None:
    with pytest.raises(SystemExit):
        driver.locked_version("this is not toml = = =")


# ── The generator invocation ──────────────────────────────────────────────────


def test_regen_argv_pins_the_locked_version() -> None:
    """The pin is the whole point: an unpinned uvx would use latest instead."""
    assert driver.regen_argv("0.21.0") == [
        "uvx",
        "atlan-application-sdk-conformance==0.21.0",
        "gen-contract-ledger",
    ]


@pytest.mark.parametrize("version", ["0.21.0", "1.0", "2.0.0rc1", "0.21.0.post1"])
def test_regen_argv_accepts_published_version_shapes(version: str) -> None:
    assert driver.regen_argv(version)[1].endswith(f"=={version}")


@pytest.mark.parametrize(
    "version", ["", "latest", "0.21.0; rm -rf /", "@main", "0.21.0 --with evil"]
)
def test_regen_argv_refuses_a_non_version_string(version: str) -> None:
    """Nothing but a PEP 440 release string reaches a subprocess argument."""
    with pytest.raises(SystemExit):
        driver.regen_argv(version)


# ── No-op conditions ──────────────────────────────────────────────────────────


def test_run_regenerates_at_the_locked_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path]] = []

    class _Result:
        returncode = 0

    def fake_run(argv: list[str], cwd: Path) -> _Result:
        calls.append((argv, cwd))
        return _Result()

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    repo = _repo(tmp_path)

    assert driver.run(repo) == 0
    assert calls == [(driver.regen_argv("0.21.0"), repo)]


def test_run_is_a_noop_without_a_committed_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo that never adopted the ledger must not acquire one from a bump."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the generator must not run")

    monkeypatch.setattr(driver.subprocess, "run", fail)
    repo = _repo(tmp_path, ledger=False)

    assert driver.run(repo) == 0
    assert not (repo / "contract_schema.lock.json").exists()


def test_run_is_a_noop_without_a_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the generator must not run")

    monkeypatch.setattr(driver.subprocess, "run", fail)
    repo = _repo(tmp_path, lock=None)

    assert driver.run(repo) == 0


def test_run_is_a_noop_when_conformance_is_not_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the generator must not run")

    monkeypatch.setattr(driver.subprocess, "run", fail)
    repo = _repo(tmp_path, lock=LOCK_WITHOUT_CONFORMANCE)

    assert driver.run(repo) == 0


def test_run_propagates_a_generator_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure must be loud: renovate/artifacts goes red and withholds approval."""

    class _Result:
        returncode = 7

    monkeypatch.setattr(driver.subprocess, "run", lambda *_a, **_k: _Result())
    repo = _repo(tmp_path)

    assert driver.run(repo) == 7
