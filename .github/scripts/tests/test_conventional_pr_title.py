"""Tests for .github/scripts/conventional_pr_title.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import conventional_pr_title as cpt

TYPES = cpt.DEFAULT_TYPES
EXEMPT = cpt.DEFAULT_EXEMPT_ACTORS


# ---------------------------------------------------------------------------
# is_exempt
# ---------------------------------------------------------------------------


class TestIsExempt:
    def test_bump_version_branch(self):
        assert cpt.is_exempt("anything at all", "bump-version-main", "alice", EXEMPT)

    def test_bump_version_title(self):
        assert cpt.is_exempt("Bump version to 1.2.3", "feature/foo", "alice", EXEMPT)

    def test_release_title(self):
        assert cpt.is_exempt("chore: release 1.2.3", "feature/foo", "alice", EXEMPT)

    def test_scoped_release_title(self):
        assert cpt.is_exempt(
            "chore(release): release 1.2.3", "feature/foo", "alice", EXEMPT
        )

    def test_dependabot_is_exempt(self):
        assert cpt.is_exempt(
            "Bump urllib3 from 2.0.0 to 2.5.0",
            "dependabot/pip/urllib3-2.5.0",
            "dependabot[bot]",
            EXEMPT,
        )

    def test_renovate_is_not_exempt(self):
        # The fleet preset forces semanticCommitType: chore, so Renovate titles
        # are conventional already — a regression there should go red.
        assert not cpt.is_exempt(
            "Update dependency foo to v2", "renovate/foo-2.x", "renovate[bot]", EXEMPT
        )

    def test_human_pr_is_not_exempt(self):
        assert not cpt.is_exempt("add a thing", "feature/foo", "alice", EXEMPT)

    def test_empty_actor_does_not_match_empty_exempt_entry(self):
        # A blank --actor must never be swallowed by a stray empty entry.
        assert not cpt.is_exempt("add a thing", "feature/foo", "", ("",))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidateAccepts:
    @pytest.mark.parametrize(
        "title",
        [
            "feat: add incremental extraction",
            "fix(sql): retry a closed cursor",
            "feat(api)!: drop the v1 payload shape",
            "fix!: stop swallowing the timeout",
            "chore(deps): update dependency foo to v2",
            "revert: feat: add incremental extraction",
            "docs: explain the credential flow",
            "ci(release): pin the publish action",
        ],
    )
    def test_valid_titles(self, title: str):
        assert cpt.validate(title, TYPES) == ""

    def test_surrounding_whitespace_is_tolerated(self):
        assert cpt.validate("  feat: add a thing  ", TYPES) == ""

    def test_every_documented_type_is_accepted(self):
        for kind in TYPES:
            assert cpt.validate(f"{kind}: do the thing", TYPES) == ""


class TestValidateRejects:
    def test_no_type_prefix(self):
        msg = cpt.validate("add incremental extraction", TYPES)
        assert "not a conventional commit" in msg

    def test_dependabot_shaped_title(self):
        # Only exempt by actor — the grammar itself must still reject it.
        msg = cpt.validate("Bump urllib3 from 2.0.0 to 2.5.0", TYPES)
        assert "not a conventional commit" in msg

    def test_unknown_type(self):
        msg = cpt.validate("wip: still working", TYPES)
        assert "'wip' is not an allowed commit type" in msg

    def test_capitalised_type_is_rejected_with_a_hint(self):
        msg = cpt.validate("Feat: add a thing", TYPES)
        assert "'Feat' is not an allowed commit type" in msg
        assert "did you mean 'feat'" in msg

    def test_unknown_type_gets_no_lowercase_hint(self):
        msg = cpt.validate("Wip: still working", TYPES)
        assert "did you mean" not in msg

    def test_empty_scope(self):
        msg = cpt.validate("feat(): add a thing", TYPES)
        assert "empty scope" in msg

    def test_whitespace_only_scope(self):
        msg = cpt.validate("feat(   ): add a thing", TYPES)
        assert "empty scope" in msg

    def test_missing_description(self):
        msg = cpt.validate("feat:", TYPES)
        assert "no description" in msg

    def test_whitespace_only_description(self):
        msg = cpt.validate("feat:   ", TYPES)
        assert "no description" in msg

    def test_missing_space_after_colon(self):
        msg = cpt.validate("feat:add a thing", TYPES)
        assert "space after the colon" in msg

    def test_nested_scope_parens(self):
        msg = cpt.validate("feat(a)(b): add a thing", TYPES)
        assert "not a conventional commit" in msg

    def test_bang_before_scope_is_not_conventional(self):
        msg = cpt.validate("feat!(api): add a thing", TYPES)
        assert "not a conventional commit" in msg

    def test_error_message_lists_allowed_types(self):
        msg = cpt.validate("add a thing", TYPES)
        for kind in TYPES:
            assert f"'{kind}'" in msg


class TestValidateCustomTypes:
    def test_narrowed_type_list_rejects_a_default_type(self):
        assert cpt.validate("docs: explain it", ("feat", "fix")) != ""

    def test_extra_type_is_accepted(self):
        assert cpt.validate("hotfix: patch it", ("feat", "fix", "hotfix")) == ""


# ---------------------------------------------------------------------------
# parse_list
# ---------------------------------------------------------------------------


class TestParseList:
    def test_comma_separated(self):
        assert cpt.parse_list("feat,fix,chore") == ("feat", "fix", "chore")

    def test_tolerates_spaces_and_newlines(self):
        assert cpt.parse_list("feat, fix\n chore ") == ("feat", "fix", "chore")

    def test_empty_value(self):
        assert cpt.parse_list("  ") == ()

    def test_no_empty_entries_from_trailing_comma(self):
        assert cpt.parse_list("feat,fix,") == ("feat", "fix")


# ---------------------------------------------------------------------------
# run / comment output
# ---------------------------------------------------------------------------


class TestRun:
    def test_valid_title_writes_no_comment(self, tmp_path: Path):
        comment = tmp_path / "comment.md"
        assert (
            cpt.run(
                "feat: add a thing", "feature/foo", "alice", TYPES, EXEMPT, str(comment)
            )
            == ""
        )
        assert not comment.exists()

    def test_exempt_title_writes_no_comment(self, tmp_path: Path):
        comment = tmp_path / "comment.md"
        assert (
            cpt.run(
                "Bump version to 1.2.3",
                "bump-version-main",
                "atlan-ci",
                TYPES,
                EXEMPT,
                str(comment),
            )
            == ""
        )
        assert not comment.exists()

    def test_violation_writes_the_comment(self, tmp_path: Path):
        comment = tmp_path / "comment.md"
        msg = cpt.run(
            "add a thing", "feature/foo", "alice", TYPES, EXEMPT, str(comment)
        )
        assert msg
        body = comment.read_text(encoding="utf-8")
        assert msg in body
        assert "not a conventional commit" in body
        for kind in TYPES:
            assert f"`{kind}`" in body


class TestMain:
    def _run_main(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str):
        out = tmp_path / "github_output"
        out.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "conventional_pr_title.py",
                "--comment-out",
                str(tmp_path / "comment.md"),
                *argv,
            ],
        )
        assert cpt.main() == 0
        return dict(
            line.split("=", 1)
            for line in out.read_text(encoding="utf-8").splitlines()
            if line
        )

    def test_valid_title_outputs(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        outputs = self._run_main(
            monkeypatch, tmp_path, "--pr-title", "feat: add a thing"
        )
        assert outputs["violation"] == "false"
        assert outputs["error_message"] == ""

    def test_invalid_title_outputs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        outputs = self._run_main(monkeypatch, tmp_path, "--pr-title", "add a thing")
        assert outputs["violation"] == "true"
        assert "not a conventional commit" in outputs["error_message"]

    def test_error_message_output_is_single_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # It is written to $GITHUB_OUTPUT as a bare key=value pair and echoed
        # into an ::error:: annotation — a newline would truncate both.
        outputs = self._run_main(monkeypatch, tmp_path, "--pr-title", "Wip: nope")
        assert "\n" not in outputs["error_message"]

    def test_empty_types_input_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # An empty `types:` input must not accept everything (or nothing).
        outputs = self._run_main(
            monkeypatch, tmp_path, "--pr-title", "feat: add a thing", "--types", ""
        )
        assert outputs["violation"] == "false"
        outputs = self._run_main(
            monkeypatch, tmp_path, "--pr-title", "wip: add a thing", "--types", ""
        )
        assert outputs["violation"] == "true"

    def test_empty_exempt_actors_input_polices_everyone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        outputs = self._run_main(
            monkeypatch,
            tmp_path,
            "--pr-title",
            "Bump urllib3 from 2.0.0 to 2.5.0",
            "--actor",
            "dependabot[bot]",
            "--exempt-actors",
            "",
        )
        assert outputs["violation"] == "true"
