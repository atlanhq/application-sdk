"""Install an app's private atlanhq git dependencies without the token reaching uv.

An app with a PRIVATE atlanhq git dependency needs a GitHub token for the
commands that fetch it: ``uv sync`` / ``uv lock`` on a cold cache. Those
commands also run PEP 517 build backends and build hooks, which are app or
dependency code running as the job's user. They can read uv's environment, and
the environment of every ancestor process (``/proc/<pid>/environ``), so the
token must be in neither: not in uv's env, and not in the env of the step
shell or wrapper that starts uv.

So the token is used in its own workflow step, which exits before uv runs:

1. ``git_auth.py mirror`` (the step whose ``env:`` holds ``GIT_TOKEN``) clones
   each private atlanhq repository the project names with
   ``git clone --mirror`` into ``$RUNNER_TEMP/git-mirrors``. Only git sees the
   token, as a ``GIT_CONFIG_*`` rewrite on that git process. A bare mirror
   clone runs no repository code (no checkout, no hooks, no filters), and git
   stores the remote URL as written, so the token is not left in the mirror.
2. ``git_auth.py run -- <cmd>`` (a later step with no token in its env) runs
   the command with a token-free rewrite pointing each mirrored remote at its
   mirror (``url.file://<mirror>.insteadOf``). uv fetches from the mirrors and
   the lock keeps the original URLs. ``run`` refuses to start if ``GIT_TOKEN``
   is set: the token in that step's env is exactly what this split prevents.

Public atlanhq repositories are not mirrored: uv fetches them from GitHub,
unauthenticated, as before. With no mirrors, ``run`` runs the command plain.

Usage, from workflow ``run:`` steps::

    # step 1: env GIT_TOKEN, PRIVATE_GIT_DEPS
    python3 .github/scripts/git_auth.py mirror
    # step 2: no token
    python3 .github/scripts/git_auth.py run -- uv sync --all-extras --all-groups
    # before the mint: the private repos, for a scoped token
    python3 .github/scripts/git_auth.py repos

Environment for ``mirror``:

``GIT_TOKEN``
    The token. Empty or unset with ``PRIVATE_GIT_DEPS`` unset mirrors nothing.
``PRIVATE_GIT_DEPS``
    ``true`` makes a missing token an error when the project names a private
    atlanhq repository. A caller that declared private git dependencies but
    got no token would otherwise fail later, inside uv, on an unauthenticated
    clone that says nothing about why.

``repos`` prints the private atlanhq repositories the project names (and
fails when one is not in ``ALLOWED_PRIVATE_DEPS``, unless the repository is an
``AGGREGATOR_REPOS`` entry), comma separated, and writes ``repositories=<list>`` to ``$GITHUB_OUTPUT`` when it is
set. Workflows mint the fleet App token scoped to exactly that list
(``actions/create-github-app-token``'s ``repositories``), and skip the mint
when it is empty: an empty ``repositories`` would grant every repository in
the installation.

A repository counts as private when an unauthenticated ``git ls-remote`` of it
fails. The project's references are read from every ``pyproject.toml`` and
``uv.lock`` under the project directory (git sources: ``git = "..."`` and
``git+https://`` / ``git+ssh://`` requirements), so a sub-package's private
dependency is found too.

Once one sync has populated the job's uv cache and venv, later ``uv sync`` /
``uv run`` / ``uv lock --check`` calls, and even a re-lock of a stale lock, do
not fetch again (verified against a blocked github.com for FND-4740).

Shared by the certify gate (``build_and_publish/certify.py install-deps``), the
tests / checks / conformance reusables and the composite actions they call,
release-version-bump.yaml, and the ``repos`` step every scoped token mint runs
before minting. See docs/standards/ci.md.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

GITHUB_ATLANHQ = "https://github.com/atlanhq/"

#: Remote forms a lock or pyproject may name an atlanhq dependency by. Both are
#: pointed at the local mirror; the fleet's private deps are mostly written
#: ``ssh://git@github.com/atlanhq/...``.
ATLANHQ_REMOTES = (GITHUB_ATLANHQ, "ssh://git@github.com/atlanhq/")

TOKEN_VAR = "GIT_TOKEN"
REQUIRED_VAR = "PRIVATE_GIT_DEPS"

#: Files that name a project's git dependencies.
SCANNED_FILES = frozenset({"pyproject.toml", "uv.lock"})
#: Never descended into, besides any dot-directory (.git, .venv, ...).
SKIPPED_DIRS = frozenset({"node_modules", "__pycache__", "venv"})

#: A git source of an atlanhq repository: ``git = "<url>"`` (uv.lock sources,
#: ``[tool.uv.sources]``) or a ``git+<url>`` requirement.
_GIT_SOURCE = re.compile(
    r"""(?:\bgit\s*=\s*["']|\bgit\+)"""
    r"(?:https://|ssh://git@)github\.com/atlanhq/([A-Za-z0-9._-]+)"
)

#: The private atlanhq repositories an app may name as a git dependency, and so
#: the most a private-dependency token is ever scoped to. A PR controls its own
#: pyproject.toml / uv.lock, so the scope cannot come from them alone: an app
#: naming a private repository not listed here fails closed, in both ``repos``
#: (before any token is minted) and ``mirror``. This file is read from the
#: workflow's own commit (``job.workflow_sha``), never from the PR's tree, so
#: only an SDK change can widen it (lens F-6e4829 on #4731).
#:
#: Private git dependencies are an anti-pattern (there is no private package
#: registry); this list is the existing debt, not an invitation. Do not add to
#: it without a plan to remove the entry.
ALLOWED_PRIVATE_DEPS = frozenset(
    {
        # The query redactors (query-redaction), used by 9 SQL connectors.
        "atlan-query-intelligence-app",
        # Atlan's SQLGlot fork, installed in place of upstream sqlglot.
        # Only atlan-query-intelligence-app depends on it.
        "sqlglot-for-lorien",
        # MIGRATE (near-term): consumers (atlan-enrichment-studio-app,
        # atlan-memory-app) move to the atlan-application-sdk-api package this
        # repo publishes, then this entry is removed.
        "server-sdk",
        # MIGRATE (near-term): the usage-metering client
        # (ai_application_sdk.metering), pinned to a different raw commit by
        # each consumer (atlan-automation-engine-app,
        # atlan-enrichment-studio-app, wisdom). Move it to a published package,
        # then remove this entry.
        "ai-application-sdk",
    }
)

#: Repositories that may name ANY private atlanhq repository: aggregators that
#: bundle other apps. Matched against ``GITHUB_REPOSITORY``, which GitHub sets
#: for the run and a PR cannot change. Their own PRs can widen their token's
#: scope; that is the accepted cost of an aggregator.
AGGREGATOR_REPOS = frozenset({"atlanhq/common-app-server"})

GIT_TIMEOUT_SECONDS = 600
LS_REMOTE_TIMEOUT_SECONDS = 60

MISSING_TOKEN_ERROR = (
    "::error title=No token for private git dependencies::"
    "Private git dependencies are declared but the fleet App token did not "
    "mint. Pass FLEET_APP_ID and FLEET_APP_PRIVATE_KEY to the reusable "
    "(`secrets: inherit`, or name both in an explicit `secrets:` mapping). "
    "Dependencies were not installed."
)

Execute = Callable[[Sequence[str], dict[str, str]], int]
IsPublic = Callable[[str], bool]


def rewrite_env(
    rewrites: Mapping[str, str], base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``GIT_CONFIG_*`` entries for ``url.<target>.insteadOf=<remote>``.

    *rewrites* maps each remote prefix to its target. Entries already in
    *base* (an env that sets its own ``GIT_CONFIG_COUNT``) are kept: the new
    ones are numbered after them.
    """
    base = base or {}
    start = int(base.get("GIT_CONFIG_COUNT", "0") or "0")
    env = {"GIT_CONFIG_COUNT": str(start + len(rewrites))}
    for i, (remote, target) in enumerate(rewrites.items(), start=start):
        env[f"GIT_CONFIG_KEY_{i}"] = f"url.{target}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{i}"] = remote
    return env


def git_auth_env(token: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``GIT_CONFIG_*`` entries pointing atlanhq remotes at an authed URL.

    Only ever given to the ``git clone --mirror`` processes, never to uv.
    """
    authed = f"https://x-access-token:{token}@github.com/atlanhq/"
    return rewrite_env(dict.fromkeys(ATLANHQ_REMOTES, authed), base)


def _repo_name(match: str) -> str:
    return match.removesuffix(".git")


def referenced_repos(project_dir: str | os.PathLike[str] = ".") -> list[str]:
    """The atlanhq repositories the project names as git sources."""
    names: set[str] = set()
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in SKIPPED_DIRS
        )
        for name in files:
            if name in SCANNED_FILES:
                text = Path(root, name).read_text(encoding="utf-8", errors="replace")
                # A commented-out source (`# x = { git = "..." }`) is not a
                # dependency, and must not widen the token's scope.
                live = "\n".join(
                    line
                    for line in text.splitlines()
                    if not line.lstrip().startswith("#")
                )
                names.update(_repo_name(m) for m in _GIT_SOURCE.findall(live))
    return sorted(n for n in names if n)


def _anonymous_git_env() -> dict[str, str]:
    """An env in which git has no credential at all: no global or system
    config (so no credential helper, no persisted checkout auth) and no
    prompt."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": tempfile.gettempdir(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def is_public(name: str) -> bool:
    """True when the repository is readable without credentials."""
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                f"{GITHUB_ATLANHQ}{name}",
                "HEAD",
            ],
            env=_anonymous_git_env(),
            cwd=tempfile.gettempdir(),
            capture_output=True,
            check=False,
            timeout=LS_REMOTE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Unknown: treat as private, so it is fetched with the token.
        return False
    return proc.returncode == 0


def private_repos(
    project_dir: str | os.PathLike[str] = ".", public: IsPublic | None = None
) -> list[str]:
    """The private atlanhq repositories the project names."""
    public = is_public if public is None else public
    return [n for n in referenced_repos(project_dir) if not public(n)]


def refused_repos(
    private: Sequence[str], environ: Mapping[str, str] | None = None
) -> list[str]:
    """The private repos in *private* this repository may not fetch."""
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_REPOSITORY", "") in AGGREGATOR_REPOS:
        return []
    return sorted(set(private) - ALLOWED_PRIVATE_DEPS)


def _refuse(refused: Sequence[str]) -> None:
    print(
        "::error title=Private git dependency not allowed::"
        f"This app names private atlanhq repositories that are not on the "
        f"application-sdk allowlist: {', '.join(refused)}. The fleet token is "
        "never scoped to them. Private git dependencies are being removed "
        "from the fleet: depend on a published package instead "
        "(ALLOWED_PRIVATE_DEPS in application-sdk's .github/scripts/git_auth.py).",
        flush=True,
    )


def mirror_set(private: Sequence[str], referenced: Sequence[str]) -> list[str]:
    """The repositories to mirror: the private ones, plus any referenced one
    whose name starts with a private one's. ``insteadOf`` matches by prefix,
    so the rewrite for ``foo`` would otherwise also catch ``foo-bar``."""
    chosen = set(private)
    for name in referenced:
        if any(name != p and name.startswith(p) for p in private):
            chosen.add(name)
    return sorted(chosen)


def mirror(
    names: Sequence[str], dest: Path, clone_rewrite: Mapping[str, str]
) -> str | None:
    """Mirror-clone each repository into *dest*; return the first that failed.

    *clone_rewrite* is the rewrite the clones run under (the token's, in
    production). Each mirror is ``dest/<name>``, with ``dest/<name>.git``
    linked to it so a remote written with ``.git`` resolves too.
    """
    env = {**_anonymous_git_env(), **rewrite_env(clone_rewrite)}
    for name in names:
        target = dest / name
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "clone",
                    "--mirror",
                    "--quiet",
                    f"{GITHUB_ATLANHQ}{name}",
                    str(target),
                ],
                env=env,
                cwd=dest,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return name
        if proc.returncode != 0:
            return name
        (dest / f"{name}.git").symlink_to(name)
    return None


def mirror_env(
    dest: Path, names: Sequence[str], base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Token-free ``GIT_CONFIG_*`` entries pointing each repository's remotes
    at its mirror in *dest*."""
    rewrites = {
        f"{remote}{name}": f"file://{dest / name}"
        for name in names
        for remote in ATLANHQ_REMOTES
    }
    return rewrite_env(rewrites, base)


def _execute(command: Sequence[str], env: dict[str, str]) -> int:
    return subprocess.run(list(command), env=env, check=False).returncode  # noqa: S603


def mirror_dest(environ: Mapping[str, str] | None = None) -> Path:
    """Where ``mirror`` writes and ``run`` reads: ``$RUNNER_TEMP/git-mirrors``."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "git-mirrors"


def prepare_mirrors(
    environ: Mapping[str, str] | None = None,
    *,
    project_dir: str | os.PathLike[str] = ".",
    dest: Path | None = None,
    public: IsPublic | None = None,
    clone_rewrite: Mapping[str, str] | None = None,
) -> int:
    """Mirror the project's private atlanhq deps into *dest*; return 0 or 1.

    *dest* is emptied first, so a stale mirror never survives. *public*
    defaults to :func:`is_public`; *clone_rewrite* replaces the token rewrite
    the clones use (tests point it at local repositories).
    """
    environ = os.environ if environ is None else environ
    dest = mirror_dest(environ) if dest is None else dest
    public = is_public if public is None else public
    shutil.rmtree(dest, ignore_errors=True)
    token = environ.get(TOKEN_VAR, "")
    required = environ.get(REQUIRED_VAR, "") == "true"
    if not token and not required:
        return 0
    referenced = referenced_repos(project_dir)
    private = [n for n in referenced if not public(n)]
    if not private:
        return 0
    refused = refused_repos(private, environ)
    if refused:
        _refuse(refused)
        return 1
    if not token:
        print(MISSING_TOKEN_ERROR, flush=True)
        return 1
    dest.mkdir(parents=True)
    rewrite = clone_rewrite or {
        remote: f"https://x-access-token:{token}@github.com/atlanhq/"
        for remote in ATLANHQ_REMOTES
    }
    failed = mirror(mirror_set(private, referenced), dest, rewrite)
    if failed is not None:
        shutil.rmtree(dest, ignore_errors=True)
        print(
            "::error title=Could not fetch a private git dependency::"
            f"atlanhq/{failed} could not be cloned with the fleet App token. "
            "Check that the atlan-app-fleet App is installed on it. "
            "Dependencies were not installed.",
            flush=True,
        )
        return 1
    return 0


def mirrored_names(dest: Path) -> list[str]:
    """The repositories mirrored in *dest* (the ``.git`` links excluded)."""
    if not dest.is_dir():
        return []
    return sorted(p.name for p in dest.iterdir() if p.is_dir() and not p.is_symlink())


TOKEN_IN_RUN_ERROR = (
    "::error title=Token in the step that runs uv::"
    "GIT_TOKEN is set in the step that runs the install. Build backends can read "
    "the environment of every ancestor process, so pass the token only to the "
    "earlier `git_auth.py mirror` step. Dependencies were not installed."
)


def run(
    command: Sequence[str],
    environ: Mapping[str, str] | None = None,
    *,
    dest: Path | None = None,
    execute: Execute = _execute,
) -> int:
    """Run *command* against the mirrors in *dest*; return its exit code.

    Refuses when ``GIT_TOKEN`` is set: then the token is in this process's
    environment, and so readable by any build backend *command* starts.
    """
    environ = os.environ if environ is None else environ
    if environ.get(TOKEN_VAR, ""):
        print(TOKEN_IN_RUN_ERROR, flush=True)
        return 1
    dest = mirror_dest(environ) if dest is None else dest
    env = {k: v for k, v in environ.items() if k != TOKEN_VAR}
    names = mirrored_names(dest)
    if names:
        env.update(mirror_env(dest, names, env))
    return execute(command, env)


def write_repos_output(
    repos: Sequence[str], environ: Mapping[str, str] | None = None
) -> str:
    """Print the comma-separated list; append it to ``$GITHUB_OUTPUT`` if set."""
    environ = os.environ if environ is None else environ
    joined = ",".join(repos)
    print(joined or "(no private atlanhq git dependencies)")
    output = environ.get("GITHUB_OUTPUT", "")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"repositories={joined}\n")
    return joined


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install private atlanhq git dependencies without exposing the token."
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    mirror_parser = sub.add_parser(
        "mirror", help="mirror the private atlanhq deps (the only step with the token)"
    )
    mirror_parser.add_argument("--project-dir", default=".")
    run_parser = sub.add_parser(
        "run", help="run a command against the mirrors, with no token"
    )
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    repos_parser = sub.add_parser(
        "repos", help="list the private atlanhq repos the project names"
    )
    repos_parser.add_argument("--project-dir", default=".")
    args = parser.parse_args(argv)
    if args.mode == "repos":
        private = private_repos(args.project_dir)
        refused = refused_repos(private)
        if refused:
            _refuse(refused)
            return 1
        write_repos_output(private)
        return 0
    if args.mode == "mirror":
        return prepare_mirrors(project_dir=args.project_dir)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("run needs a command after --")
    return run(command)


if __name__ == "__main__":
    sys.exit(main())
