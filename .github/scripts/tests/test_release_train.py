"""Tests for .github/scripts/release_train.py."""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import release_train
from _frozen_clock import freeze_clock
from release_train import CONFORMANCE, CONTRACT_TOOLKIT, TRAINS, Commit, Train

REPO_ROOT = Path(__file__).resolve().parents[3]


def _c(subject: str, body: str = "", sha: str = "abc1234") -> Commit:
    return Commit(sha, subject, body)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class TestTrains:
    @pytest.mark.parametrize("train", TRAINS.values(), ids=list(TRAINS))
    def test_the_declared_version_is_readable(self, train: Train) -> None:
        version = release_train.read_version(str(REPO_ROOT / train.version_file))
        assert train.is_release_tag(train.release_tag(version))

    def test_each_train_has_its_own_tag_prefix_and_changelog(self) -> None:
        trains = list(TRAINS.values())
        assert len({t.tag_prefix for t in trains}) == len(trains)
        assert len({t.changelog for t in trains}) == len(trains)


class TestIsReleaseTag:
    @pytest.mark.parametrize(
        "tag", ["contract-toolkit-v0.11.0", "contract-toolkit-v1.20.300"]
    )
    def test_release_shaped_tags(self, tag: str) -> None:
        assert CONTRACT_TOOLKIT.is_release_tag(tag)

    @pytest.mark.parametrize(
        "tag",
        [
            # A personal pre-release pushed by hand.
            "contract-toolkit-v0.11.0-someone.0",
            "contract-toolkit-v0.11",
            "contract-toolkit-v01.2.3",
            "contract-toolkit-0.11.0",
            "conformance-v0.11.0",
            "v0.11.0",
        ],
    )
    def test_everything_else_is_not_a_release(self, tag: str) -> None:
        assert not CONTRACT_TOOLKIT.is_release_tag(tag)


# ---------------------------------------------------------------------------
# compute_bump
# ---------------------------------------------------------------------------


class TestComputeBump:
    def test_breaking_via_subject_exclamation(self) -> None:
        assert release_train.compute_bump([_c("feat!: drop Python 3.10")]) == "major"

    def test_breaking_via_scoped_exclamation(self) -> None:
        assert (
            release_train.compute_bump([_c("feat(ci)!: remove legacy hook")]) == "major"
        )

    def test_breaking_via_body_breaking_change(self) -> None:
        commit = _c("feat: something", "BREAKING CHANGE: old API removed")
        assert release_train.compute_bump([commit]) == "major"

    @pytest.mark.parametrize("trailer", ["BREAKING_CHANGE:", "BREAKING-CHANGE:"])
    def test_only_the_spelling_release_py_prices_is_breaking(
        self, trailer: str
    ) -> None:
        """One definition of breaking for every package (FND-4672).

        The per-package scripts used to accept these two spellings while
        ``release.py`` (the SDK) did not, so the same commit cut a major in one
        package and a minor in another. They now share ``conventional_breaking``.
        """
        commit = _c("feat: something", f"{trailer} removed")
        assert release_train.compute_bump([commit]) == "minor"

    def test_feat_gives_minor(self) -> None:
        assert release_train.compute_bump([_c("feat: add new rule")]) == "minor"

    def test_feat_scoped_gives_minor(self) -> None:
        assert (
            release_train.compute_bump([_c("feat(conformance): add E019")]) == "minor"
        )

    def test_fix_gives_patch(self) -> None:
        assert release_train.compute_bump([_c("fix: correct regex anchor")]) == "patch"

    def test_chore_gives_patch(self) -> None:
        assert release_train.compute_bump([_c("chore: update deps")]) == "patch"

    def test_no_commits_gives_patch(self) -> None:
        assert release_train.compute_bump([]) == "patch"

    def test_any_feat_gives_minor(self) -> None:
        commits = [_c("chore: update lock"), _c("feat: new check"), _c("fix: typo")]
        assert release_train.compute_bump(commits) == "minor"

    def test_breaking_takes_precedence_over_feat(self) -> None:
        commits = [_c("feat!: overhaul"), _c("feat: add thing")]
        assert release_train.compute_bump(commits) == "major"


# ---------------------------------------------------------------------------
# bump_version
# ---------------------------------------------------------------------------


class TestBumpVersion:
    def test_major_bump_resets_minor_and_patch(self) -> None:
        assert release_train.bump_version("1.2.3", "major") == "2.0.0"

    def test_minor_bump_resets_patch(self) -> None:
        assert release_train.bump_version("1.2.3", "minor") == "1.3.0"

    def test_patch_bump_increments_patch(self) -> None:
        assert release_train.bump_version("1.2.3", "patch") == "1.2.4"

    def test_from_zero(self) -> None:
        assert release_train.bump_version("0.0.0", "minor") == "0.1.0"

    def test_large_numbers(self) -> None:
        assert release_train.bump_version("10.20.30", "patch") == "10.20.31"

    # Semver §4: a breaking change on a 0.x package bumps the MINOR. Promoting
    # 0.x to 1.0.0 declares the API stable and stays a human decision.
    def test_breaking_on_zero_major_bumps_minor(self) -> None:
        assert release_train.bump_version("0.1.0", "major") == "0.2.0"

    def test_breaking_on_zero_major_does_not_reach_one(self) -> None:
        assert release_train.bump_version("0.29.0", "major") == "0.30.0"

    def test_breaking_on_zero_major_resets_patch(self) -> None:
        assert release_train.bump_version("0.25.2", "major") == "0.26.0"

    def test_breaking_past_one_still_bumps_major(self) -> None:
        assert release_train.bump_version("1.0.0", "major") == "2.0.0"


# ---------------------------------------------------------------------------
# categorize
# ---------------------------------------------------------------------------

_REPO = "testorg/testrepo"


def _link(sha: str) -> str:
    return f"https://github.com/{_REPO}/commit/{sha}"


class TestCategorize:
    def test_feat_lands_in_features(self) -> None:
        cats = release_train.categorize([_c("feat: add rule E019")], _REPO)
        assert cats["features"] == [(_link("abc1234"), "add rule E019")]

    def test_feat_scope_stripped(self) -> None:
        cats = release_train.categorize([_c("feat(conformance): new check")], _REPO)
        assert cats["features"][0][1] == "new check"

    def test_fix_lands_in_fixes(self) -> None:
        cats = release_train.categorize([_c("fix: correct anchor")], _REPO)
        assert cats["fixes"] == [(_link("abc1234"), "correct anchor")]

    def test_fix_scope_stripped(self) -> None:
        cats = release_train.categorize([_c("fix(ci): pin digest")], _REPO)
        assert cats["fixes"][0][1] == "pin digest"

    def test_chore_lands_in_other(self) -> None:
        cats = release_train.categorize([_c("chore: bump uv")], _REPO)
        assert len(cats["other"]) == 1

    def test_breaking_subject_lands_in_breaking(self) -> None:
        cats = release_train.categorize([_c("feat!: remove old API")], _REPO)
        assert cats["breaking"] == [(_link("abc1234"), "remove old API")]

    def test_breaking_revert_keeps_its_whole_subject(self) -> None:
        """Only a conventional prefix is stripped: a revert of a breaking change
        must still read as a revert, without a stray quote."""
        subject = 'Revert "feat(api)!: drop v1"'
        cats = release_train.categorize([_c(subject)], _REPO)
        assert cats["breaking"] == [(_link("abc1234"), subject)]

    def test_breaking_body_lands_in_breaking(self) -> None:
        commit = _c("feat: overhaul", "BREAKING CHANGE: old interface removed")
        cats = release_train.categorize([commit], _REPO)
        assert len(cats["breaking"]) == 1

    def test_mixed_commits_correctly_bucketed(self) -> None:
        commits = [
            _c("feat: add thing", sha="aaa1111"),
            _c("fix: repair thing", sha="bbb2222"),
            _c("chore: housekeeping", sha="ccc3333"),
            _c("feat!: breaking change", sha="ddd4444"),
        ]
        cats = release_train.categorize(commits, _REPO)
        assert [len(cats[k]) for k in ("features", "fixes", "other", "breaking")] == [
            1,
            1,
            1,
            1,
        ]

    def test_ci_is_dropped(self) -> None:
        commits = [_c("ci: tweak workflow"), _c("ci(x): pin", sha="def5678")]
        cats = release_train.categorize(commits, _REPO)
        assert all(len(v) == 0 for v in cats.values())

    def test_breaking_ci_is_kept(self) -> None:
        """A major bump must always have notes explaining it."""
        cats = release_train.categorize([_c("ci!: drop runner")], _REPO)
        assert len(cats["breaking"]) == 1

    def test_perf_and_refactor_land_in_other(self) -> None:
        commits = [_c("perf: faster"), _c("refactor: split", sha="def5678")]
        cats = release_train.categorize(commits, _REPO)
        assert len(cats["other"]) == 2

    def test_every_commit_compute_bump_prices_as_breaking_is_listed_as_breaking(
        self,
    ) -> None:
        """The bump and the notes read one predicate, so they cannot disagree."""
        commits = [
            _c("feat!: a"),
            _c("fix: b", "BREAKING CHANGE: c"),
            _c("chore: d", "breaking change: e"),
            _c("feat: f", "BREAKING-CHANGE: g"),
        ]
        cats = release_train.categorize(commits, _REPO)
        priced = [c for c in commits if release_train.compute_bump([c]) == "major"]
        assert len(cats["breaking"]) == len(priced) == 3

    def test_empty_commits_gives_empty_cats(self) -> None:
        cats = release_train.categorize([], _REPO)
        assert all(len(v) == 0 for v in cats.values())


# ---------------------------------------------------------------------------
# format_block
# ---------------------------------------------------------------------------


def _empty_cats() -> dict[str, list[tuple[str, str]]]:
    return {"breaking": [], "features": [], "fixes": [], "other": []}


class TestFormatBlock:
    def test_contains_version_header(self) -> None:
        assert "## [1.2.3]" in release_train.format_block("1.2.3", _empty_cats())

    def test_contains_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Frozen, so the module's "today" and the expected one are one reading
        # of the clock rather than two that can straddle midnight.
        at = freeze_clock(
            monkeypatch,
            release_train,
            datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        block = release_train.format_block("1.2.3", _empty_cats())
        assert f"## [1.2.3] - {at.date().isoformat()}" in block

    def test_sections_render_in_order(self) -> None:
        cats = _empty_cats()
        cats["features"] = [("https://github.com/o/r/commit/abc1234", "cool feature")]
        cats["fixes"] = [("https://github.com/o/r/commit/def5678", "fixed bug")]
        cats["breaking"] = [("https://github.com/o/r/commit/ghi9012", "dropped API")]
        cats["other"] = [("https://github.com/o/r/commit/jkl3456", "chore: update")]
        block = release_train.format_block("2.0.0", cats)
        headings = [
            "### Breaking changes",
            "### Features",
            "### Bug fixes",
            "### Other changes",
        ]
        positions = [block.index(h) for h in headings]
        assert positions == sorted(positions)
        assert "- cool feature ([abc1234](https://github.com/o/r/commit/abc1234))" in (
            block
        )

    def test_empty_section_not_present(self) -> None:
        block = release_train.format_block("1.0.0", _empty_cats())
        assert "###" not in block


# ---------------------------------------------------------------------------
# prepend_changelog
# ---------------------------------------------------------------------------


class TestPrependChangelog:
    def test_creates_a_missing_file(self, tmp_path: Path) -> None:
        cl = tmp_path / "CHANGELOG.md"
        release_train.prepend_changelog(str(cl), "## [0.1.0] - 2026-01-01\n")
        assert cl.read_text().startswith("# Changelog\n\n## [0.1.0]")

    def test_prepends_before_existing_version(self, tmp_path: Path) -> None:
        cl = tmp_path / "CHANGELOG.md"
        cl.write_text("# Changelog\n\n## [0.1.0] - 2026-01-01\n\n- old entry\n")
        release_train.prepend_changelog(str(cl), "## [0.2.0] - 2026-06-01\n")
        content = cl.read_text()
        assert content.index("0.2.0") < content.index("0.1.0")
        assert content.count("## [0.1.0]") == 1
        assert content.startswith("# Changelog\n")

    def test_prepends_to_header_only_file(self, tmp_path: Path) -> None:
        cl = tmp_path / "CHANGELOG.md"
        cl.write_text("# Changelog\n")
        release_train.prepend_changelog(str(cl), "## [1.0.0] - 2026-01-01\n")
        assert cl.read_text() == "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n"


# ---------------------------------------------------------------------------
# read_version / write_version
# ---------------------------------------------------------------------------


class TestVersionFile:
    def test_pyproject(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\nversion = "1.0.0"\n')
        assert release_train.read_version(str(p)) == "1.0.0"
        release_train.write_version(str(p), "1.0.0", "1.1.0")
        assert 'version = "1.1.0"' in p.read_text()

    def test_pklproject_reads_the_package_version_only(self, tmp_path: Path) -> None:
        p = tmp_path / "PklProject"
        p.write_text(
            "package {\n"
            '  name = "app-contract-toolkit"\n'
            '  version = "0.3.0"\n'
            '  packageZipUrl = "https://x/\\(name)@\\(version).zip"\n'
            "}\n"
        )
        assert release_train.read_version(str(p)) == "0.3.0"
        release_train.write_version(str(p), "0.3.0", "0.4.0")
        assert '  version = "0.4.0"' in p.read_text()
        assert "@\\(version).zip" in p.read_text()

    def test_replaces_only_the_first_occurrence(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('version = "1.0.0"\n[tool.x]\nversion = "1.0.0"\n')
        release_train.write_version(str(p), "1.0.0", "2.0.0")
        assert p.read_text() == 'version = "2.0.0"\n[tool.x]\nversion = "1.0.0"\n'

    def test_exits_when_version_not_found(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('version = "9.9.9"\n')
        with pytest.raises(SystemExit):
            release_train.write_version(str(p), "1.0.0", "1.1.0")


# ---------------------------------------------------------------------------
# Against a real git repo
#
# The lock-file exclusion depends on git pathspec magic (`:(exclude,glob)`) and
# the tag handling on real tags, so these run real git — a stubbed git would
# only assert that we pass the strings we wrote.
# ---------------------------------------------------------------------------

BASE_TAG = "conformance-v0.1.0"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, stderr=subprocess.DEVNULL, text=True
    )


def _commit(repo: Path, subject: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", subject)


def _init(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A temp repo with commits after conformance-v0.1.0 covering every case.

    Included (real package content):
      - feat(conformance): add O007 rule       — rule source
      - chore(deps): bump ruff                 — package pyproject.toml
      - fix(conformance): tighten O007         — rule source *and* uv.lock (mixed)

    Excluded (noise):
      - chore(deps): lock file maintenance     — packages/conformance/uv.lock only
      - chore(deps): root lock                 — outside packages/conformance
    """
    _init(tmp_path)
    _commit(
        tmp_path,
        "chore: initial commit",
        {
            "README.md": "init\n",
            "packages/conformance/pyproject.toml": '[project]\nversion = "0.1.0"\n',
        },
    )
    _git(tmp_path, "tag", BASE_TAG)
    _commit(
        tmp_path,
        "feat(conformance): add O007 rule",
        {"packages/conformance/conformance/rules/o007.py": "rule\n"},
    )
    _commit(
        tmp_path,
        "chore(deps): lock file maintenance",
        {"packages/conformance/uv.lock": "lock-1\n"},
    )
    _commit(tmp_path, "chore(deps): root lock", {"uv.lock": "root-lock\n"})
    _commit(
        tmp_path,
        "chore(deps): bump ruff",
        {"packages/conformance/pyproject.toml": '[project]\nversion = "0.1.0"\n#\n'},
    )
    _commit(
        tmp_path,
        "fix(conformance): tighten O007",
        {
            "packages/conformance/conformance/rules/o007.py": "rule-2\n",
            "packages/conformance/uv.lock": "lock-2\n",
        },
    )
    return tmp_path


def _subjects(base: str) -> list[str]:
    return [c.subject for c in release_train.get_commits(CONFORMANCE, base)]


class TestPathspecExclusions:
    def test_lock_only_commits_are_excluded_and_mixed_commits_kept(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_repo)
        assert _subjects(BASE_TAG) == [
            "fix(conformance): tighten O007",
            "chore(deps): bump ruff",
            "feat(conformance): add O007 rule",
        ]

    def test_sdk_ledger_only_commit_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init(tmp_path)
        _commit(tmp_path, "chore: initial commit", {"README.md": "init\n"})
        _git(tmp_path, "tag", BASE_TAG)
        _commit(
            tmp_path,
            "fix(conformance): sunset a field",
            {"contract_schema.lock.json": "{}\n"},
        )
        _commit(
            tmp_path, "chore: nested ledger", {"x/contract_schema.lock.json": "{}\n"}
        )
        monkeypatch.chdir(tmp_path)
        assert _subjects(BASE_TAG) == ["fix(conformance): sunset a field"]

    def test_lock_only_history_yields_no_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init(tmp_path)
        _commit(tmp_path, "chore: initial commit", {"README.md": "init\n"})
        _git(tmp_path, "tag", BASE_TAG)
        _commit(
            tmp_path,
            "chore(deps): lock file maintenance",
            {"packages/conformance/uv.lock": "lock\n"},
        )
        monkeypatch.chdir(tmp_path)
        assert _subjects(BASE_TAG) == []


# ---------------------------------------------------------------------------
# bump — the release workflow's entry point
# ---------------------------------------------------------------------------


@pytest.fixture
def outputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        release_train, "set_output", lambda k, v: captured.update({k: v})
    )
    return captured


@pytest.fixture
def guards_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bump() hermetic: both release guards would otherwise `git fetch`
    origin. The guards are covered by test_release_guard.py; their wiring into
    bump() by TestGuardWiring below."""
    monkeypatch.setattr(
        release_train.release_guard,
        "already_released",
        lambda *_a, **_k: (False, None),
    )
    monkeypatch.setattr(
        release_train.release_guard,
        "landed_on_branch",
        lambda *_a, **_k: (True, "stubbed"),
    )


def _train(repo: Path) -> Train:
    return dataclasses.replace(
        CONFORMANCE, release_notes_file=str(repo / "release-notes.md")
    )


@pytest.mark.usefixtures("guards_pass")
class TestBump:
    def test_bumps_version_and_prepends_changelog(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]
    ) -> None:
        monkeypatch.chdir(git_repo)
        train = _train(git_repo)
        release_train.bump(train, _REPO)

        # feat: from 0.1.0 → minor → 0.2.0
        assert outputs == {
            "skip": "false",
            "old": "0.1.0",
            "new": "0.2.0",
            "tag": "conformance-v0.2.0",
        }
        assert release_train.read_version(train.version_file) == "0.2.0"
        changelog = (git_repo / train.changelog).read_text()
        assert "## [0.2.0]" in changelog
        assert "- add O007 rule" in changelog
        assert "lock file maintenance" not in changelog
        notes = Path(train.release_notes_file).read_text()
        assert notes.startswith("## [0.2.0]")

    def test_no_commits_sets_skip_true(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]
    ) -> None:
        monkeypatch.chdir(git_repo)
        _git(git_repo, "tag", "-d", BASE_TAG)
        _git(git_repo, "tag", BASE_TAG)  # now at HEAD
        release_train.bump(_train(git_repo), _REPO)
        assert outputs == {"skip": "true"}

    def test_first_release_walks_from_the_root_commit(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        outputs: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_repo)
        _git(git_repo, "tag", "-d", BASE_TAG)
        release_train.bump(_train(git_repo), _REPO)
        assert "first release" in capsys.readouterr().out
        assert outputs["new"] == "0.2.0"

    def test_personal_tags_do_not_count_as_a_previous_release(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]
    ) -> None:
        """A hand-pushed pre-release tag is not a release of the train, so with
        no release tag at all this is still a first release, not an error."""
        monkeypatch.chdir(git_repo)
        _git(git_repo, "tag", "-d", BASE_TAG)
        _git(git_repo, "tag", "conformance-v0.0.9-someone.1", "HEAD~2")
        release_train.bump(_train(git_repo), _REPO)
        assert outputs["new"] == "0.2.0"

    def test_error_when_other_release_tags_exist_but_current_missing(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_repo)
        _git(git_repo, "tag", "-d", BASE_TAG)
        _git(git_repo, "tag", "conformance-v0.0.9", "HEAD~2")
        with pytest.raises(SystemExit, match="other conformance release tags exist"):
            release_train.bump(_train(git_repo), _REPO)

    def test_contract_toolkit_train(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]
    ) -> None:
        _init(tmp_path)
        pkl = 'amends "pkl:Project"\npackage {\n  version = "0.39.1"\n}\n'
        _commit(tmp_path, "chore: init", {"contract-toolkit/src/PklProject": pkl})
        _git(tmp_path, "tag", "contract-toolkit-v0.39.1")
        _commit(
            tmp_path,
            "fix(contract-toolkit): a widget",
            {"contract-toolkit/src/Widgets.pkl": "x\n"},
        )
        _commit(tmp_path, "feat: not the toolkit", {"elsewhere.py": "x\n"})
        monkeypatch.chdir(tmp_path)
        train = dataclasses.replace(
            CONTRACT_TOOLKIT, release_notes_file=str(tmp_path / "notes.md")
        )
        release_train.bump(train, _REPO)
        assert outputs["tag"] == "contract-toolkit-v0.39.2"
        assert '  version = "0.39.2"' in (tmp_path / train.version_file).read_text()
        changelog = (tmp_path / train.changelog).read_text()
        assert "### Bug fixes\n\n- a widget" in changelog
        assert "not the toolkit" not in changelog


class TestGuardWiring:
    def _snapshot(self, repo: Path) -> list[str]:
        return [
            (repo / CONFORMANCE.version_file).read_text(),
            str((repo / CONFORMANCE.changelog).exists()),
        ]

    def test_already_released_skips_and_leaves_every_file_untouched(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        outputs: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """application-sdk#3570: a stale checkout recomputes the version that
        was just published. The guard must run before any file is written."""
        monkeypatch.chdir(git_repo)
        monkeypatch.delenv(release_train.release_guard.MERGE_COMMIT_ENV, raising=False)
        seen: dict[str, str] = {}

        def fake_guard(path, new_version, **_kw):
            seen.update(path=path, new_version=new_version)
            return True, "0.2.0"

        monkeypatch.setattr(release_train.release_guard, "already_released", fake_guard)
        before = self._snapshot(git_repo)
        train = _train(git_repo)
        release_train.bump(train, _REPO)

        assert outputs == {"skip": "true"}
        assert seen == {"path": CONFORMANCE.version_file, "new_version": "0.2.0"}
        assert self._snapshot(git_repo) == before
        assert not Path(train.release_notes_file).exists()
        assert "already been published" in capsys.readouterr().out

    def test_stacked_pr_merge_into_a_feature_branch_skips(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        outputs: dict,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """application-sdk#3794: the merged commit is not on origin/main, so
        nothing is bumped and the version guard is never consulted."""
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv(
            release_train.release_guard.MERGE_COMMIT_ENV, "feedfacecafe0001"
        )
        seen: dict[str, str] = {}

        def fake_landed(sha, **_kw):
            seen["sha"] = sha
            return False, "not reachable"

        monkeypatch.setattr(
            release_train.release_guard, "landed_on_branch", fake_landed
        )
        monkeypatch.setattr(
            release_train.release_guard,
            "already_released",
            lambda *_a, **_k: pytest.fail("already_released consulted after skip"),
        )
        before = self._snapshot(git_repo)
        release_train.bump(_train(git_repo), _REPO)

        assert outputs == {"skip": "true"}
        assert seen["sha"] == "feedfacecafe0001"
        assert self._snapshot(git_repo) == before
        assert "did not land on origin/main" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# publish-meta — the publish workflow's entry point
# ---------------------------------------------------------------------------


class TestPublishMeta:
    def test_outputs_version_tag_and_commit(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]
    ) -> None:
        monkeypatch.chdir(git_repo)
        release_train.main(["publish-meta", "conformance"])
        assert outputs == {
            "version": "0.1.0",
            "tag": "conformance-v0.1.0",
            "merge_sha": _git(git_repo, "rev-parse", "HEAD").strip(),
        }

    def test_refuses_a_version_that_is_not_a_release_tag(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]
    ) -> None:
        monkeypatch.chdir(git_repo)
        (git_repo / CONFORMANCE.version_file).write_text(
            '[project]\nversion = "0.11.0-someone.1"\n'
        )
        with pytest.raises(SystemExit, match="X.Y.Z"):
            release_train.main(["publish-meta", "conformance"])
        assert outputs == {}

    def test_unknown_train_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            release_train.main(["bump", "sdk"])
