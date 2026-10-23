#!/usr/bin/env python3
"""Wait until a first-party release is consistently visible on PyPI.

Invoked by the release fan-out jobs in tag-and-publish.yaml (SDK + api) and
conformance-publish.yml before they dispatch renovate.yaml (FND-4330). The
contract-toolkit fan-out already waits the same way for GitHub Pages via
wait_for_pages_publish.py; this is the PyPI counterpart.

WHY. A `uv publish` returning is not the same as every reader seeing the
release. On 2026-10-22 the fan-out fired seconds after publish and the sweep's
per-repo lookups ran about three minutes later: one repo's lookup of the
conformance package returned a release list ending at the previous version,
while another repo's lookup in the same second saw the new one, and a third
repo's `uv lock --upgrade-package` resolved to an unchanged lock. Each of those
repos then waited for the next scheduled sweep, two to four hours later.

WHAT IS WAITED FOR. Every package passed in, not just the one this workflow
published. The SDK and conformance bumps are separate PRs that routinely merge
seconds apart, and both fan-out jobs share one cancel-in-progress concurrency
group, so the job that survives reads every first-party version from `main`
and waits for all of them. That is what turns two releases into one fan-out.

HOW. Each round probes both index APIs a reader may use (the PEP 691 Simple
JSON API, which uv reads, and the legacy `/pypi/<name>/json`, which Renovate's
pypi datasource reads by default), each twice: once with a cache-busting query
string (has the origin got it?) and once plain (has this CDN edge got it?).
A round passes only when every probe shows the version. A single good probe
proves nothing, since two probes in the same second can disagree, so the
release must pass `--consecutive` rounds in a row, followed by a
`--settle-seconds` buffer for the remaining edges to converge.

A TIMEOUT IS NOT A SKIP. If the budget runs out, the script prints a warning
and exits 0, so the fan-out still dispatches. A late sweep gets most repos and
the post-sweep verification re-dispatches the rest, while not dispatching at
all would leave the whole fleet for the scheduled sweep.

Writes `packages=<JSON {name: version}>` to $GITHUB_OUTPUT for the follow-up
step (renovate_release_followup.py), which verifies the fleet against exactly
these versions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path
from typing import Callable

SIMPLE_URL = "https://pypi.org/simple/{name}/"
LEGACY_JSON_URL = "https://pypi.org/pypi/{name}/json"
SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess and capture output. Single seam so tests can stub curl."""
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def read_release(pyproject: Path) -> tuple[str, str]:
    """(name, version) from a pyproject.toml with a static [project] version."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    return str(project["name"]), str(project["version"])


def _fetch_json(url: str, accept: str | None) -> dict | None:
    """GET `url` as JSON, or None on any failure (a failed probe is a miss)."""
    cmd = ["curl", "-sS", "--fail", "--max-time", "15"]
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    result = run(cmd + [url])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _simple_has(payload: dict | None, version: str) -> bool:
    # `versions` alone is not enough for an installer: uv needs a file to
    # download, and a version can be listed before its files are.
    if payload is None or version not in (payload.get("versions") or []):
        return False
    files = payload.get("files") or []
    return any(f"-{version}" in str(f.get("filename", "")) for f in files)


def _legacy_has(payload: dict | None, version: str) -> bool:
    if payload is None:
        return False
    return bool((payload.get("releases") or {}).get(version))


def probe(name: str, version: str, nonce: Callable[[], str]) -> bool:
    """True only if all four probes (two APIs, busted and plain) see `version`."""
    simple = SIMPLE_URL.format(name=name)
    legacy = LEGACY_JSON_URL.format(name=name)
    for bust in (f"?cachebust={nonce()}", ""):
        if not _simple_has(_fetch_json(simple + bust, SIMPLE_JSON_ACCEPT), version):
            return False
        if not _legacy_has(_fetch_json(legacy + bust, None), version):
            return False
    return True


def wait_for_release(
    packages: dict[str, str],
    *,
    consecutive: int = 3,
    interval_seconds: int = 10,
    settle_seconds: int = 60,
    timeout_seconds: int = 900,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    nonce: Callable[[], str] | None = None,
) -> bool:
    """Poll until every package passes `consecutive` rounds in a row, then settle.

    Returns True when visible, False on timeout. `sleep`/`monotonic` are
    resolved at call time rather than bound as defaults, so a test patching
    `time` is honoured (see poll_dispatched_run.py for why that matters).
    """
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    nonce = nonce or (lambda: uuid.uuid4().hex)

    started = monotonic()
    streak = 0
    while True:
        missing = [n for n, v in packages.items() if not probe(n, v, nonce)]
        if missing:
            if streak:
                print(f"Visibility streak reset at {streak}: {', '.join(missing)}")
            streak = 0
        else:
            streak += 1
            print(f"All packages visible ({streak}/{consecutive} consecutive rounds)")
            if streak >= consecutive:
                print(f"Settling {settle_seconds}s for CDN edges to converge")
                sleep(settle_seconds)
                return True

        elapsed = monotonic() - started
        if elapsed >= timeout_seconds:
            pending = missing or list(packages)
            print(
                f"::warning::{', '.join(f'{n}=={packages[n]}' for n in pending)} not "
                f"consistently visible on PyPI after {int(elapsed)}s; dispatching "
                "Renovate anyway, and the post-sweep check re-dispatches the repos "
                "it misses."
            )
            return False
        sleep(interval_seconds)


def write_output(packages: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"packages={json.dumps(packages, sort_keys=True)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pyproject",
        action="append",
        required=True,
        type=Path,
        help="pyproject.toml of a package to wait for; repeatable.",
    )
    parser.add_argument("--consecutive", type=int, default=3)
    parser.add_argument("--interval-seconds", type=int, default=10)
    parser.add_argument("--settle-seconds", type=int, default=60)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Give up (warn, exit 0) after this long. Default 15 minutes.",
    )
    args = parser.parse_args(argv)

    packages = dict(read_release(p) for p in args.pyproject)
    print(
        "Waiting for "
        + ", ".join(f"{n}=={v}" for n, v in sorted(packages.items()))
        + " on PyPI"
    )
    # Written before the wait, so the follow-up step has the versions even if
    # this step is cancelled or times out.
    write_output(packages)
    wait_for_release(
        packages,
        consecutive=args.consecutive,
        interval_seconds=args.interval_seconds,
        settle_seconds=args.settle_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
