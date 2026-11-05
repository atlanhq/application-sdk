"""Tests for .github/scripts/git_auth.py (FND-4740).

The rewrite is checked against git itself, and ``run`` against a real child
process, because what matters is what the child (uv, and the git it starts)
actually sees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import git_auth  # noqa: E402

#: A synthetic value; never a real token.
TOKEN = "ghs_SENTINEL_git_auth_token"
AUTHED = f"https://x-access-token:{TOKEN}@github.com/atlanhq/private-dep.git"

#: A child that prints ONLY the git-auth variables it was given, as JSON. Never
#: dump the whole env: a developer's shell can hold real credentials.
DUMP_ENV = [
    sys.executable,
    "-c",
    "import json, os; print(json.dumps({k: v for k, v in os.environ.items()"
    " if k == 'GIT_TOKEN' or k.startswith('GIT_CONFIG_')}))",
]

#: The only env the child runs with in these tests: synthetic, no real secrets.
_BASE_ENV = {"PATH": os.environ.get("PATH", "")}


def _git(args: list[str], home: Path, extra: dict[str, str]) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            **extra,
        },
    ).stdout


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/atlanhq/private-dep.git",
        # The fleet's private deps are all written in this form.
        "ssh://git@github.com/atlanhq/private-dep.git",
    ],
)
def test_rewrites_both_atlanhq_remote_forms_in_real_git(
    tmp_path: Path, remote: str
) -> None:
    got = _git(
        ["ls-remote", "--get-url", remote], tmp_path, git_auth.git_auth_env(TOKEN)
    )
    assert got.strip() == AUTHED
    # Nothing written: a later process without the env has no credential.
    assert _git(["ls-remote", "--get-url", remote], tmp_path, {}).strip() == remote
    assert not (tmp_path / ".gitconfig").exists()


def test_leaves_other_remotes_alone(tmp_path: Path) -> None:
    remote = "https://github.com/someone-else/dep.git"
    got = _git(
        ["ls-remote", "--get-url", remote], tmp_path, git_auth.git_auth_env(TOKEN)
    )
    assert got.strip() == remote


def test_keeps_config_entries_the_env_already_carries(tmp_path: Path) -> None:
    """A job that sets its own GIT_CONFIG_COUNT keeps it: ours are appended."""
    base = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "kept",
    }
    env = {**base, **git_auth.git_auth_env(TOKEN, base)}
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert _git(["config", "user.name"], tmp_path, env).strip() == "kept"
    remote = "ssh://git@github.com/atlanhq/private-dep.git"
    assert _git(["ls-remote", "--get-url", remote], tmp_path, env).strip() == AUTHED


def test_child_env_never_carries_the_token_variable() -> None:
    env = git_auth.child_env({"GIT_TOKEN": TOKEN, "PATH": "/bin"}, required=True)
    assert env is not None
    assert "GIT_TOKEN" not in env
    assert env["PATH"] == "/bin"
    assert env["GIT_CONFIG_COUNT"] == "2"


def test_no_token_and_not_required_runs_plain() -> None:
    """The wrapper is a no-op for a caller with no private deps."""
    env = git_auth.child_env({"GIT_TOKEN": "", "PATH": "/bin"}, required=False)
    assert env == {"PATH": "/bin"}


def test_no_token_when_required_refuses() -> None:
    assert git_auth.child_env({"PATH": "/bin"}, required=True) is None


def _run(
    capfd: pytest.CaptureFixture[str], **env: str
) -> tuple[int, dict[str, str] | None, str]:
    """Run the wrapper against a synthetic env; the child's output is read from
    the real fd (it is a separate process), and is only ever the filtered dump."""
    rc = git_auth.run(DUMP_ENV, {**_BASE_ENV, **env})
    out = capfd.readouterr().out
    lines = out.strip().splitlines()
    seen = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else None
    return rc, seen, out


def test_run_scopes_the_token_to_the_child(capfd: pytest.CaptureFixture[str]) -> None:
    rc, seen, _ = _run(capfd, GIT_TOKEN=TOKEN, PRIVATE_GIT_DEPS="true")
    assert rc == 0
    assert seen is not None
    assert "GIT_TOKEN" not in seen
    for key, value in git_auth.git_auth_env(TOKEN).items():
        assert seen[key] == value
    # The wrapper's own process env is untouched: the next step sees nothing.
    assert "GIT_CONFIG_COUNT" not in os.environ


def test_run_without_token_or_flag_passes_through(
    capfd: pytest.CaptureFixture[str],
) -> None:
    rc, seen, _ = _run(capfd)
    assert rc == 0
    assert seen == {}


def test_run_fails_closed_naming_the_missing_secrets(
    capfd: pytest.CaptureFixture[str],
) -> None:
    rc, seen, out = _run(capfd, GIT_TOKEN="", PRIVATE_GIT_DEPS="true")
    assert rc == 1
    assert seen is None  # the command never ran
    assert "::error title=No token for private git dependencies::" in out
    assert "FLEET_APP_ID" in out and "FLEET_APP_PRIVATE_KEY" in out


def test_run_returns_the_commands_exit_code() -> None:
    assert (
        git_auth.run(
            [sys.executable, "-c", "raise SystemExit(3)"], {"PATH": os.environ["PATH"]}
        )
        == 3
    )


def test_run_needs_a_command() -> None:
    with pytest.raises(SystemExit):
        git_auth.main(["run", "--"])
