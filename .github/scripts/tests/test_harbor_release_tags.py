"""Tests for .github/scripts/harbor_release_tags.py.

Covers the branching that used to be inlined in the *Compute tag prefix* and
*Compute image tags* steps of harbor-release.yaml. The ladder decides what a
tenant resolves `app-runtime-base:latest` (and `:3`, and `:3.1`) to, so the
cases that matter most are the ones that must NOT advance those aliases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import harbor_release_tags as mod  # noqa: E402

HARBOR = "registry.atlan.com/public/app-runtime-base"
GHCR = "ghcr.io/atlanhq/app-runtime-base"


def _suffixes(tags: list, repo: str) -> list:
    return [t.split(":", 1)[1] for t in tags if t.startswith(f"{repo}:")]


# ── The prefix ───────────────────────────────────────────────────────────────


def test_release_always_labels_itself_main() -> None:
    """Release tags are the version ladder; the prefix is only a label."""
    assert mod.resolve_prefix("release", "", "refs/tags/v3.1.0") == "main"


def test_release_ignores_an_explicit_prefix() -> None:
    """The `release` branch is checked first in the shell this replaces."""
    assert mod.resolve_prefix("release", "someprefix", "main") == "main"


def test_explicit_prefix_is_used_verbatim() -> None:
    assert (
        mod.resolve_prefix("workflow_dispatch", "refactor-v3", "main") == "refactor-v3"
    )


@pytest.mark.parametrize(
    "bad",
    ["has space", "slash/prefix", "semi;colon", "quote'", "dollar$", "at@sign"],
)
def test_an_unsafe_explicit_prefix_is_rejected_not_sanitised(bad: str) -> None:
    """A prefix reaches a registry reference. Silently rewriting an operator's
    input would publish a tag they did not ask for, so this fails instead."""
    with pytest.raises(mod.PrefixError):
        mod.resolve_prefix("workflow_dispatch", bad, "main")


def test_branch_name_is_sanitised_when_no_prefix_given() -> None:
    assert (
        mod.resolve_prefix("workflow_dispatch", "", "feat/my branch")
        == "feat-my-branch"
    )


def test_runs_of_illegal_characters_collapse_to_one_separator() -> None:
    """`tr -cs` squeezes repeats; a naive per-character replace would not."""
    assert mod.sanitize_branch("feat//my   branch") == "feat-my-branch"


def test_a_legal_branch_name_is_untouched() -> None:
    assert mod.sanitize_branch("release-3.1.0_rc") == "release-3.1.0_rc"


# ── The ladder ───────────────────────────────────────────────────────────────


def test_stable_release_publishes_the_full_alias_ladder() -> None:
    tags = mod.build_tags("release", "3.1.4", "main", "abc1234")
    assert _suffixes(tags, HARBOR) == [
        "latest",
        "3.1.4",
        "3.1",
        "3",
        "sha-abc1234",
    ]


@pytest.mark.parametrize("version", ["3.1.0-rc1", "3.1.0-alpha.1", "4.0.0-beta"])
def test_a_prerelease_never_advances_a_floating_alias(version: str) -> None:
    """`:latest`, `:MAJOR` and `:MAJOR.MINOR` are what tenants resolve. A
    pre-release moving any of them ships an unstable base fleet-wide."""
    suffixes = _suffixes(mod.build_tags("release", version, "main", "abc1234"), HARBOR)
    assert suffixes == [version, "sha-abc1234"]
    assert "latest" not in suffixes
    assert not any(s in {"3", "3.1", "4", "4.0"} for s in suffixes)


def test_workflow_dispatch_scopes_every_alias_to_the_prefix() -> None:
    """A dev build must not collide with the release ladder."""
    suffixes = _suffixes(
        mod.build_tags("workflow_dispatch", "3.1.4", "refactor-v3", "abc1234"), HARBOR
    )
    assert suffixes == ["refactor-v3-latest", "refactor-v3-3.1.4", "sha-abc1234"]
    assert "latest" not in suffixes


def test_both_registries_get_an_identical_ladder() -> None:
    """The two registries must be interchangeable for a given tag — that is the
    whole premise of the GHCR base redirect in build-and-publish-app.yaml."""
    tags = mod.build_tags("release", "3.1.4", "main", "abc1234")
    assert _suffixes(tags, HARBOR) == _suffixes(tags, GHCR)


def test_harbor_and_ghcr_are_the_only_targets() -> None:
    assert set(mod.REPOS) == {HARBOR, GHCR}


@pytest.mark.parametrize(
    "version, prerelease",
    [("3.1.4", False), ("3.1.0-rc1", True), ("0.0.1", False), ("1.0.0-x", True)],
)
def test_prerelease_detection(version: str, prerelease: bool) -> None:
    assert mod.is_prerelease(version) is prerelease


def test_the_sha_tag_is_always_present() -> None:
    """The only immutable reference in the ladder — the recovery path in
    build-security.md depends on it existing for every event."""
    for event, version in [
        ("release", "3.1.4"),
        ("release", "3.1.4-rc1"),
        ("workflow_dispatch", "3.1.4"),
    ]:
        suffixes = _suffixes(mod.build_tags(event, version, "p", "abc1234"), HARBOR)
        assert "sha-abc1234" in suffixes


# ── Version discovery ────────────────────────────────────────────────────────


def test_version_is_read_from_the_top_level_of_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "3.1.4"\n', encoding="utf-8")
    assert mod.read_version(str(pyproject)) == "3.1.4"


def test_an_indented_version_key_is_not_mistaken_for_the_project_version(
    tmp_path: Path,
) -> None:
    """The awk this replaces anchored at column 0. A nested table's `version`
    would otherwise win and tag the release with a dependency's number."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.other]\n  version = "9.9.9"\n\nversion = "3.1.4"\n', encoding="utf-8"
    )
    assert mod.read_version(str(pyproject)) == "3.1.4"


def test_a_pyproject_without_a_version_is_an_error(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        mod.read_version(str(pyproject))


# ── Output plumbing ──────────────────────────────────────────────────────────


def test_the_multiline_tag_list_is_heredoc_quoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `tags=<multi-line>` truncates at the first newline, so the merge
    job would create the manifest under only the first tag."""
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    mod._write_outputs({"tag_prefix": "main", "tags": "a\nb\nc"})

    body = out.read_text(encoding="utf-8")
    assert "tag_prefix=main" in body
    assert "tags<<EOF_" in body
    assert "\na\nb\nc\n" in body
