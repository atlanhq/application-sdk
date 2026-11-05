"""Authenticate atlanhq git remotes for one command, through its env alone.

An app with a PRIVATE atlanhq git dependency needs a GitHub token for the
commands that fetch it: ``uv sync`` / ``uv lock`` on a cold cache. Writing the
token into git config (``git config --global url.<authed>.insteadOf``) leaves
it readable by every later step of the job, including tests, hooks and
anything else that runs app code. This hands it to git as ``GIT_CONFIG_*``
environment variables on the one child process instead: git reads them as if
they were ``-c`` options, every git process the child starts inherits them,
and nothing is persisted.

Usage, from a workflow ``run:`` step::

    python3 .github/scripts/git_auth.py run -- uv sync --all-extras --all-groups

Environment:

``GIT_TOKEN``
    The token. Removed from the child's env: it only reaches the child inside
    the rewrite. Empty or unset runs the command without authentication,
    exactly as if this wrapper were not there.
``PRIVATE_GIT_DEPS``
    ``true`` makes a missing token an error. A caller that declared private
    git dependencies but got no token would otherwise fail later, inside uv,
    on an unauthenticated clone that says nothing about why.

Once one authenticated sync has populated the job's uv cache and venv, later
``uv sync`` / ``uv run`` / ``uv lock --check`` calls, and even a re-lock of a
stale lock, do not fetch again (verified against a blocked github.com for
FND-4740), so only the first install needs this wrapper.

Shared by the certify gate (``build_and_publish/certify.py install-deps``), the
tests / checks / conformance reusables and the composite actions they call, and
release-version-bump.yaml. See docs/standards/ci.md.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

#: Rewritten to carry the token: an app's lock may name either form, and the
#: fleet's private deps are written ``ssh://git@github.com/atlanhq/...``.
ATLANHQ_REMOTES = ("https://github.com/atlanhq/", "ssh://git@github.com/atlanhq/")

TOKEN_VAR = "GIT_TOKEN"
REQUIRED_VAR = "PRIVATE_GIT_DEPS"

MISSING_TOKEN_ERROR = (
    "::error title=No token for private git dependencies::"
    "Private git dependencies are declared but the fleet App token did not "
    "mint. Pass FLEET_APP_ID and FLEET_APP_PRIVATE_KEY to the reusable "
    "(`secrets: inherit`, or name both in an explicit `secrets:` mapping). "
    "Dependencies were not installed."
)


def git_auth_env(token: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``GIT_CONFIG_*`` entries that point atlanhq remotes at an authed URL.

    Equivalent to ``git -c url.<authed>.insteadOf=<remote>`` for each remote.
    Entries already in *base* (an env that sets its own ``GIT_CONFIG_COUNT``)
    are kept: the new ones are numbered after them.
    """
    base = base or {}
    start = int(base.get("GIT_CONFIG_COUNT", "0") or "0")
    authed = f"https://x-access-token:{token}@github.com/atlanhq/"
    env = {"GIT_CONFIG_COUNT": str(start + len(ATLANHQ_REMOTES))}
    for i, remote in enumerate(ATLANHQ_REMOTES, start=start):
        env[f"GIT_CONFIG_KEY_{i}"] = f"url.{authed}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{i}"] = remote
    return env


def child_env(environ: Mapping[str, str], *, required: bool) -> dict[str, str] | None:
    """The env to run the command with, or ``None`` when a required token is missing.

    The token variable itself never reaches the child.
    """
    token = environ.get(TOKEN_VAR, "")
    env = {k: v for k, v in environ.items() if k != TOKEN_VAR}
    if not token:
        return None if required else env
    env.update(git_auth_env(token, env))
    return env


def run(command: Sequence[str], environ: Mapping[str, str] | None = None) -> int:
    """Run *command* with atlanhq remotes authenticated; return its exit code."""
    environ = os.environ if environ is None else environ
    env = child_env(environ, required=environ.get(REQUIRED_VAR, "") == "true")
    if env is None:
        print(MISSING_TOKEN_ERROR, flush=True)
        return 1
    return subprocess.run(list(command), env=env, check=False).returncode  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a command with atlanhq git remotes authenticated."
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    run_parser = sub.add_parser(
        "run", help="run a command with atlanhq remotes authenticated"
    )
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("run needs a command after --")
    return run(command)


if __name__ == "__main__":
    sys.exit(main())
