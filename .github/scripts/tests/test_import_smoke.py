"""import_smoke catches an order-dependent import cycle (FND-4176).

The package under test lives in a real venv: a ``.pth`` points at a tmp source
tree and a hand-written ``.dist-info`` declares its console scripts — the same
shape as an editable install. So the script runs exactly as it does in CI: a
separate interpreter, ``-I``, scripts read from installed metadata.

The cycle is built so that importing ``cyc.b`` first succeeds and importing
``cyc.a`` first fails, mirroring conformance 0.51.0, where a single process
that happened to import the right module first saw nothing wrong.
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_smoke  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="console-script shims need a POSIX shebang"
)

# b defines y before importing a, so b-first completes. a-first runs b, which
# asks the half-initialised a for x.
CYCLIC_A = "from cyc.b import y\n\nx = 1\n"
CYCLIC_B = "y = 2\n\nfrom cyc.a import x  # noqa: E402\n"
ACYCLIC_B = "y = 2\n"

CLI = """\
import sys

_COMMANDS = {"ok": None, "uses-a": None}


def main() -> None:
    if sys.argv[1] == "uses-a":
        import cyc.a  # noqa: F401
    sys.exit(0)


def other() -> None:
    sys.exit(0)
"""

ENTRY_POINTS = """\
[console_scripts]
cyc-cli = cyc.cli:main
cyc-other = cyc.cli:other
"""


def _make_venv(
    tmp_path: Path, b_source: str, entry_points: str = ENTRY_POINTS
) -> tuple[Path, Path]:
    """A venv with ``cyc`` installed from ``tmp_path/src``; returns (python, src)."""
    src = tmp_path / "src"
    pkg = src / "cyc"
    (pkg / "tools").mkdir(parents=True)  # namespace subpackage, no __init__
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(CYCLIC_A)
    (pkg / "b.py").write_text(b_source)
    (pkg / "cli.py").write_text(CLI)
    (pkg / "tools" / "gen.py").write_text("from cyc.a import x  # noqa: F401\n")
    (pkg / "not-a-module").mkdir()
    (pkg / "not-a-module" / "x.py").write_text("raise SystemExit(1)\n")

    env = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(env)
    python = env / "bin" / "python"
    purelib = Path(
        subprocess.run(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    (purelib / "cyc.pth").write_text(f"{src}\n")
    info = purelib / "cyc-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: cyc\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text(entry_points)

    for name, attr in (("cyc-cli", "main"), ("cyc-other", "other")):
        script = env / "bin" / name
        script.write_text(f"#!{python}\nfrom cyc.cli import {attr}\n{attr}()\n")
        script.chmod(0o755)
    return python, src


def _smoke(python: Path, *extra: str) -> int:
    return import_smoke.main(
        [
            "--python",
            str(python),
            "--package",
            "cyc",
            "--dist",
            "cyc",
            "--commands-from",
            "cyc.cli:_COMMANDS",
            *extra,
        ]
    )


def _failed(err: str) -> set[str]:
    return {line[5:] for line in err.splitlines() if line.startswith("FAIL ")}


def test_single_process_import_order_hides_the_cycle(tmp_path: Path) -> None:
    """The premise: b-then-a in one process imports cleanly; a alone does not."""
    python, _ = _make_venv(tmp_path, CYCLIC_B)
    ok = subprocess.run([str(python), "-I", "-c", "import cyc.b, cyc.a"])
    assert ok.returncode == 0
    broken = subprocess.run(
        [str(python), "-I", "-c", "import cyc.a"], capture_output=True, text=True
    )
    assert "circular import" in broken.stderr


def test_red_on_cycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    python, _ = _make_venv(tmp_path, CYCLIC_B)
    assert _smoke(python) == 1
    assert _failed(capsys.readouterr().err) == {
        "cyc.a",
        "cyc.tools.gen",
        "cyc-cli uses-a --help",
    }


def test_green_without_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    python, _ = _make_venv(tmp_path, ACYCLIC_B)
    assert _smoke(python) == 0
    out = capsys.readouterr().out
    assert "imported 5 modules of cyc, 0 failed" in out
    # cyc-cli --help, its 2 subcommands, cyc-other --help. cyc-other's entry
    # point is in the dispatcher module too, so it also gets the subcommands.
    assert "ran 6 console-script invocations of cyc, 0 failed" in out


def test_scripts_come_from_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A script dropped from entry_points.txt is not run, even though its shim
    is still on disk — the list is the metadata's, not the filesystem's."""
    python, _ = _make_venv(
        tmp_path, ACYCLIC_B, "[console_scripts]\ncyc-cli = cyc.cli:main\n"
    )
    assert _smoke(python) == 0
    assert "ran 3 console-script invocations" in capsys.readouterr().out


def test_red_when_dist_declares_no_scripts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    python, _ = _make_venv(tmp_path, ACYCLIC_B, "")
    assert _smoke(python) == 1
    assert _failed(capsys.readouterr().err) == {"cyc"}


def test_red_when_package_resolves_inside_outside_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--outside is what proves the release gate tested the wheel, not the tree."""
    python, src = _make_venv(tmp_path, ACYCLIC_B)
    assert _smoke(python, "--outside", str(src)) == 1
    assert "not from the install" in capsys.readouterr().err
    assert _smoke(python, "--outside", str(tmp_path / "elsewhere")) == 0


def test_discover_includes_namespace_modules_and_skips_non_identifiers(
    tmp_path: Path,
) -> None:
    python, _ = _make_venv(tmp_path, ACYCLIC_B)
    pkg_dir = import_smoke.locate_package(str(python), "cyc")
    assert import_smoke.discover_modules(pkg_dir, "cyc") == [
        "cyc",
        "cyc.a",
        "cyc.b",
        "cyc.cli",
        "cyc.tools.gen",
    ]


def test_commands_from_without_dist_is_rejected() -> None:
    with pytest.raises(SystemExit):
        import_smoke.main(
            ["--python", sys.executable, "--package", "x", "--commands-from", "y:z"]
        )
