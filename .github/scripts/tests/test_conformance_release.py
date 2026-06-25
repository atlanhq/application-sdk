"""Tests for .github/scripts/conformance_release.py."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import conformance_release

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pyproject(version: str, tmp_path: Path) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(f'[project]\nname = "foo"\nversion = "{version}"\n')
    return p


def _version_py(version: str, tmp_path: Path) -> Path:
    p = tmp_path / "__init__.py"
    p.write_text(f'__version__ = "{version}"\n')
    return p


def _changelog(content: str, tmp_path: Path) -> Path:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# compute_bump
# ---------------------------------------------------------------------------


class TestComputeBump:
    def test_breaking_via_subject_exclamation(self) -> None:
        assert (
            conformance_release.compute_bump("feat!: drop Python 3.10", "") == "major"
        )

    def test_breaking_via_scoped_exclamation(self) -> None:
        assert (
            conformance_release.compute_bump("feat(ci)!: remove legacy hook", "")
            == "major"
        )

    def test_breaking_via_body_breaking_change(self) -> None:
        assert (
            conformance_release.compute_bump(
                "feat: something", "BREAKING CHANGE: old API removed"
            )
            == "major"
        )

    def test_breaking_via_body_breaking_underscore(self) -> None:
        assert (
            conformance_release.compute_bump(
                "feat: something", "BREAKING_CHANGE: removed"
            )
            == "major"
        )

    def test_breaking_via_body_breaking_dash(self) -> None:
        assert (
            conformance_release.compute_bump(
                "feat: something", "BREAKING-CHANGE: removed"
            )
            == "major"
        )

    def test_feat_gives_minor(self) -> None:
        assert conformance_release.compute_bump("feat: add new rule", "") == "minor"

    def test_feat_scoped_gives_minor(self) -> None:
        assert (
            conformance_release.compute_bump("feat(conformance): add E019", "")
            == "minor"
        )

    def test_fix_gives_patch(self) -> None:
        assert (
            conformance_release.compute_bump("fix: correct regex anchor", "") == "patch"
        )

    def test_chore_gives_patch(self) -> None:
        assert conformance_release.compute_bump("chore: update deps", "") == "patch"

    def test_empty_subjects_gives_patch(self) -> None:
        assert conformance_release.compute_bump("", "") == "patch"

    def test_multiline_subjects_feat_gives_minor(self) -> None:
        subjects = "chore: update lock\nfeat: new check\nfix: typo"
        assert conformance_release.compute_bump(subjects, "") == "minor"

    def test_breaking_takes_precedence_over_feat(self) -> None:
        subjects = "feat!: overhaul\nfeat: add thing"
        assert conformance_release.compute_bump(subjects, "") == "major"


# ---------------------------------------------------------------------------
# bump_version
# ---------------------------------------------------------------------------


class TestBumpVersion:
    def test_major_bump_resets_minor_and_patch(self) -> None:
        assert conformance_release.bump_version("1.2.3", "major") == "2.0.0"

    def test_minor_bump_resets_patch(self) -> None:
        assert conformance_release.bump_version("1.2.3", "minor") == "1.3.0"

    def test_patch_bump_increments_patch(self) -> None:
        assert conformance_release.bump_version("1.2.3", "patch") == "1.2.4"

    def test_from_zero(self) -> None:
        assert conformance_release.bump_version("0.0.0", "minor") == "0.1.0"

    def test_large_numbers(self) -> None:
        assert conformance_release.bump_version("10.20.30", "patch") == "10.20.31"

    def test_major_from_zero(self) -> None:
        assert conformance_release.bump_version("0.1.0", "major") == "1.0.0"


# ---------------------------------------------------------------------------
# categorize
# ---------------------------------------------------------------------------


class TestCategorize:
    def setup_method(self) -> None:
        self._orig_repo = conformance_release.REPO
        conformance_release.REPO = "testorg/testrepo"

    def teardown_method(self) -> None:
        conformance_release.REPO = self._orig_repo

    def _link(self, sha: str) -> str:
        return f"https://github.com/testorg/testrepo/commit/{sha}"

    def test_feat_lands_in_features(self) -> None:
        cats = conformance_release.categorize([("abc1234", "feat: add rule E019", "")])
        assert len(cats["features"]) == 1
        assert cats["features"][0] == (self._link("abc1234"), "add rule E019")

    def test_feat_scope_stripped(self) -> None:
        cats = conformance_release.categorize(
            [("abc1234", "feat(conformance): new check", "")]
        )
        assert cats["features"][0][1] == "new check"

    def test_fix_lands_in_fixes(self) -> None:
        cats = conformance_release.categorize([("abc1234", "fix: correct anchor", "")])
        assert len(cats["fixes"]) == 1
        assert cats["fixes"][0][1] == "correct anchor"

    def test_fix_scope_stripped(self) -> None:
        cats = conformance_release.categorize([("abc1234", "fix(ci): pin digest", "")])
        assert cats["fixes"][0][1] == "pin digest"

    def test_chore_lands_in_other(self) -> None:
        cats = conformance_release.categorize([("abc1234", "chore: bump uv", "")])
        assert len(cats["other"]) == 1

    def test_breaking_subject_lands_in_breaking(self) -> None:
        cats = conformance_release.categorize(
            [("abc1234", "feat!: remove old API", "")]
        )
        assert len(cats["breaking"]) == 1
        assert "remove old API" in cats["breaking"][0][1]

    def test_breaking_body_lands_in_breaking(self) -> None:
        cats = conformance_release.categorize(
            [("abc1234", "feat: overhaul", "BREAKING CHANGE: old interface removed")]
        )
        assert len(cats["breaking"]) == 1

    def test_mixed_commits_correctly_bucketed(self) -> None:
        commits = [
            ("aaa1111", "feat: add thing", ""),
            ("bbb2222", "fix: repair thing", ""),
            ("ccc3333", "chore: housekeeping", ""),
            ("ddd4444", "feat!: breaking change", ""),
        ]
        cats = conformance_release.categorize(commits)
        assert len(cats["features"]) == 1
        assert len(cats["fixes"]) == 1
        assert len(cats["other"]) == 1
        assert len(cats["breaking"]) == 1

    def test_empty_commits_gives_empty_cats(self) -> None:
        cats = conformance_release.categorize([])
        assert all(len(v) == 0 for v in cats.values())


# ---------------------------------------------------------------------------
# format_block
# ---------------------------------------------------------------------------


class TestFormatBlock:
    def _empty_cats(self):
        return {"breaking": [], "features": [], "fixes": [], "other": []}

    def test_contains_version_header(self) -> None:
        block = conformance_release.format_block("1.2.3", self._empty_cats())
        assert "## [1.2.3]" in block

    def test_contains_today(self) -> None:
        block = conformance_release.format_block("1.2.3", self._empty_cats())
        assert date.today().isoformat() in block

    def test_features_section_present(self) -> None:
        cats = self._empty_cats()
        cats["features"] = [("https://github.com/o/r/commit/abc1234", "cool feature")]
        block = conformance_release.format_block("1.0.0", cats)
        assert "### Features" in block
        assert "cool feature" in block
        assert "abc1234" in block

    def test_fixes_section_present(self) -> None:
        cats = self._empty_cats()
        cats["fixes"] = [("https://github.com/o/r/commit/def5678", "fixed bug")]
        block = conformance_release.format_block("1.0.0", cats)
        assert "### Bug fixes" in block
        assert "fixed bug" in block

    def test_breaking_section_present(self) -> None:
        cats = self._empty_cats()
        cats["breaking"] = [("https://github.com/o/r/commit/ghi9012", "dropped API")]
        block = conformance_release.format_block("2.0.0", cats)
        assert "### Breaking changes" in block
        assert "dropped API" in block

    def test_other_section_present(self) -> None:
        cats = self._empty_cats()
        cats["other"] = [("https://github.com/o/r/commit/jkl3456", "chore: update")]
        block = conformance_release.format_block("1.0.1", cats)
        assert "### Other changes" in block

    def test_empty_section_not_present(self) -> None:
        block = conformance_release.format_block("1.0.0", self._empty_cats())
        assert "### Features" not in block
        assert "### Bug fixes" not in block
        assert "### Breaking changes" not in block


# ---------------------------------------------------------------------------
# prepend_changelog
# ---------------------------------------------------------------------------


class TestPrependChangelog:
    def test_prepends_to_empty_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cl = tmp_path / "CHANGELOG.md"
        monkeypatch.setattr(conformance_release, "CHANGELOG", str(cl))
        block = "## [0.1.0] - 2026-01-01\n\n### Features\n\n- first thing\n"
        conformance_release.prepend_changelog(block)
        content = cl.read_text()
        assert "## [0.1.0]" in content

    def test_prepends_before_existing_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cl = _changelog(
            "# Changelog\n\n## [0.1.0] - 2026-01-01\n\n- old entry\n", tmp_path
        )
        monkeypatch.setattr(conformance_release, "CHANGELOG", str(cl))
        block = "## [0.2.0] - 2026-06-01\n\n### Features\n\n- new thing\n"
        conformance_release.prepend_changelog(block)
        content = cl.read_text()
        new_pos = content.index("0.2.0")
        old_pos = content.index("0.1.0")
        assert new_pos < old_pos

    def test_prepends_to_header_only_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cl = _changelog("# Changelog\n", tmp_path)
        monkeypatch.setattr(conformance_release, "CHANGELOG", str(cl))
        block = "## [1.0.0] - 2026-01-01\n\n- something\n"
        conformance_release.prepend_changelog(block)
        content = cl.read_text()
        assert "# Changelog" in content
        assert "## [1.0.0]" in content

    def test_does_not_duplicate_existing_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = "# Changelog\n\n## [0.1.0] - 2026-01-01\n\n- old\n"
        cl = _changelog(original, tmp_path)
        monkeypatch.setattr(conformance_release, "CHANGELOG", str(cl))
        block = "## [0.2.0] - 2026-06-01\n\n- new\n"
        conformance_release.prepend_changelog(block)
        content = cl.read_text()
        assert content.count("## [0.1.0]") == 1
        assert content.count("## [0.2.0]") == 1


# ---------------------------------------------------------------------------
# update_pyproject
# ---------------------------------------------------------------------------


class TestUpdatePyproject:
    def test_updates_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _pyproject("1.0.0", tmp_path)
        monkeypatch.setattr(conformance_release, "PYPROJECT", str(p))
        conformance_release.update_pyproject("1.0.0", "1.1.0")
        assert 'version = "1.1.0"' in p.read_text()

    def test_does_not_double_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _pyproject("1.0.0", tmp_path)
        monkeypatch.setattr(conformance_release, "PYPROJECT", str(p))
        conformance_release.update_pyproject("1.0.0", "2.0.0")
        assert p.read_text().count("2.0.0") == 1

    def test_exits_when_version_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _pyproject("9.9.9", tmp_path)
        monkeypatch.setattr(conformance_release, "PYPROJECT", str(p))
        with pytest.raises(SystemExit):
            conformance_release.update_pyproject("1.0.0", "1.1.0")


# ---------------------------------------------------------------------------
# main() — bootstrap path (no conformance-v* tags yet)
# ---------------------------------------------------------------------------


class TestMainBootstrap:
    """main() bootstrap branch: no conformance-vX.Y.Z tag for the current version exists.

    Two sub-cases:
      - No conformance-v* tags at all → fall back to the initial commit (first release).
      - Other conformance-v* tags exist but not the current one → error exit.
    """

    def _setup(
        self, version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple:
        pyproject = _pyproject(version, tmp_path)
        version_py = _version_py(version, tmp_path)
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")
        relnotes = tmp_path / "release-notes.md"

        monkeypatch.setattr(conformance_release, "PYPROJECT", str(pyproject))
        monkeypatch.setattr(conformance_release, "VERSION_PY", str(version_py))
        monkeypatch.setattr(conformance_release, "CHANGELOG", str(changelog))
        monkeypatch.setattr(conformance_release, "RELEASE_NOTES_FILE", str(relnotes))
        return pyproject, version_py, changelog, relnotes

    def test_first_release_falls_back_to_initial_commit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        pyproject, version_py, _changelog, relnotes = self._setup(
            "0.1.0", tmp_path, monkeypatch
        )

        initial_sha = "abc1234def5678"

        def fake_run(cmd, **_kw):
            if "tag" in cmd and "--list" in cmd:
                return ""  # no existing conformance tags
            if "rev-list" in cmd:
                return initial_sha
            if "--format=%s" in cmd:
                return "feat: add initial conformance suite"
            if "--format=%B" in cmd:
                return ""
            if any("--format=%H" in str(c) for c in cmd):
                return "abc1234\x00feat: add initial conformance suite\x00\x1e"
            return ""

        monkeypatch.setattr(conformance_release, "tag_exists", lambda _tag: False)
        monkeypatch.setattr(conformance_release, "_run", fake_run)

        outputs: dict = {}
        monkeypatch.setattr(
            conformance_release, "_set_output", lambda k, v: outputs.update({k: v})
        )

        conformance_release.main()

        out = capsys.readouterr().out
        assert "first release" in out
        assert "0.2.0" in out  # feat: from 0.1.0 → minor bump → 0.2.0
        assert outputs["skip"] == "false"
        assert outputs["old"] == "0.1.0"
        assert outputs["new"] == "0.2.0"
        assert outputs["tag"] == "conformance-v0.2.0"
        assert 'version = "0.2.0"' in pyproject.read_text()
        assert '__version__ = "0.2.0"' in version_py.read_text()
        assert relnotes.exists()

    def test_error_when_other_tags_exist_but_current_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup("0.1.0", tmp_path, monkeypatch)

        def fake_run(cmd, **_kw):
            if "tag" in cmd and "--list" in cmd:
                return "conformance-v0.2.0"  # stale tags exist — error condition
            return ""

        monkeypatch.setattr(conformance_release, "tag_exists", lambda _tag: False)
        monkeypatch.setattr(conformance_release, "_run", fake_run)

        with pytest.raises(SystemExit):
            conformance_release.main()


# ---------------------------------------------------------------------------
# main() — normal path (existing conformance-vX.Y.Z tag found)
# ---------------------------------------------------------------------------


class TestMainExistingTag:
    """main() normal path: tag_exists returns True, so no bootstrap fallback."""

    def _setup(
        self, version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple:
        pyproject = _pyproject(version, tmp_path)
        version_py = _version_py(version, tmp_path)
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")
        relnotes = tmp_path / "release-notes.md"

        monkeypatch.setattr(conformance_release, "PYPROJECT", str(pyproject))
        monkeypatch.setattr(conformance_release, "VERSION_PY", str(version_py))
        monkeypatch.setattr(conformance_release, "CHANGELOG", str(changelog))
        monkeypatch.setattr(conformance_release, "RELEASE_NOTES_FILE", str(relnotes))
        return pyproject, version_py, changelog, relnotes

    def test_no_commits_sets_skip_true(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        self._setup("0.2.0", tmp_path, monkeypatch)

        monkeypatch.setattr(conformance_release, "tag_exists", lambda _tag: True)
        monkeypatch.setattr(conformance_release, "_run", lambda _cmd, **_kw: "")

        outputs: dict = {}
        monkeypatch.setattr(
            conformance_release, "_set_output", lambda k, v: outputs.update({k: v})
        )

        conformance_release.main()

        assert outputs.get("skip") == "true"
        assert "No unreleased" in capsys.readouterr().out

    def test_fix_commits_produce_patch_bump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pyproject, version_py, _changelog, _relnotes = self._setup(
            "0.2.0", tmp_path, monkeypatch
        )

        def fake_run(cmd, **_kw):
            if "--format=%s" in cmd:
                return "fix: correct anchor"
            if "--format=%B" in cmd:
                return ""
            if any("--format=%H" in str(c) for c in cmd):
                return "abc5678\x00fix: correct anchor\x00\x1e"
            return ""

        monkeypatch.setattr(conformance_release, "tag_exists", lambda _tag: True)
        monkeypatch.setattr(conformance_release, "_run", fake_run)

        outputs: dict = {}
        monkeypatch.setattr(
            conformance_release, "_set_output", lambda k, v: outputs.update({k: v})
        )

        conformance_release.main()

        assert outputs["skip"] == "false"
        assert outputs["new"] == "0.2.1"  # patch bump from 0.2.0
        assert 'version = "0.2.1"' in pyproject.read_text()
        assert '__version__ = "0.2.1"' in version_py.read_text()

    def test_feat_commits_produce_minor_bump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pyproject, _ver_py, _changelog, _relnotes = self._setup(
            "0.2.0", tmp_path, monkeypatch
        )

        def fake_run(cmd, **_kw):
            if "--format=%s" in cmd:
                return "feat: add new check"
            if "--format=%B" in cmd:
                return ""
            if any("--format=%H" in str(c) for c in cmd):
                return "abc9999\x00feat: add new check\x00\x1e"
            return ""

        monkeypatch.setattr(conformance_release, "tag_exists", lambda _tag: True)
        monkeypatch.setattr(conformance_release, "_run", fake_run)

        outputs: dict = {}
        monkeypatch.setattr(
            conformance_release, "_set_output", lambda k, v: outputs.update({k: v})
        )

        conformance_release.main()

        assert outputs["new"] == "0.3.0"  # minor bump from 0.2.0
        assert 'version = "0.3.0"' in pyproject.read_text()


# ---------------------------------------------------------------------------
# update_version_py
# ---------------------------------------------------------------------------


class TestUpdateVersionPy:
    def test_updates_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _version_py("0.1.0", tmp_path)
        monkeypatch.setattr(conformance_release, "VERSION_PY", str(p))
        conformance_release.update_version_py("0.1.0", "0.2.0")
        assert '__version__ = "0.2.0"' in p.read_text()

    def test_does_not_double_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _version_py("1.0.0", tmp_path)
        monkeypatch.setattr(conformance_release, "VERSION_PY", str(p))
        conformance_release.update_version_py("1.0.0", "2.0.0")
        assert p.read_text().count("2.0.0") == 1

    def test_exits_when_version_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _version_py("9.9.9", tmp_path)
        monkeypatch.setattr(conformance_release, "VERSION_PY", str(p))
        with pytest.raises(SystemExit):
            conformance_release.update_version_py("1.0.0", "2.0.0")
