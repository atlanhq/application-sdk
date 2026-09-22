"""Round-trip guard for the private-atlanhq-dep opt-ins.

``conformance.yaml`` and ``release.yaml`` are always-overwrite shims, so a
per-repo value on either survives only if BOTH halves exist: bootstrap's
autodetection must read it back before re-rendering, and the C002 drift
checker must read it back before comparing. Before this existed, a bare
bootstrap run silently deleted:

* ``private-git-deps: true`` + ``secrets: inherit`` from ``conformance.yaml``
* ``private_git_auth: true`` from ``release.yaml``

from any repo pinning a private ``atlanhq`` package via ``ssh://``. The first
reds ``Conformance Gate`` — a REQUIRED check — on a cold ``uv`` cache. The
second is silent until a release simply never happens. Both were observed on
live connector repos.

These tests assert the loop end to end: write a file with the opt-in, run the
real bootstrap, read the file back.
"""

from __future__ import annotations

import pathlib

import pytest
from conformance.bootstrap.extract import (
    extract_conformance_private_git_deps,
    extract_release_private_git_auth,
)
from conformance.bootstrap.render import render
from conformance.cli import _cmd_bootstrap

_CONFORMANCE = ".github/workflows/conformance.yaml"
_RELEASE = ".github/workflows/release.yaml"


@pytest.fixture
def repo(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> pathlib.Path:
    """A minimal consumer repo, cwd'd into — bootstrap operates on `.`."""
    (tmp_path / "atlan.yaml").write_text("name: test-app\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _bootstrap() -> None:
    assert _cmd_bootstrap([]) == 0


def _seed(root: pathlib.Path, rel: str, **kwargs: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(pathlib.Path(rel).name, **kwargs), encoding="utf-8")


# --- extractors ---------------------------------------------------------


def test_extractors_read_only_a_literal_true() -> None:
    """``false`` restates the reusable default and must render no line."""
    assert (
        extract_conformance_private_git_deps("      private-git-deps: true") == "true"
    )
    assert extract_conformance_private_git_deps("      private-git-deps: false") == ""
    assert extract_conformance_private_git_deps("with:\n  event_name: x\n") == ""
    assert extract_release_private_git_auth("      private_git_auth: true") == "true"
    assert extract_release_private_git_auth("      private_git_auth: false") == ""
    assert extract_release_private_git_auth("jobs:\n  bump:\n") == ""


def test_conformance_optin_renders_both_halves() -> None:
    """The input is useless without ``secrets: inherit`` feeding it the PAT."""
    opted_in = render("conformance.yaml", conformance_private_git_deps="true")
    assert "private-git-deps: true" in opted_in
    assert "secrets: inherit" in opted_in

    default = render("conformance.yaml")
    assert "private-git-deps" not in default
    assert "secrets: inherit" not in default


# --- the loop that was broken -------------------------------------------


def test_bootstrap_preserves_conformance_private_git_deps(
    repo: pathlib.Path,
) -> None:
    """A bare re-run must not delete the opt-in it found on disk."""
    _seed(repo, _CONFORMANCE, conformance_private_git_deps="true")
    _bootstrap()
    after = (repo / _CONFORMANCE).read_text(encoding="utf-8")
    assert extract_conformance_private_git_deps(after) == "true"
    assert "secrets: inherit" in after


def test_bootstrap_preserves_release_private_git_auth(repo: pathlib.Path) -> None:
    """Same, on the quieter of the two failures."""
    _seed(repo, _RELEASE, release_private_git_auth="true")
    _bootstrap()
    after = (repo / _RELEASE).read_text(encoding="utf-8")
    assert extract_release_private_git_auth(after) == "true"


def test_bootstrap_adds_nothing_to_a_repo_without_the_optin(
    repo: pathlib.Path,
) -> None:
    """The contrast that makes the two above meaningful."""
    _bootstrap()
    conformance = (repo / _CONFORMANCE).read_text(encoding="utf-8")
    release = (repo / _RELEASE).read_text(encoding="utf-8")
    assert "private-git-deps" not in conformance
    assert "secrets: inherit" not in conformance
    assert "private_git_auth" not in release


def test_optin_is_stable_across_repeated_runs(repo: pathlib.Path) -> None:
    """Idempotent: the second run must not undo what the first preserved."""
    _seed(repo, _CONFORMANCE, conformance_private_git_deps="true")
    _seed(repo, _RELEASE, release_private_git_auth="true")
    _bootstrap()
    first = (repo / _CONFORMANCE).read_text(encoding="utf-8")
    _bootstrap()
    assert (repo / _CONFORMANCE).read_text(encoding="utf-8") == first
    assert (
        extract_release_private_git_auth((repo / _RELEASE).read_text(encoding="utf-8"))
        == "true"
    )
