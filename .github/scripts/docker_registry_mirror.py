#!/usr/bin/env python3
"""Point the runner's Docker daemon at a Docker Hub pull-through mirror.

Integration tests pull Docker Hub images (``mysql:8.0``, testcontainers'
``ryuk``) anonymously from a shared runner IP, so they hit Docker Hub's
unauthenticated rate limit and its intermittent 5xx. Either error fails test
setup before any test runs, and the merge queue ejects the PR.

``registry-mirrors`` in ``daemon.json`` makes the daemon try the mirror first
for ``docker.io`` images and fall back to Docker Hub when the mirror misses or
errors. Image names don't change, and testcontainers needs nothing, because it
pulls through the daemon. Other registries (ghcr.io, ...) are unaffected.

The step is best-effort, with one exception. The mirror only reduces failures,
so a ``daemon.json`` this script can't parse is left untouched, and the step
emits a warning instead of failing. Restarting the daemon is the one action
that can break the job: if Docker doesn't come back, the original file is
restored and the daemon restarted again. The step fails only if Docker is
still down after that, because the tests can't run without it.

Run as root (it writes ``/etc/docker/daemon.json`` and restarts the service):
    sudo python3 docker_registry_mirror.py --mirror https://mirror.gcr.io
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_DAEMON_JSON = Path("/etc/docker/daemon.json")
MIRRORS_KEY = "registry-mirrors"
# `docker info` exits non-zero until the restarted daemon answers on its socket.
_MIRRORS_FORMAT = "{{json .RegistryConfig.Mirrors}}"


class DaemonConfigError(Exception):
    """The existing daemon.json can't be safely merged into."""


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Single seam for every external command, so tests can stub it."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def load_config(path: Path) -> dict[str, Any] | None:
    """Return the parsed daemon.json, or None when there is none to merge into."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DaemonConfigError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(config, dict):
        raise DaemonConfigError(f"{path} is not a JSON object")
    existing = config.get(MIRRORS_KEY)
    if existing is not None and not (
        isinstance(existing, list) and all(isinstance(m, str) for m in existing)
    ):
        raise DaemonConfigError(f"{path} has a non-list {MIRRORS_KEY!r}")
    return config


def merge_mirror(config: dict[str, Any] | None, mirror: str) -> dict[str, Any]:
    """Put ``mirror`` first in registry-mirrors, keeping every other key."""
    merged = dict(config or {})
    others = [m for m in merged.get(MIRRORS_KEY) or [] if m != mirror]
    merged[MIRRORS_KEY] = [mirror, *others]
    return merged


def write_config(path: Path, config: dict[str, Any]) -> None:
    """Replace ``path`` atomically, so a crash can't leave half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".daemon.json.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def restart_and_wait(timeout_s: float) -> list[str] | None:
    """Restart Docker; return its mirrors once it answers, or None on timeout."""
    restart = run(["systemctl", "restart", "docker"])
    if restart.returncode != 0:
        print(f"systemctl restart docker failed: {restart.stderr.strip()}")
    deadline = time.monotonic() + timeout_s
    while True:
        info = run(["docker", "info", "--format", _MIRRORS_FORMAT])
        if info.returncode == 0:
            try:
                mirrors = json.loads(info.stdout.strip() or "null")
            except json.JSONDecodeError:
                mirrors = None
            return mirrors if isinstance(mirrors, list) else []
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def configure(daemon_json: Path, mirror: str, timeout_s: float) -> int:
    try:
        original = load_config(daemon_json)
    except DaemonConfigError as exc:
        print(f"::warning::not adding Docker Hub mirror {mirror}: {exc}")
        return 0

    merged = merge_mirror(original, mirror)
    if merged == original:
        print(f"{daemon_json} already lists {mirror}; not restarting Docker.")
        return 0

    original_text = (
        daemon_json.read_text(encoding="utf-8") if daemon_json.exists() else None
    )
    write_config(daemon_json, merged)
    mirrors = restart_and_wait(timeout_s)
    if mirrors is not None:
        if mirror in mirrors:
            print(
                f"Docker Hub pulls now try {mirror} first (Registry Mirrors: {mirrors})."
            )
        else:
            print(
                f"::warning::Docker restarted but does not report {mirror} as a "
                f"registry mirror (got {mirrors}); pulls go straight to Docker Hub."
            )
        return 0

    # Docker didn't come back on the new config: put the old one back.
    if original_text is None:
        daemon_json.unlink(missing_ok=True)
    else:
        daemon_json.write_text(original_text, encoding="utf-8")
    if restart_and_wait(timeout_s) is not None:
        print(
            f"::warning::Docker did not restart with registry mirror {mirror}; "
            "restored the previous daemon.json. Pulls go straight to Docker Hub."
        )
        return 0
    print(
        f"::error::Docker is not running after restoring {daemon_json}; "
        "integration tests can't pull or start containers."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mirror", required=True, help="mirror URL, e.g. https://mirror.gcr.io"
    )
    parser.add_argument("--daemon-json", type=Path, default=DEFAULT_DAEMON_JSON)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not args.mirror.startswith("https://"):
        parser.error("--mirror must be an https:// URL")
    return configure(args.daemon_json, args.mirror, args.timeout_seconds)


if __name__ == "__main__":
    sys.exit(main())
