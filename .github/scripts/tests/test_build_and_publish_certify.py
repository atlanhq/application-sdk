"""Tests for .github/scripts/build_and_publish/certify.py."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
        (
            {"uv run poe --help": 1},
            1,
            "::error::contract/app.pkl exists but the 'poe' task runner is not installed",
        ),
        ({"uv run poe generate": 1}, 1, "::error::'poe generate' failed"),
        (
            {"git status": 128},
            1,
            "::error::'git status' failed, so contract drift could not be checked",
        ),
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
            ["git", "status", "--porcelain", "--untracked-files=all"],
        ]


def test_contract_without_poe_fails_closed(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An app with contract/app.pkl but no poe must block publish, not pass."""
    calls = _stub(monkeypatch, {"uv run poe --help": 1})
    assert certify.main(["contract-drift"]) == 1
    assert calls == [["uv", "run", "poe", "--help"]]
    out = capsys.readouterr().out
    assert out.startswith("::error::")
    assert "poethepoet" in out
    assert "[project.optional-dependencies]" in out


# The drift tests below run real git against a real repo; only the `uv run`
# calls are faked, and `poe generate` is simulated by ``generate``.

_GIT = (
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@t",
    "-c",
    "commit.gpgsign=false",
)


@pytest.fixture
def git_app(app: Path) -> Path:
    """An app repo whose generated output is committed and up to date."""
    (app / "app" / "generated").mkdir(parents=True)
    (app / "app" / "generated" / "_input.py").write_text("FIELDS = ['host']\n")
    (app / "atlan.yaml").write_text("name: example\n")
    (app / ".gitignore").write_text(".venv/\n")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run([*_GIT, *cmd], cwd=app, check=True)
    return app


def _generate(
    monkeypatch: pytest.MonkeyPatch, generate: Callable[[], None]
) -> list[list[str]]:
    """Fake only `uv run …`; `poe generate` runs ``generate``. git runs for real."""
    real_run = _bap.run
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[:2] == ["uv", "run"]:
            if cmd[3:] == ["generate"]:
                generate()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(_bap, "run", fake)
    return calls


def test_drift_passes_when_generate_is_a_no_op(
    git_app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rewrite_identically() -> None:
        (git_app / "app" / "generated" / "_input.py").write_text("FIELDS = ['host']\n")
        (git_app / ".venv").mkdir()  # ignored output must not count
        (git_app / ".venv" / "x").write_text("")

    _generate(monkeypatch, rewrite_identically)
    assert certify.main(["contract-drift"]) == 0
    assert "::error::" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("path", "content", "listed"),
    [
        # A stale tracked file under app/generated/ (the old `-- generated/`
        # pathspec only matched a top-level generated/ directory).
        (
            "app/generated/_input.py",
            "FIELDS = ['host', 'port']\n",
            " M app/generated/_input.py",
        ),
        # New output that was never committed (`git diff` ignores untracked files).
        ("app/generated/_e2e_base.py", "BASE = 1\n", "?? app/generated/_e2e_base.py"),
        # A generated root-level file.
        ("atlan.yaml", "name: changed\n", " M atlan.yaml"),
    ],
)
def test_drift_fails_on_any_working_tree_change(
    git_app: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
    content: str,
    listed: str,
) -> None:
    _generate(monkeypatch, lambda: (git_app / path).write_text(content))
    assert certify.main(["contract-drift"]) == 1
    out = capsys.readouterr().out
    assert "::error::'poe generate' left 1 changed or untracked file(s)" in out
    assert f"\n  {listed}\n" in out
    assert content.strip() not in out  # paths only, never contents


def test_drift_listing_is_capped(
    git_app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def many() -> None:
        for i in range(certify._MAX_LISTED_PATHS + 3):
            (git_app / "app" / "generated" / f"f{i:03}.py").write_text("")

    _generate(monkeypatch, many)
    assert certify.main(["contract-drift"]) == 1
    out = capsys.readouterr().out
    assert out.count("\n  ?? app/generated/") == certify._MAX_LISTED_PATHS
    assert "  … and 3 more" in out


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


def test_install_failures_block_and_are_labelled(
    summary: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The shape a run takes when the app's deps fail to install: drift is
    # skipped and the unit fan-in reports install_failed — nothing failed, yet
    # nothing was certified, so publish is blocked (FND-4538).
    assert (
        _verdict(
            monkeypatch,
            SDK_INSTALL="failure",
            APP_INSTALL="failure",
            CM="skipped",
            CD="skipped",
            UT="install_failed",
            COV="skipped",
        )
        == 1
    )
    text = summary.read_text()
    assert (
        "**v3 shape (check_migration) — warn-only (sdk install failed)** — skipped"
        in text
    )
    assert "**Contract drift (poe generate) — enforced (app install failed)**" in text
    assert (
        "- ❌ **Unit tests — enforced (app install failed)** — install_failed" in text
    )
    assert "\n\n### ❌ Publish blocked — app dependencies did not install\n\n" in text
    out = capsys.readouterr().out
    assert "SDK install failed, so check_migration did not run." in out
    assert "::error title=Publish blocked::App dependencies did not install" in out


@pytest.mark.parametrize(
    "env",
    [
        # certify's own install failed (default single-shard layout).
        {"APP_INSTALL": "failure", "CD": "skipped", "UT": "install_failed"},
        # Install outcome not "success" for any reason (cancelled, skipped).
        {"APP_INSTALL": "cancelled", "CD": "skipped", "UT": "skipped"},
        # Sharded: certify installed, but a shard runner did not.
        {"UT": "install_failed", "COV": "skipped"},
    ],
)
def test_app_install_failure_fails_closed(
    summary: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
) -> None:
    assert _verdict(monkeypatch, **env) == 1
    assert "Publish blocked — app dependencies did not install" in summary.read_text()
    assert "::error title=Publish blocked::" in capsys.readouterr().out


def test_an_app_without_unit_tests_still_passes(
    summary: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No tests/unit: certify_unit_shards reports "skipped" with the install a
    # success. By design this is not a block — 8 fleet apps have no unit suite.
    assert _verdict(monkeypatch, UT="skipped", COV="skipped") == 0
    assert "- ➖ **Unit tests — enforced** — skipped" in summary.read_text()
    assert "Publish blocked" not in summary.read_text()
    assert "::error" not in capsys.readouterr().out
