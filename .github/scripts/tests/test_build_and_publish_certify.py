"""Tests for .github/scripts/build_and_publish/certify.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))

import _bap  # noqa: E402
import certify  # noqa: E402


@pytest.fixture
def summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "summary"
    path.write_text("")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


def _stub(monkeypatch: pytest.MonkeyPatch, codes: dict[str, int]) -> list[list[str]]:
    """Exit codes keyed by the command's first distinguishing words."""
    calls: list[list[str]] = []

    def fake(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        key = next((k for k in codes if " ".join(cmd).startswith(k)), None)
        return subprocess.CompletedProcess(cmd, codes.get(key, 0) if key else 0, "", "")

    monkeypatch.setattr(_bap, "run", fake)
    return calls


# ── check-migration ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rc", "expected", "message"),
    [
        (0, 0, "check_migration: no FAILs"),
        (3, 1, "::warning::check_migration reported FAILs — see output above"),
    ],
)
def test_check_migration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rc: int,
    expected: int,
    message: str,
) -> None:
    calls = _stub(monkeypatch, {"uv run python": rc})
    assert certify.main(["check-migration"]) == expected
    assert calls == [
        [
            "uv",
            "run",
            "python",
            "-m",
            "tools.migrate_v3.check_migration",
            "--no-color",
            "../app",
        ]
    ]
    assert message in capsys.readouterr().out


# ── seed-pkl-ca ───────────────────────────────────────────────────────────────


def test_seed_copies_the_system_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("PEM")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert certify.seed_pkl_ca(str(ca)) == 0
    assert (tmp_path / ".pkl-ca-bundle.pem").read_text() == "PEM"
    assert f"Seeded {tmp_path}/.pkl-ca-bundle.pem from {ca}" in capsys.readouterr().out


def test_seed_without_a_system_bundle_warns_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert certify.seed_pkl_ca(str(tmp_path / "missing")) == 0
    assert not (tmp_path / ".pkl-ca-bundle.pem").exists()
    assert "skipping pkl CA seed" in capsys.readouterr().out


# ── contract-drift ────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "app.pkl").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_no_contract_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _stub(monkeypatch, {})
    assert certify.main(["contract-drift"]) == 0
    assert not calls
    assert (
        "No contract/app.pkl — skipping contract-drift check."
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("codes", "expected", "message"),
    [
        ({}, 0, ""),
        ({"uv run poe --help": 1}, 0, "::warning::'poe' task runner not available"),
        ({"uv run poe generate": 1}, 1, "::error::'poe generate' failed"),
        ({"git diff": 1}, 1, "::error::generated/ is stale"),
    ],
)
def test_contract_drift(
    app: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    codes: dict[str, int],
    expected: int,
    message: str,
) -> None:
    calls = _stub(monkeypatch, codes)
    assert certify.main(["contract-drift"]) == expected
    assert message in capsys.readouterr().out
    if not codes:
        assert calls == [
            ["uv", "run", "poe", "--help"],
            ["uv", "run", "poe", "generate"],
            ["git", "diff", "--exit-code", "--", "generated/"],
        ]


# ── verdict ───────────────────────────────────────────────────────────────────


def _verdict(monkeypatch: pytest.MonkeyPatch, **env: str) -> int:
    defaults = {
        "CM": "success",
        "CD": "success",
        "UT": "success",
        "COV": "success",
        "SDK_INSTALL": "success",
        "APP_INSTALL": "success",
    }
    for key, value in {**defaults, **env}.items():
        monkeypatch.setenv(key, value)
    return certify.main(["verdict"])


def test_all_green(
    summary: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _verdict(monkeypatch) == 0
    assert summary.read_text().startswith(
        "### App certification\n\n"
        "- ✅ **v3 shape (check_migration) — warn-only** — success\n"
        "- ✅ **Contract drift (poe generate) — enforced** — success\n"
        "- ✅ **Unit tests — enforced** — success\n"
        "- ✅ **Coverage (>=85%) — warn-only** — success\n\n"
        "**Unit tests and contract drift block publish on ❌.**"
    )
    assert capsys.readouterr().out == ""


def test_icons_for_every_outcome(
    summary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verdict(monkeypatch, CM="skipped", COV="cancelled")
    text = summary.read_text()
    assert "- ➖ **v3 shape" in text
    assert "- ❓ **Coverage (>=85%) — warn-only** — cancelled" in text


def test_warn_only_failures_do_not_block(
    summary: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _verdict(monkeypatch, CM="failure", COV="failure") == 0
    out = capsys.readouterr().out
    assert "v3 shape failed; publish proceeded (warn-only)." in out
    assert "Coverage below 85%; publish proceeded (warn-only)." in out
    assert "Publish blocked" not in summary.read_text()


@pytest.mark.parametrize(
    ("env", "heading"),
    [
        ({"CD": "failure"}, "### ❌ Publish blocked — contract drift"),
        ({"UT": "failure"}, "### ❌ Publish blocked — unit tests failed"),
    ],
)
def test_enforced_failures_block(
    summary: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    heading: str,
) -> None:
    assert _verdict(monkeypatch, **env) == 1
    assert f"\n\n{heading}\n\n" in summary.read_text()
    assert "::error title=Publish blocked::" in capsys.readouterr().out


def test_install_failures_are_labelled(
    summary: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _verdict(
            monkeypatch,
            SDK_INSTALL="failure",
            APP_INSTALL="failure",
            CM="skipped",
            CD="skipped",
            UT="failure",
        )
        == 1
    )
    text = summary.read_text()
    assert (
        "**v3 shape (check_migration) — warn-only (sdk install failed)** — skipped"
        in text
    )
    assert "**Contract drift (poe generate) — enforced (app install failed)**" in text
    assert "**Unit tests — enforced (app install failed)** — failure" in text
    out = capsys.readouterr().out
    assert "SDK install failed, so check_migration did not run." in out
    assert "App install failed; contract drift and unit tests did not run." in out
