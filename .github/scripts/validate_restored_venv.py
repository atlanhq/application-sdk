#!/usr/bin/env python3
"""Decide whether a ``.venv`` restored from the setup-deps cache is usable.

The setup-deps venv cache is keyed on the Python ``actions/setup-python``
installed, but ``uv sync`` builds the venv on whatever interpreter satisfies
the project's ``requires-python``. When the two disagree — e.g. the SDR e2e
harness asks for 3.13 while an app declares ``<3.13`` — uv picks another
interpreter, and the venv's ``bin/python`` links to it:

* on ubuntu-24.04 that was the system ``/usr/bin/python3`` (3.12), present on
  every runner, so restored venvs kept working;
* on ubuntu-26.04 there is no system 3.12, so uv downloads one into the
  runner's temp dir (``UV_PYTHON_INSTALL_DIR``), which is not cached. The saved
  venv then links to a path that does not exist on the next runner.

A cache hit skips ``uv sync``, so the first ``uv run`` finds the dangling
interpreter, deletes the venv and rebuilds it with only the default dependency
groups — dropping pytest and failing with ``Failed to spawn: pytest``.

This script runs after the restore. If the venv's interpreter is missing,
cannot start, or is a different version than the venv was built for (a
``/usr/bin/python3`` that moved from 3.12 to 3.14 between images), it removes
the venv and reports ``usable=false`` so setup-deps does a full install.

Usage:
    python3 -I validate_restored_venv.py [<venv-dir>]   # default: .venv
Writes ``usable=true|false`` to ``$GITHUB_OUTPUT`` when set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_PROBE = "import sys; print('.'.join(map(str, sys.version_info[:3])))"


def _built_version(venv: Path) -> str | None:
    """The ``version_info`` / ``version`` uv or venv recorded in pyvenv.cfg."""
    cfg = venv / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    for line in cfg.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("version_info", "version"):
            return value.strip()
    return None


def _running_version(python: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def check(venv: Path) -> tuple[bool, str]:
    """Return (usable, reason). ``reason`` is empty when usable."""
    python = venv / "bin" / "python"
    if not venv.is_dir():
        return False, f"no {venv} was restored"
    if not python.exists():
        target = os.readlink(python) if python.is_symlink() else "<missing>"
        return False, f"{python} links to {target}, which does not exist on this runner"
    running = _running_version(python)
    if running is None:
        return False, f"{python} does not start"
    built = _built_version(venv)
    if built is None:
        return False, f"{venv}/pyvenv.cfg records no Python version"
    # Compare major.minor.micro; pyvenv.cfg may carry a release suffix.
    if running.split(".")[:3] != built.split(".")[:3]:
        return (
            False,
            f"{python} is Python {running}, but the venv was built for {built}",
        )
    return True, ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: validate_restored_venv.py [<venv-dir>]", file=sys.stderr)
        return 2
    venv = Path(args[0] if args else ".venv")
    usable, reason = check(venv)
    if not usable:
        print(
            f"::warning::restored venv is not usable ({reason}); "
            "removing it and installing dependencies from scratch."
        )
        shutil.rmtree(venv, ignore_errors=True)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"usable={'true' if usable else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
