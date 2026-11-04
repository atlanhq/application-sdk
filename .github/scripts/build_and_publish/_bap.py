"""Shared plumbing for the build-and-publish-app.yaml step scripts.

Every script in this package replaces a ``run:`` block of
``.github/workflows/build-and-publish-app.yaml`` that used to branch inline
(docs/standards/ci.md). They are invoked as ``python3 <path>/<script>.py``, so
``import _bap`` resolves against this directory.

Two seams for tests:

* :func:`run` is the only place an external command (gh, git, gitleaks, curl,
  docker, uv) is executed. Tests replace it and let the rest run for real.
* :func:`append` writes to the file a GitHub env var names (``GITHUB_OUTPUT``,
  ``GITHUB_STEP_SUMMARY``). Tests point those vars at ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence


def run(
    cmd: Sequence[str],
    *,
    cwd: str | None = None,
    capture: bool = False,
    quiet: bool = False,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` and return the completed process; never raises on exit code.

    ``capture`` collects stdout (stderr still reaches the job log, as it did
    inside ``$(...)``). ``quiet`` discards both streams, like ``>/dev/null 2>&1``.
    """
    stdout = subprocess.PIPE if capture else (subprocess.DEVNULL if quiet else None)
    stderr = subprocess.DEVNULL if quiet else None
    return subprocess.run(  # noqa: S603 - argv lists only, no shell
        list(cmd),
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        input=input,
        text=True,
        check=False,
    )


def run_checked(cmd: Sequence[str], **kwargs: object) -> str:
    """Run ``cmd``, raise :class:`CommandFailed` on a non-zero exit, return stdout.

    Mirrors ``X=$(cmd)`` under ``bash -e``: a failure ends the step with the
    command's own exit code. Trailing newlines are stripped as ``$(...)`` does.
    """
    proc = run(cmd, capture=True, **kwargs)  # type: ignore[arg-type]
    if proc.returncode != 0:
        raise CommandFailed(proc.returncode)
    return (proc.stdout or "").rstrip("\n")


class CommandFailed(Exception):
    """An external command exited non-zero where the step must stop."""

    def __init__(self, returncode: int) -> None:
        super().__init__(f"command exited {returncode}")
        self.returncode = returncode


def append(env_var: str, text: str) -> None:
    """Append ``text`` to the file named by ``env_var`` (GITHUB_OUTPUT etc.)."""
    with open(os.environ[env_var], "a", encoding="utf-8") as fh:
        fh.write(text)


def output(**values: object) -> None:
    """Write ``key=value`` lines to ``$GITHUB_OUTPUT``."""
    append("GITHUB_OUTPUT", "".join(f"{k}={v}\n" for k, v in values.items()))


def summary(*lines: str) -> None:
    """Append lines to ``$GITHUB_STEP_SUMMARY``, one per argument."""
    append("GITHUB_STEP_SUMMARY", "".join(f"{line}\n" for line in lines))


def env(name: str) -> str:
    """An env var, empty when unset (steps always set them, possibly empty)."""
    return os.environ.get(name, "")


def strip_crlf(value: str) -> str:
    """Remove every CR/LF, as ``${X//$'\\r'/}`` then ``${X//$'\\n'/}`` did."""
    return value.replace("\r", "").replace("\n", "")


def jq_str(value: object) -> str:
    """Render a JSON value the way a ``jq`` string interpolation does."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def line_buffered_stdout() -> None:
    """Flush each printed line, so it lands in the job log in order with the
    output of the commands :func:`run` starts (stdout is a pipe on a runner)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
