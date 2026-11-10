"""Tests for .github/scripts/git_auth.py (FND-4740, lens round 5).

What matters is what the command (uv, the git it starts, and every build
backend it runs) actually sees, so ``run`` is exercised against real git and a
real child process, with local repositories standing in for GitHub: the
mirror clones are pointed at them through ``clone_rewrite``, the same seam the
token rewrite uses in production.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import git_auth  # noqa: E402

#: A synthetic value; never a real token.
TOKEN = "ghs_SENTINEL_git_auth_token"
AUTHED = f"https://x-access-token:{TOKEN}@github.com/atlanhq/private-dep.git"
SSH_REMOTE = "ssh://git@github.com/atlanhq/private-dep"


def _git(args: list[str], home: Path, extra: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            **(extra or {}),
        },
    ).stdout


# ── the token rewrite, given only to the mirror clones ──────────────────────


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/atlanhq/private-dep.git",
        "ssh://git@github.com/atlanhq/private-dep.git",
    ],
)
def test_token_rewrite_covers_both_atlanhq_remote_forms_in_real_git(
    tmp_path: Path, remote: str
) -> None:
    got = _git(
        ["ls-remote", "--get-url", remote], tmp_path, git_auth.git_auth_env(TOKEN)
    )
    assert got.strip() == AUTHED
    assert _git(["ls-remote", "--get-url", remote], tmp_path).strip() == remote
    assert not (tmp_path / ".gitconfig").exists()


def test_token_rewrite_leaves_other_remotes_alone(tmp_path: Path) -> None:
    remote = "https://github.com/someone-else/dep.git"
    got = _git(
        ["ls-remote", "--get-url", remote], tmp_path, git_auth.git_auth_env(TOKEN)
    )
    assert got.strip() == remote


def test_rewrite_keeps_config_entries_the_env_already_carries(tmp_path: Path) -> None:
    base = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "kept",
    }
    env = {**base, **git_auth.git_auth_env(TOKEN, base)}
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert _git(["config", "user.name"], tmp_path, env).strip() == "kept"


# ── which repositories the project names ────────────────────────────────────


def test_referenced_repos_reads_every_git_source_form(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'dependencies = ["a @ git+https://github.com/atlanhq/pep508-dep.git@v1"]\n'
        "[tool.uv.sources]\n"
        'b = { git = "ssh://git@github.com/atlanhq/ssh-dep", tag = "v1" }\n'
        "# a comment naming https://github.com/atlanhq/not-a-dep is not a source\n"
    )
    (tmp_path / "uv.lock").write_text(
        'source = { git = "https://github.com/atlanhq/locked-dep?tag=v2#abc" }\n'
        'source = { git = "https://github.com/someone-else/other?tag=v1#abc" }\n'
    )
    sub = tmp_path / "server"
    sub.mkdir()
    (sub / "pyproject.toml").write_text(
        'c = { git = "https://github.com/atlanhq/sub-package-dep.git" }\n'
    )
    for skipped in (".venv", "node_modules"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "pyproject.toml").write_text(
            'd = { git = "https://github.com/atlanhq/vendored-dep" }\n'
        )
    assert git_auth.referenced_repos(tmp_path) == [
        "locked-dep",
        "pep508-dep",
        "ssh-dep",
        "sub-package-dep",
    ]


def test_referenced_repos_ignores_commented_out_sources(tmp_path: Path) -> None:
    """A commented-out source is not a dependency and must not widen the
    token's scope (9 fleet apps carry such a block for experimental-app-sdk)."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv.sources]\n"
        '# x = { git = "https://github.com/atlanhq/commented-dep.git" }\n'
        '    #  y = { git = "ssh://git@github.com/atlanhq/indented-commented" }\n'
        'z = { git = "https://github.com/atlanhq/live-dep" }\n'
    )
    assert git_auth.referenced_repos(tmp_path) == ["live-dep"]


def test_referenced_repos_reads_hosted_api_app_pins(tmp_path: Path) -> None:
    """common-app-server's image build clones every ``repo`` in
    hosted-api-apps.toml, and uv.lock only sees them as path dependencies."""
    (tmp_path / "uv.lock").write_text(
        'source = { git = "https://github.com/atlanhq/locked-dep?rev=abc#abc" }\n'
    )
    (tmp_path / "hosted-api-apps.toml").write_text(
        "# [apps.example]\n"
        '# repo = "atlanhq/commented-app"\n'
        "[sdk]\n"
        'repo = "atlanhq/application-sdk"\n'
        "[apps.one]\n"
        'repo = "atlanhq/hosted-app"\n'
        'tag = "v1.0.0"\n'
        "[apps.other]\n"
        'repo = "someone-else/other-app"\n'
    )
    # Only that file is read for `repo =` keys.
    (tmp_path / "pyproject.toml").write_text('repo = "atlanhq/not-a-dep"\n')
    assert git_auth.referenced_repos(tmp_path) == [
        "application-sdk",
        "hosted-app",
        "locked-dep",
    ]


def test_private_repos_drops_the_public_ones(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        'source = { git = "https://github.com/atlanhq/application-sdk?tag=v3#a" }\n'
        'source = { git = "ssh://git@github.com/atlanhq/private-dep?tag=v1#b" }\n'
    )
    public = {"application-sdk"}
    assert git_auth.private_repos(tmp_path, lambda n: n in public) == ["private-dep"]


def test_mirror_set_adds_a_public_repo_a_private_prefix_would_capture() -> None:
    """insteadOf matches by prefix: the rewrite for ``dep`` also catches
    ``dep-extra``, so that one is mirrored too."""
    assert git_auth.mirror_set(["dep"], ["dep", "dep-extra", "other"]) == [
        "dep",
        "dep-extra",
    ]


def test_is_public_asks_git_with_no_credential_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmds: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def fake(
        cmd: list[str], *, env: dict[str, str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        cmds.append(cmd)
        envs.append(env)
        return subprocess.CompletedProcess(cmd, 128, "", "")

    monkeypatch.setattr(git_auth.subprocess, "run", fake)
    assert git_auth.is_public("private-dep") is False
    (cmd,) = cmds
    (env,) = envs
    assert cmd[:3] == ["git", "-c", "credential.helper="]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(k.startswith("GIT_CONFIG_KEY") for k in env)


# ── the allowlist: a PR cannot widen the token's scope ──────────────────────


def test_the_allowlist_is_the_reviewed_set() -> None:
    """Widening it is an SDK change, and shows up here in review."""
    assert git_auth.ALLOWED_PRIVATE_DEPS == {
        "atlan-query-intelligence-app",
        "sqlglot-for-lorien",
        "server-sdk",
        "ai-application-sdk",
    }
    assert git_auth.AGGREGATOR_REPOS == {"atlanhq/common-app-server"}


def _names_unlisted(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    (app / "uv.lock").write_text(
        'source = { git = "ssh://git@github.com/atlanhq/atlan-query-intelligence-app?tag=v1#a" }\n'
        'source = { git = "https://github.com/atlanhq/some-other-private-repo?tag=v1#b" }\n'
    )
    return app


def test_repos_refuses_an_unlisted_private_repo_before_any_mint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _names_unlisted(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setattr(git_auth, "is_public", lambda name: False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_REPOSITORY", "atlanhq/atlan-some-app")
    assert git_auth.main(["repos", "--project-dir", str(app)]) == 1
    out = capsys.readouterr().out
    assert "::error title=Private git dependency not allowed::" in out
    assert "some-other-private-repo" in out
    # No scope written, so the mint step (gated on a non-empty list) is skipped.
    assert not output.exists()


def test_mirror_refuses_an_unlisted_private_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = _names_unlisted(tmp_path)
    rc = git_auth.prepare_mirrors(
        {
            "GIT_TOKEN": TOKEN,
            "PRIVATE_GIT_DEPS": "true",
            "GITHUB_REPOSITORY": "atlanhq/atlan-some-app",
        },
        project_dir=app,
        dest=tmp_path / "git-mirrors",
        public=lambda name: False,
        clone_rewrite={r: "file:///nonexistent/" for r in git_auth.ATLANHQ_REMOTES},
    )
    assert rc == 1
    assert "some-other-private-repo" in capsys.readouterr().out
    assert not (tmp_path / "git-mirrors").exists()


def test_an_aggregator_may_name_any_private_repo() -> None:
    private = ["atlan-query-intelligence-app", "atlan-redshift-app"]
    assert git_auth.refused_repos(
        private, {"GITHUB_REPOSITORY": "atlanhq/atlan-some-app"}
    ) == ["atlan-redshift-app"]
    assert (
        git_auth.refused_repos(
            private, {"GITHUB_REPOSITORY": "atlanhq/common-app-server"}
        )
        == []
    )
    # Never by name lookalike: the full owner/repo must match.
    assert git_auth.refused_repos(
        private, {"GITHUB_REPOSITORY": "someone/common-app-server"}
    ) == ["atlan-redshift-app"]


# ── mirror, then run: the token never reaches uv or its ancestors ─────────


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    """A local stand-in for github.com/atlanhq, holding ``private-dep``.

    git runs with an empty HOME and no system config, so no developer config
    (signing, hooks, credential helpers) applies to these throwaway repos.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    src = tmp_path / "src"
    src.mkdir()
    (src / "README").write_text("dep\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", "-C", str(src), *args], check=True, env=env)
    root = tmp_path / "remotes"
    root.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(root / "private-dep")],
        check=True,
        env=env,
    )
    return root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    (app / "pyproject.toml").write_text(
        f'[tool.uv.sources]\nprivate-dep = {{ git = "{SSH_REMOTE}", branch = "main" }}\n'
    )
    return app


#: A child standing in for uv: it fetches the dependency through whatever
#: rewrite it was given (as uv's git would) and reports, without ever printing
#: a value, whether the token reached it anywhere.
_CHILD = f"""
import json, os, subprocess, sys
token = {TOKEN!r}
url = subprocess.run(["git", "ls-remote", "--get-url", {SSH_REMOTE!r}],
                     capture_output=True, text=True).stdout.strip()
heads = subprocess.run(["git", "ls-remote", {SSH_REMOTE!r}],
                       capture_output=True, text=True).stdout
print(json.dumps({{
    "token_in_env": any(token in v for v in os.environ.values()),
    "git_token_var": "GIT_TOKEN" in os.environ,
    "url": url,
    "fetched_main": "refs/heads/main" in heads,
}}))
"""


_MIRRORS = "git-mirrors"


def _mirror(
    capfd: pytest.CaptureFixture[str],
    project: Path,
    remotes: Path,
    tmp_path: Path,
    **env: str,
) -> tuple[int, str]:
    """The mirror step: the only one given the token. The synthetic
    ``private-dep`` stands in for a listed repository."""
    allowed = git_auth.ALLOWED_PRIVATE_DEPS | {"private-dep"}
    with mock.patch.object(git_auth, "ALLOWED_PRIVATE_DEPS", allowed):
        rc = git_auth.prepare_mirrors(
            {"PATH": os.environ.get("PATH", ""), **env},
            project_dir=project,
            dest=tmp_path / _MIRRORS,
            public=lambda name: False,
            clone_rewrite={
                remote: f"file://{remotes}/" for remote in git_auth.ATLANHQ_REMOTES
            },
        )
    return rc, capfd.readouterr().out


def _run_child(
    capfd: pytest.CaptureFixture[str], tmp_path: Path, **env: str
) -> tuple[int, dict[str, object] | None, str]:
    """The run step: a later step, with no token in its env."""
    environ = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        **env,
    }
    rc = git_auth.run([sys.executable, "-c", _CHILD], environ, dest=tmp_path / _MIRRORS)
    out = capfd.readouterr().out
    lines = [line for line in out.strip().splitlines() if line.startswith("{")]
    return rc, (json.loads(lines[-1]) if lines else None), out


def test_the_run_step_fetches_from_the_mirror_with_no_token_anywhere(
    capfd: pytest.CaptureFixture[str], project: Path, remotes: Path, tmp_path: Path
) -> None:
    rc, _ = _mirror(
        capfd, project, remotes, tmp_path, GIT_TOKEN=TOKEN, PRIVATE_GIT_DEPS="true"
    )
    assert rc == 0
    rc, seen, _ = _run_child(capfd, tmp_path)
    assert rc == 0
    assert seen is not None
    assert seen["token_in_env"] is False
    assert seen["git_token_var"] is False
    assert seen["url"] == f"file://{tmp_path / _MIRRORS / 'private-dep'}"
    assert seen["fetched_main"] is True
    # Nothing the run step can reach holds the token: not the mirror.
    for path in (tmp_path / _MIRRORS).rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    assert "GIT_CONFIG_COUNT" not in os.environ


def test_the_run_step_refuses_a_token_in_its_env(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A token in the step that runs uv is readable by every build backend
    through /proc/<ancestor>/environ, so the run step must not have one."""
    rc, seen, out = _run_child(capfd, tmp_path, GIT_TOKEN=TOKEN)
    assert rc == 1
    assert seen is None  # the command never ran
    assert "::error title=Token in the step that runs uv::" in out


def test_the_mirror_does_not_store_the_rewritten_url(
    tmp_path: Path, remotes: Path
) -> None:
    """git keeps the URL as written, not the insteadOf target, so a token in
    the clone's rewrite is not left in the mirror's config."""
    dest = tmp_path / "mirrors"
    dest.mkdir()
    rewrite = {remote: f"file://{remotes}/" for remote in git_auth.ATLANHQ_REMOTES}
    assert git_auth.mirror(["private-dep"], dest, rewrite) is None
    config = (dest / "private-dep" / "config").read_text()
    assert "url = https://github.com/atlanhq/private-dep" in config
    assert str(remotes) not in config
    assert (dest / "private-dep.git").resolve() == (dest / "private-dep").resolve()
    assert git_auth.mirrored_names(dest) == ["private-dep"]


def test_the_mirror_step_fails_closed_when_a_clone_fails(
    capfd: pytest.CaptureFixture[str], project: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "no-remotes"
    empty.mkdir()
    rc, out = _mirror(
        capfd, project, empty, tmp_path, GIT_TOKEN=TOKEN, PRIVATE_GIT_DEPS="true"
    )
    assert rc == 1
    assert "atlanhq/private-dep could not be cloned" in out
    assert not (tmp_path / _MIRRORS).exists()


def test_the_mirror_step_fails_closed_naming_the_missing_secrets(
    capfd: pytest.CaptureFixture[str], project: Path, remotes: Path, tmp_path: Path
) -> None:
    rc, out = _mirror(
        capfd, project, remotes, tmp_path, GIT_TOKEN="", PRIVATE_GIT_DEPS="true"
    )
    assert rc == 1
    assert "::error title=No token for private git dependencies::" in out
    assert "FLEET_APP_ID" in out and "FLEET_APP_PRIVATE_KEY" in out


def test_the_mirror_step_replaces_a_stale_mirror(
    capfd: pytest.CaptureFixture[str], project: Path, remotes: Path, tmp_path: Path
) -> None:
    stale = tmp_path / _MIRRORS / "gone-dep"
    stale.mkdir(parents=True)
    rc, _ = _mirror(
        capfd, project, remotes, tmp_path, GIT_TOKEN=TOKEN, PRIVATE_GIT_DEPS="true"
    )
    assert rc == 0
    assert git_auth.mirrored_names(tmp_path / _MIRRORS) == ["private-dep"]


def test_no_private_repo_named_needs_no_token_and_runs_plain(
    capfd: pytest.CaptureFixture[str], remotes: Path, tmp_path: Path
) -> None:
    """The scoped mint is skipped for such a project, so no token arrives."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text("[project]\nname = 'plain'\n")
    rc, _ = _mirror(
        capfd, plain, remotes, tmp_path, GIT_TOKEN="", PRIVATE_GIT_DEPS="true"
    )
    assert rc == 0
    assert not (tmp_path / _MIRRORS).exists()
    rc, seen, _ = _run_child(capfd, tmp_path)
    assert rc == 0
    assert seen is not None
    assert seen["url"] == SSH_REMOTE


def test_the_mirror_step_without_token_or_flag_does_not_look(tmp_path: Path) -> None:
    calls: list[str] = []

    def public(name: str) -> bool:
        calls.append(name)
        return False

    rc = git_auth.prepare_mirrors(
        {"PATH": os.environ.get("PATH", "")}, dest=tmp_path / _MIRRORS, public=public
    )
    assert rc == 0
    assert calls == []


def test_run_returns_the_commands_exit_code(tmp_path: Path) -> None:
    assert (
        git_auth.run(
            [sys.executable, "-c", "raise SystemExit(3)"],
            {"PATH": os.environ["PATH"]},
            dest=tmp_path / _MIRRORS,
        )
        == 3
    )


def test_run_needs_a_command() -> None:
    with pytest.raises(SystemExit):
        git_auth.main(["run", "--"])


def test_mirror_dest_is_under_runner_temp(tmp_path: Path) -> None:
    assert git_auth.mirror_dest({"RUNNER_TEMP": str(tmp_path)}) == tmp_path / _MIRRORS


# ── repos: the list a scoped mint uses ──────────────────────────────────────


def test_repos_writes_the_scope_to_github_output(tmp_path: Path) -> None:
    output = tmp_path / "out"
    joined = git_auth.write_repos_output(
        ["ai-dep", "private-dep"], {"GITHUB_OUTPUT": str(output)}
    )
    assert joined == "ai-dep,private-dep"
    assert output.read_text() == "repositories=ai-dep,private-dep\n"


def test_repos_writes_an_empty_scope_so_the_mint_is_skipped(tmp_path: Path) -> None:
    output = tmp_path / "out"
    git_auth.write_repos_output([], {"GITHUB_OUTPUT": str(output)})
    assert output.read_text() == "repositories=\n"
