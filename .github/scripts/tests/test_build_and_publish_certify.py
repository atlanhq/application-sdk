"""Tests for .github/scripts/build_and_publish/certify.py."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))

import check_generated_freshness  # noqa: E402
import renovate_pkl_sync  # noqa: E402

# The scripts import each other as top-level modules (`import _bap`), so the
# tests do too: that keeps one module object for monkeypatching. pyright
# resolves them through the package path instead.
if TYPE_CHECKING:
    from build_and_publish import _bap, certify
else:
    import _bap
    import certify


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


# ── contract-drift ────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "app.pkl").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _regeneration(
    monkeypatch: pytest.MonkeyPatch,
    regenerate: Callable[[str], bool],
    roots: list[Path] | None = None,
) -> list[str]:
    """Replace the SDK regeneration seam; returns the contract dirs regenerated."""
    calls: list[str] = []

    def fake_regenerate(contract_dir: str) -> bool:
        calls.append(contract_dir)
        return regenerate(contract_dir)

    def fake_roots(contract_dir: str) -> list[Path]:
        return [Path(contract_dir) / "app.pkl"] if roots is None else roots

    monkeypatch.setattr(
        certify, "_sdk_regeneration", lambda: (fake_roots, fake_regenerate)
    )
    return calls


def test_the_seam_is_the_freshness_checks_regeneration() -> None:
    """Certify must regenerate with the exact function the generated-freshness
    check and Renovate's toolkit bump use, not a copy and not the app's poe."""
    eval_roots, regenerate = certify._sdk_regeneration()
    assert regenerate is renovate_pkl_sync.regenerate
    assert regenerate is check_generated_freshness.regenerate
    assert eval_roots is renovate_pkl_sync.eval_roots


def test_no_contract_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = _regeneration(monkeypatch, lambda _: True, roots=[])
    assert certify.main(["contract-drift"]) == 0
    assert not calls
    assert "No pkl contract in contract/ — skipping" in capsys.readouterr().out


def test_a_contract_that_does_not_regenerate_fails_closed(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _regeneration(monkeypatch, lambda _: False)
    assert certify.main(["contract-drift"]) == 1
    assert "::error::The committed contract does not regenerate" in (
        capsys.readouterr().out
    )


def test_a_regeneration_that_cannot_run_fails_closed(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_pkl(_: str) -> bool:
        raise FileNotFoundError("pkl")

    _regeneration(monkeypatch, no_pkl)
    assert certify.main(["contract-drift"]) == 1
    assert "::error::Could not run the contract regeneration" in (
        capsys.readouterr().out
    )


def test_git_status_failure_fails_closed(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _regeneration(monkeypatch, lambda _: True)
    _stub(monkeypatch, {"git status": 128})
    assert certify.main(["contract-drift"]) == 1
    assert "::error::'git status' failed, so contract drift could not be checked" in (
        capsys.readouterr().out
    )


def test_poe_is_never_run(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An app's `poe generate` is app-defined shell; certify must not run it,
    and an app without poe is not failed for lacking it."""
    _regeneration(monkeypatch, lambda _: True)
    calls = _stub(monkeypatch, {})
    assert certify.main(["contract-drift"]) == 0
    assert calls == [["git", "status", "--porcelain", "--untracked-files=all"]]


# The drift tests below run real git against a real repo; only the
# regeneration is faked, by ``generate``.

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


def _generate(monkeypatch: pytest.MonkeyPatch, generate: Callable[[], object]) -> None:
    def regenerate(_: str) -> bool:
        generate()
        return True

    _regeneration(monkeypatch, regenerate)


def test_drift_passes_when_regeneration_is_a_no_op(
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
        # A stale tracked file under app/generated/.
        (
            "app/generated/_input.py",
            "FIELDS = ['host', 'port']\n",
            " M app/generated/_input.py",
        ),
        # New output that was never committed (`git diff` ignores untracked files).
        ("app/generated/_e2e_base.py", "BASE = 1\n", "?? app/generated/_e2e_base.py"),
        # A generated root-level file.
        ("atlan.yaml", "name: changed\n", " M atlan.yaml"),
        # Output placed outside app/generated by the contract.
        ("app/contracts/_input.py", "X = 1\n", "?? app/contracts/_input.py"),
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
    def write() -> None:
        (git_app / path).parent.mkdir(parents=True, exist_ok=True)
        (git_app / path).write_text(content)

    _generate(monkeypatch, write)
    assert certify.main(["contract-drift"]) == 1
    out = capsys.readouterr().out
    assert (
        "::error::Regenerating the contract left 1 changed or untracked file(s)" in out
    )
    assert "contract/post-generate.sh" in out
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
        "- ✅ **Contract drift (SDK regeneration) — enforced** — success\n"
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
    # The shape a run takes when the app's deps fail to install: drift still
    # runs (it needs pkl, not the app's deps) and the unit fan-in reports
    # install_failed, so publish is blocked (FND-4538).
    assert (
        _verdict(
            monkeypatch,
            SDK_INSTALL="failure",
            APP_INSTALL="failure",
            CM="skipped",
            CD="success",
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
    assert "- ✅ **Contract drift (SDK regeneration) — enforced** — success" in text
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
