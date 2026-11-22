"""The pinned Pkl toolchain, available to an app's own tooling (FND-1864).

CI renders every contract with one exact pkl build — the pin in
``application_sdk.pkl_version``. This module hands an app that same build, so a
local ``uv run poe generate`` performs the *same computation* the
``Generated Artifact Freshness`` gate performs rather than an approximation of
it with whatever ``brew install pkl`` last produced.

That distinction is the whole point. pkl is a language: syntax accepted by one
release is rejected by another (backslash line-continuations inside a
multi-line string are valid from 0.28 on and a hard ``Invalid character escape
sequence`` on 0.27). Before this, a contract could render cleanly on a laptop
and be structurally incapable of rendering in CI, and the gate reported it as
stale artifacts.

Public module, unlike its sibling ``_dapr``: apps invoke it directly.

Use it from an app
------------------
In ``pyproject.toml``, run the generator through the pinned binary::

    [tool.poe.tasks]
    generate.shell = \"\"\"
      PKL="$(python -m application_sdk.dev.pkl path)"
      "$PKL" eval --project-dir contract -m . contract/app.pkl
      uvx ruff check --fix --quiet --force-exclude app/generated/*.py
      uvx ruff format --force-exclude app/generated/*.py
    \"\"\"

The ruff calls must match the freshness gate's exactly (no ``--select``, so
your own rule set applies; ``--force-exclude``, so an ``exclude`` covering
``app/generated`` is honoured for these explicit paths). Anything else
produces output the gate reports as stale.

Or, equivalently, without capturing the path::

    python -m application_sdk.dev.pkl run -- \\
      eval --project-dir contract -m . contract/app.pkl

An app that would rather keep using the pkl on ``PATH`` can at least learn when
it has drifted from CI::

    python -m application_sdk.dev.pkl check          # exit 1 on mismatch
    python -m application_sdk.dev.pkl check --warn    # advisory only

Where the binary goes
---------------------
``~/.cache/atlan-sdk/pkl/<version>/pkl``, keyed by version so several pins can
coexist and a bump is a fresh download rather than an overwrite. Same cache
convention, download-on-first-use behaviour and no-checksum posture as the
embedded daprd in ``application_sdk.dev._dapr`` — pkl publishes no per-asset
digest, and this is the same HTTPS fetch of the same pinned release asset that
the CI ``install-pkl`` action already performs. The fetch itself — socket
timeout, retries that skip a 4xx, atomic publish — is the one daprd uses
(``application_sdk.dev._download``). A cached binary is used only if it runs
and reports the pinned version; anything else is downloaded again.
"""

from __future__ import annotations

import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path

from application_sdk.dev._download import (
    NETWORK_ERRORS,
    fetch_release_asset,
    fetch_to_file,
    is_retryable_network_error,
)
from application_sdk.dev._pkl_errors import (
    PklDownloadError,
    UnsupportedPklPlatformError,
)
from application_sdk.pkl_version import PKL_VERSION


def _progress(message: str) -> None:
    """Report download progress on **stderr**.

    Deliberately not the SDK logger. That writes to stdout — correct for app
    runtime, where the collector reads stdout for OTel — and wrong here: this
    module's stdout is a machine-readable value that a generate task consumes
    (``PKL="$(python -m application_sdk.dev.pkl path)"``), so a single log line
    on stdout turns the captured path into three lines of garbage. Nothing
    collects this process anyway; it runs on a laptop before the app exists.
    """
    print(message, file=sys.stderr)


_RELEASE_ASSET_URL = "https://github.com/apple/pkl/releases/download/{version}/{asset}"

# The pkl release assets, keyed by (platform.system().lower(), normalised arch).
# Enumerated rather than templated because the naming is not uniform: Linux and
# macOS spell 64-bit ARM `aarch64` while Dapr spells it `arm64`, and Windows
# publishes an `.exe` for amd64 only. A missing key is a real gap, not a string
# to guess at.
_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "amd64"): "pkl-linux-amd64",
    ("linux", "aarch64"): "pkl-linux-aarch64",
    ("darwin", "amd64"): "pkl-macos-amd64",
    ("darwin", "aarch64"): "pkl-macos-aarch64",
    ("windows", "amd64"): "pkl-windows-amd64.exe",
}

# A stalled download must fail, not hang ``poe generate``; the release CDN
# returns 503s in bursts lasting minutes (the reason CI's download goes through
# ``with-retry.sh``), so a 5xx or dropped connection earns another attempt. A
# 4xx — a pin with no such asset — fails on the first.
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_DOWNLOAD_MAX_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_SECONDS = 5.0

# Accepts pkl's own `--version` banner ("Pkl 0.32.1 (macOS 26.4, native)") and a
# bare version, and tolerates a pre-release suffix so an rc pin still parses.
_VERSION_RE = re.compile(r"(?:Pkl\s+)?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")


def _cache_dir(version: str) -> Path:
    return Path.home() / ".cache" / "atlan-sdk" / "pkl" / version


def _binary_name() -> str:
    return "pkl.exe" if platform.system().lower() == "windows" else "pkl"


def platform_key() -> tuple[str, str]:
    """Return ``(os, arch)`` normalised to the keys of :data:`_ASSETS`."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        raise UnsupportedPklPlatformError(
            os_name=platform.system(), architecture=platform.machine()
        )
    if (system, arch) not in _ASSETS:
        raise UnsupportedPklPlatformError(
            os_name=platform.system(), architecture=platform.machine()
        )
    return system, arch


def asset_name() -> str:
    """The pkl release asset published for the current platform."""
    return _ASSETS[platform_key()]


def asset_url(version: str = PKL_VERSION) -> str:
    """The download URL for *version* on the current platform."""
    return _RELEASE_ASSET_URL.format(version=version, asset=asset_name())


def _fetch(url: str, dest: Path) -> None:
    """Stream *url* into *dest*, bounded by a socket timeout."""
    fetch_to_file(url, dest, timeout=_DOWNLOAD_TIMEOUT_SECONDS)


def _download(url: str, target: Path) -> None:
    """Fetch *url* to *target* atomically, retrying what a retry can fix.

    Raises :class:`PklDownloadError` carrying the number of attempts made — one
    for a 4xx, which no retry will change.
    """
    made = 1

    def _on_retry(attempt: int, attempts: int, exc: BaseException) -> None:
        nonlocal made
        made = attempt + 1
        _progress(
            f"pkl download failed (attempt {attempt}/{attempts}): {exc} — "
            f"retrying in {_DOWNLOAD_BACKOFF_SECONDS * attempt:.0f}s"
        )

    try:
        fetch_release_asset(
            url,
            target,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            attempts=_DOWNLOAD_MAX_ATTEMPTS,
            backoff_seconds=_DOWNLOAD_BACKOFF_SECONDS,
            retryable=is_retryable_network_error,
            fetch=_fetch,
            on_retry=_on_retry,
        )
    except NETWORK_ERRORS as exc:
        raise PklDownloadError(asset_url=url, attempts=made) from exc
    _progress(f"pkl {target.parent.name} cached at {target}")


def _pkl_runs(binary: Path, version: str) -> bool:
    """True when *binary* executes and reports *version*."""
    try:
        result = subprocess.run(  # fixed argv, our own cached binary
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and parse_version(result.stdout) == version


def ensure_pkl(version: str = PKL_VERSION) -> Path:
    """Return the path to the pinned pkl binary, downloading it if absent.

    New downloads are published atomically, but a cache written before that —
    or damaged since — can still hold a truncated binary at this path. An
    existing file is therefore trusted only if it runs and reports *version*;
    otherwise it is replaced.
    """
    binary = _cache_dir(version) / _binary_name()
    if binary.exists() and not _pkl_runs(binary, version):
        _progress(f"Cached pkl at {binary} does not run; re-downloading")
        binary.unlink(missing_ok=True)
    if not binary.exists():
        os_name, arch = platform_key()
        _progress(f"Downloading pkl {version} for {os_name}/{arch} …")
        _download(asset_url(version), binary)
    return binary


def parse_version(text: str) -> str | None:
    """Extract a version from a ``pkl --version`` banner, or ``None``."""
    match = _VERSION_RE.search(text.strip())
    return match.group(1) if match else None


def runtime_version(binary: str | Path = "pkl") -> str | None:
    """Version reported by *binary*, or ``None`` if it cannot be run or read."""
    try:
        result = subprocess.run(
            [str(binary), "--version"], text=True, capture_output=True
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return parse_version(result.stdout)


def _cmd_print_version(_args: argparse.Namespace) -> int:
    print(PKL_VERSION)
    return 0


def _cmd_path(_args: argparse.Namespace) -> int:
    print(ensure_pkl())
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    binary = ensure_pkl()
    return subprocess.run([str(binary), *args.pkl_args]).returncode


def _cmd_check(args: argparse.Namespace) -> int:
    """Compare the pkl on ``PATH`` against the pin.

    Advisory with ``--warn``: an app that has not adopted the pinned binary
    still wants to be told, and a hard failure there would break the very
    ``poe generate`` the developer is running.
    """
    found = runtime_version(args.binary)
    if found is None:
        print(
            f"pkl not found on PATH (or unreadable). CI renders contracts with "
            f"pkl {PKL_VERSION}; install it, or render through "
            f"'python -m application_sdk.dev.pkl run -- …'.",
            file=sys.stderr,
        )
        return 0 if args.warn else 1
    if found != PKL_VERSION:
        print(
            f"pkl version skew: '{args.binary}' is {found}, CI renders contracts "
            f"with {PKL_VERSION}. pkl is a language, so the two can disagree on "
            f"what your contract even means — a clean local render is not "
            f"evidence the freshness gate will pass. Render through the pinned "
            f"binary instead: python -m application_sdk.dev.pkl run -- "
            f"eval --project-dir contract -m . contract/app.pkl",
            file=sys.stderr,
        )
        return 0 if args.warn else 1
    print(f"pkl {found} matches the SDK pin.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m application_sdk.dev.pkl", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser(
        "print-version", help="print the pkl version CI renders contracts with"
    )
    p_version.set_defaults(func=_cmd_print_version)

    p_path = sub.add_parser(
        "path", help="print the cached pinned pkl binary, downloading if needed"
    )
    p_path.set_defaults(func=_cmd_path)

    p_run = sub.add_parser("run", help="run the pinned pkl with the given arguments")
    p_run.add_argument("pkl_args", nargs=argparse.REMAINDER, metavar="-- ARGS")
    p_run.set_defaults(func=_cmd_run)

    p_check = sub.add_parser(
        "check", help="compare the pkl on PATH against the SDK pin"
    )
    p_check.add_argument(
        "--binary", default="pkl", help="binary to interrogate (default: pkl)"
    )
    p_check.add_argument(
        "--warn",
        action="store_true",
        help="report skew but exit 0, so a generate task is not broken by it",
    )
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    # `run -- eval …` leaves the separator in REMAINDER; drop it so it is not
    # passed to pkl as an argument.
    if getattr(args, "pkl_args", None) and args.pkl_args[0] == "--":
        args.pkl_args = args.pkl_args[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
