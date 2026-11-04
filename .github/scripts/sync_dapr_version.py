#!/usr/bin/env python3
"""Sync ``__dapr_version`` with the daprd baked into the runtime base image.

Driver for ``.github/workflows/check-dapr-version.yaml``; see that workflow for
why the shipped image, not dapr/dapr's upstream releases, is the source of
truth. One subcommand per workflow step:

``pinned``   read ``__dapr_version`` from ``application_sdk/version.py``.
``baked``    pull the image, run ``daprd --version`` in it, parse the semver.
``compare``  write ``pinned``/``baked``/``match`` step outputs.
``bump``     rewrite the pin in version.py and check the rewrite landed.
``open-pr``  commit the bump on ``chore/sync-daprd-<baked>`` and open (or
             reuse) its PR, writing ``pr_number`` for the approve step.

Every external command goes through :func:`_run`, so tests stub git, gh and
docker and exercise the real branching.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION_FILE = Path("application_sdk/version.py")
_PIN = re.compile(r'^__dapr_version[^\n"]*"([^"]*)"', re.M)
_SEMVER = re.compile(r"v?([0-9]+\.[0-9]+\.[0-9]+)")
BASE = "main"


class SyncError(Exception):
    """A step failed; the message becomes the ``::error::`` annotation."""


def _run(
    args: list[str], *, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    else:
        sys.stdout.write(lines)


def read_pin(version_file: Path) -> str:
    match = _PIN.search(version_file.read_text(encoding="utf-8"))
    version = match.group(1) if match else ""
    if not version:
        raise SyncError(f"Failed to parse __dapr_version from {version_file}")
    return version


def parse_daprd_version(raw: str) -> str:
    """First semver in ``daprd --version`` output, without a leading ``v``.

    Deliberately not anchored on a prefix label, which differs across daprd
    builds.
    """
    match = _SEMVER.search(raw)
    if not match:
        raise SyncError(
            "Could not extract a valid daprd version from the base image "
            "(see raw output above)"
        )
    return match.group(1)


def baked_version(image: str) -> str:
    # Public image: anonymous pull, no registry login needed.
    _run(["docker", "pull", image], capture=False)
    # daprd lives at /usr/bin/daprd in the runtime base. A non-zero exit still
    # prints the version on some builds, so the output is read either way.
    raw = _run(
        ["docker", "run", "--rm", "--entrypoint", "/usr/bin/daprd", image, "--version"],
        check=False,
    ).stdout
    # Logged unconditionally so a format change in a future daprd is visible.
    print("::group::daprd --version (raw)")
    print(raw.rstrip("\n"))
    print("::endgroup::")
    version = parse_daprd_version(raw)
    print(f"::notice::Base image daprd version: {version}")
    return version


def compare(pinned: str, baked: str) -> bool:
    match = pinned == baked
    _output(pinned=pinned, baked=baked, match=str(match).lower())
    if match:
        print(f"::notice::daprd pin is in sync with the base image ({pinned})")
    else:
        print(f"::notice::daprd pin {pinned} differs from base image {baked}")
    return match


def rewrite_pin(version_file: Path, pinned: str, baked: str) -> None:
    """Replace the exact ``__dapr_version: str = "<pinned>"`` line, if present."""
    line = re.compile(rf'^__dapr_version: str = "{re.escape(pinned)}"$', flags=re.M)
    text = version_file.read_text(encoding="utf-8")
    version_file.write_text(
        line.sub(f'__dapr_version: str = "{baked}"', text), encoding="utf-8"
    )


def bump(version_file: Path, pinned: str, baked: str) -> None:
    rewrite_pin(version_file, pinned, baked)
    new = read_pin(version_file)
    if new != baked:
        raise SyncError(
            f"version.py was not rewritten as expected (got '{new}', want '{baked}')"
        )


def pr_body(pinned: str, baked: str, image: str) -> str:
    return f"""The daprd shipped in the runtime base image is now **{baked}**.

| | version |
|---|---|
| Current pin | `{pinned}` |
| Runtime image daprd | `{baked}` |
| Runtime image | `{image}` |

This PR syncs `__dapr_version` in [`application_sdk/version.py`](application_sdk/version.py) to match the daprd that actually ships in the container, so:

- The embedded local-dev daprd (via `application_sdk.dev._dapr`) downloads the same release the container runs.
- CI workflows that read `__dapr_version` boot the matching daprd.

The version was read from `{image}`'s `daprd --version` (public image, pulled anonymously).

Opened automatically by [`.github/workflows/check-dapr-version.yaml`](.github/workflows/check-dapr-version.yaml)."""


def _open_pr_number(branch: str) -> str:
    return _run(
        [
            "gh", "pr", "list",
            "--head", branch, "--base", BASE, "--state", "open",
            "--json", "number", "--jq", ".[0].number",
        ]
    ).stdout.strip()  # fmt: skip


def open_pr(version_file: Path, pinned: str, baked: str, image: str) -> str:
    """Push the bump and return the open PR's number ("" when there is none)."""
    _run(["git", "config", "--global", "user.name", "atlan-app-fleet[bot]"])
    _run(
        [
            "git", "config", "--global", "user.email",
            "atlan-app-fleet[bot]@users.noreply.github.com",
        ]
    )  # fmt: skip
    # Per-version branch: a re-run against the same baked version lands back
    # on the existing PR instead of opening a new one.
    branch = f"chore/sync-daprd-{baked}"
    exists = (
        _run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
            check=False,
        ).returncode
        == 0
    )
    if exists:
        _run(["git", "fetch", "origin", branch], capture=False)
        _run(["git", "checkout", branch], capture=False)
        # Normally a no-op: re-applies the pin only if the branch's version.py
        # drifted from <baked>, e.g. after a manual edit.
        rewrite_pin(version_file, pinned, baked)
    else:
        _run(["git", "checkout", "-b", branch], capture=False)

    _run(["git", "add", str(version_file)])
    if _run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        print("::notice::No actual change to commit; PR may already be up to date.")
        # Still surface an open PR so the approve + queue step can (re-)act on
        # one whose approval a later push dismissed.
        return _open_pr_number(branch)

    _run(
        [
            "git",
            "commit",
            "-m",
            f"chore(deps): sync daprd pin to {baked} from base image",
        ],
        capture=False,
    )
    _run(["git", "push", "origin", branch, "--force-with-lease"], capture=False)

    existing = _open_pr_number(branch)
    if existing:
        print(f"::notice::PR #{existing} already open for {branch}; commit pushed.")
        return existing

    url = _run(
        [
            "gh", "pr", "create",
            "--base", BASE,
            "--title", f"chore(deps): sync daprd pin to {baked}",
            "--label", "dependencies",
            "--body", pr_body(pinned, baked, image),
        ]
    ).stdout.strip()  # fmt: skip
    return url.rsplit("/", 1)[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-file", type=Path, default=VERSION_FILE)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pinned")
    p = sub.add_parser("baked")
    p.add_argument("--image", required=True)
    for name in ("compare", "bump", "open-pr"):
        p = sub.add_parser(name)
        p.add_argument("--pinned", required=True)
        p.add_argument("--baked", required=True)
        if name == "open-pr":
            p.add_argument("--image", required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "pinned":
            _output(version=read_pin(args.version_file))
        elif args.cmd == "baked":
            _output(version=baked_version(args.image))
        elif args.cmd == "compare":
            compare(args.pinned, args.baked)
        elif args.cmd == "bump":
            bump(args.version_file, args.pinned, args.baked)
        else:
            number = open_pr(args.version_file, args.pinned, args.baked, args.image)
            _output(pr_number=number)
    except SyncError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
