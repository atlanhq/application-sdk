"""The ANN401 (``Any`` in a signature) baseline check (FND-4301).

The comparison and base-revision logic run against stubs; ruff runs for real
once against a throwaway tree, and once against this repo, so the committed
baseline is held to the code it describes. Every way a ruff config can
suppress a site is tried against a throwaway tree, to prove ``--isolated``
ignores it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Callable, Sequence

import check_ann401_baseline as cab
import pytest
import yaml
from check_pyright_baseline import BaseRevisionError

REPO_ROOT = Path(__file__).resolve().parents[3]

_TYPED = "def f(x: int) -> int:\n    return x\n"
_TWO_ANY = (
    "from typing import Any\n\n\ndef f(x: Any) -> Any:  # noqa: ANN401\n    return x\n"
)


def _git_result(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


class _Git:
    """A git stub answering by the subcommand, recording every call."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return self.answers.get(cmd[0], _git_result())


# ── the committed baseline, measured for real ──────────────────────────────


class TestRepoBaseline:
    def test_matches_ruff_on_this_tree(self) -> None:
        measured, _ = cab.measure()
        baseline = cab.parse_baseline((REPO_ROOT / cab.BASELINE_REL).read_text())
        above, below = cab.compare(baseline, measured)
        assert not above, "New `Any` in a signature; type it instead:\n" + "\n".join(
            above
        )
        assert not below, (
            f"Fewer ANN401 sites than the baseline; run {cab.UPDATE_COMMAND}\n"
            + "\n".join(below)
        )

    def test_is_canonically_written(self) -> None:
        text = (REPO_ROOT / cab.BASELINE_REL).read_text()
        assert text == cab.render_baseline(cab.parse_baseline(text))

    def test_target_version_is_requires_python(self) -> None:
        # --isolated reads no config, so the floor is passed by hand.
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        floor = project["project"]["requires-python"]
        assert floor == ">=3." + cab.TARGET_VERSION.removeprefix("py3")


#: Every config-file route ruff has to switch ANN401 off for a file. Each is
#: written into a throwaway tree; the check must still count both sites.
_SUPPRESSING_CONFIGS = {
    "ignore": '[tool.ruff.lint]\nignore = ["ANN401"]\n',
    "extend-ignore": '[tool.ruff.lint]\nextend-ignore = ["ANN"]\n',
    "per-file-ignores": (
        '[tool.ruff.lint.per-file-ignores]\n"application_sdk/m.py" = ["ANN401"]\n'
    ),
    "extend-per-file-ignores": (
        '[tool.ruff.lint.extend-per-file-ignores]\n"application_sdk/m.py" = ["ALL"]\n'
    ),
    "allow-star-arg-any": (
        "[tool.ruff.lint.flake8-annotations]\nallow-star-arg-any = true\n"
    ),
    "exclude": '[tool.ruff]\nexclude = ["application_sdk"]\n',
    "extend-exclude": '[tool.ruff]\nextend-exclude = ["application_sdk/m.py"]\n',
}

_STAR_ANY = (
    "from typing import Any\n\n\ndef f(*a: Any, **k: Any) -> None:\n    return None\n"
)


class TestBaselineIsTheOnlySuppression:
    """``Done when``: the baseline file is the only way ANN401 is let through."""

    @pytest.mark.parametrize("setting", sorted(_SUPPRESSING_CONFIGS))
    def test_no_root_config_hides_a_site(self, tmp_path: Path, setting: str) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUPPRESSING_CONFIGS[setting])
        (tmp_path / cab.SOURCE_DIR).mkdir()
        (tmp_path / cab.SOURCE_DIR / "m.py").write_text(_STAR_ANY)
        assert cab.measure(tmp_path)[0] == {f"{cab.SOURCE_DIR}/m.py": 2}

    def test_no_nested_config_hides_a_site(self, tmp_path: Path) -> None:
        source = tmp_path / cab.SOURCE_DIR
        source.mkdir()
        (source / "ruff.toml").write_text('[lint]\nignore = ["ANN401"]\n')
        (source / "m.py").write_text(_STAR_ANY)
        assert cab.measure(tmp_path)[0] == {f"{cab.SOURCE_DIR}/m.py": 2}

    def test_noqa_comments_do_not_hide_a_site(self, tmp_path: Path) -> None:
        (tmp_path / cab.SOURCE_DIR).mkdir()
        (tmp_path / cab.SOURCE_DIR / "m.py").write_text(_TWO_ANY)
        counts, violations = cab.measure(tmp_path)
        assert counts == {f"{cab.SOURCE_DIR}/m.py": 2}
        assert [v[1] for v in violations] == [4, 4]

    def test_typed_code_has_no_findings(self, tmp_path: Path) -> None:
        (tmp_path / cab.SOURCE_DIR).mkdir()
        (tmp_path / cab.SOURCE_DIR / "m.py").write_text(_TYPED)
        assert cab.measure(tmp_path) == ({}, [])


# ── comparison ──────────────────────────────────────────────────────────────


class TestCompare:
    def test_new_site_is_above(self) -> None:
        assert cab.compare({"a.py": 1}, {"a.py": 2}) == (
            ["a.py: 2 > baseline 1"],
            [],
        )

    def test_new_file_is_above(self) -> None:
        assert cab.compare({}, {"b.py": 1}) == (["b.py: 1 > baseline 0"], [])

    def test_paid_down_site_is_below(self) -> None:
        assert cab.compare({"a.py": 2}, {"a.py": 1}) == (
            [],
            ["a.py: 1 < baseline 2"],
        )

    def test_cleared_file_is_below(self) -> None:
        assert cab.compare({"a.py": 1}, {}) == ([], ["a.py: 0 < baseline 1"])

    def test_one_file_paying_down_does_not_buy_another_a_site(self) -> None:
        above, below = cab.compare({"a.py": 2, "b.py": 1}, {"a.py": 1, "b.py": 2})
        assert above == ["b.py: 2 > baseline 1"]
        assert below == ["a.py: 1 < baseline 2"]


class TestParseBaseline:
    @pytest.mark.parametrize(
        "text",
        ["[]", '{"a.py": "1"}', '{"a.py": 0}', '{"a.py": -1}', '{"a.py": true}'],
    )
    def test_rejects_malformed(self, text: str) -> None:
        with pytest.raises(ValueError):
            cab.parse_baseline(text)

    def test_round_trips(self) -> None:
        counts = {"b.py": 2, "a.py": 1}
        assert cab.parse_baseline(cab.render_baseline(counts)) == counts
        assert list(json.loads(cab.render_baseline(counts))) == ["a.py", "b.py"]


class TestParseReport:
    def test_keeps_only_ann401_and_relativises(self, tmp_path: Path) -> None:
        report = json.dumps(
            [
                {
                    "code": "ANN401",
                    "filename": str(tmp_path / "application_sdk/x.py"),
                    "location": {"row": 3, "column": 9},
                    "message": "m",
                },
                {
                    "code": "F401",
                    "filename": str(tmp_path / "application_sdk/x.py"),
                    "location": {"row": 1, "column": 1},
                    "message": "other",
                },
            ]
        )
        counts, violations = cab.parse_report(report, tmp_path)
        assert counts == {"application_sdk/x.py": 1}
        assert violations == [("application_sdk/x.py", 3, 9, "m")]

    def test_non_json_fails(self, tmp_path: Path) -> None:
        with pytest.raises(cab.RuffRunError):
            cab.parse_report("not json", tmp_path)


# ── the base revision ───────────────────────────────────────────────────────


class TestRaisedSinceBase:
    def test_unchanged_and_lowered_counts_pass(self) -> None:
        assert cab.raised_since_base({"a.py": 2}, {"a.py": 2}, {}) == []
        assert cab.raised_since_base({"a.py": 2}, {"a.py": 1}, {}) == []

    def test_raised_count_fails(self) -> None:
        assert cab.raised_since_base({"a.py": 1}, {"a.py": 2}, {}) == [
            "a.py: baseline 2 > base revision's 1"
        ]

    def test_new_file_fails(self) -> None:
        assert cab.raised_since_base({}, {"b.py": 1}, {}) == [
            "b.py: baseline 1 > base revision's 0"
        ]

    def test_rename_carries_the_count(self) -> None:
        assert (
            cab.raised_since_base({"old.py": 2}, {"new.py": 2}, {"old.py": "new.py"})
            == []
        )


class TestReadBaseBaseline:
    def test_empty_sha_fails_closed(self) -> None:
        with pytest.raises(BaseRevisionError):
            cab.read_base_baseline("", _Git({}))

    def test_missing_file_is_none(self) -> None:
        assert cab.read_base_baseline("abc", _Git({"ls-tree": _git_result()})) is None

    def test_reads_the_base_file(self) -> None:
        git = _Git(
            {
                "ls-tree": _git_result(stdout="100644 blob x\tpath\n"),
                "show": _git_result(stdout='{"a.py": 3}\n'),
            }
        )
        assert cab.read_base_baseline("abc", git) == {"a.py": 3}

    def test_unfetchable_base_fails_closed(self) -> None:
        git = _Git(
            {
                "cat-file": _git_result(returncode=1),
                "rev-parse": _git_result(stdout="true\n"),
                "fetch": _git_result(returncode=128, stderr="no such ref"),
            }
        )
        with pytest.raises(BaseRevisionError, match="could not fetch"):
            cab.read_base_baseline("abc", git)
        assert ["fetch", "--no-tags", "--depth=1", "origin", "abc"] in git.calls

    def test_unreadable_listing_fails_closed(self) -> None:
        git = _Git({"ls-tree": _git_result(returncode=128, stderr="bad tree")})
        with pytest.raises(BaseRevisionError):
            cab.read_base_baseline("abc", git)

    def test_malformed_base_baseline_fails_closed(self) -> None:
        git = _Git(
            {
                "ls-tree": _git_result(stdout="100644 blob x\tpath\n"),
                "show": _git_result(stdout="[]"),
            }
        )
        with pytest.raises(BaseRevisionError, match="not a baseline"):
            cab.read_base_baseline("abc", git)


class TestCheckBase:
    def test_raise_over_base_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        git = _Git(
            {
                "ls-tree": _git_result(stdout="100644 blob x\tpath\n"),
                "show": _git_result(stdout='{"a.py": 1}\n'),
            }
        )
        assert cab.check_base("abc", {"a.py": 2}, git) is True
        assert "a.py: baseline 2 > base revision's 1" in capsys.readouterr().out

    def test_introducing_pr_is_held_to_the_base_tree(self) -> None:
        # No baseline at base: ruff's findings on the base tree stand in, so
        # the introducing PR cannot bake in a site it adds itself.
        git = _Git({"ls-tree": _git_result()})
        measured_trees: list[Path] = []

        def fake_measure(tree: Path) -> tuple[cab.Counts, list[cab.Violation]]:
            measured_trees.append(tree)
            return {"a.py": 1}, []

        assert cab.check_base("abc", {"a.py": 2}, git, fake_measure) is True
        assert cab.check_base("abc", {"a.py": 1}, git, fake_measure) is False
        assert measured_trees and all(t.name == "base" for t in measured_trees)
        adds = [c for c in git.calls if c[:2] == ["worktree", "add"]]
        removes = [c for c in git.calls if c[:2] == ["worktree", "remove"]]
        assert len(adds) == len(removes) == 2

    def test_failed_worktree_fails_closed(self) -> None:
        git = _Git(
            {
                "ls-tree": _git_result(),
                "worktree": _git_result(returncode=128, stderr="nope"),
            }
        )
        assert cab.check_base("abc", {"a.py": 1}, git) is True


# ── the CLI ─────────────────────────────────────────────────────────────────


class TestMain:
    def _measure(
        self, counts: cab.Counts
    ) -> Callable[[Path], tuple[cab.Counts, list[cab.Violation]]]:
        def fake(_tree: Path) -> tuple[cab.Counts, list[cab.Violation]]:
            return counts, [(f, 1, 1, "m") for f in counts]

        return fake

    def test_equal_passes(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        baseline.write_text(cab.render_baseline({"a.py": 1}))
        assert cab.main([], baseline, self._measure({"a.py": 1})) == 0

    def test_above_fails(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        baseline.write_text(cab.render_baseline({"a.py": 1}))
        assert cab.main([], baseline, self._measure({"a.py": 2})) == 1

    def test_below_fails(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        baseline.write_text(cab.render_baseline({"a.py": 2}))
        assert cab.main([], baseline, self._measure({"a.py": 1})) == 1

    def test_a_paid_down_tree_passes(self, tmp_path: Path) -> None:
        # Nothing measured, and the canary proves ruff still reports ANN401:
        # the ratchet has reached zero, which must not fail.
        baseline = tmp_path / "b.json"
        baseline.write_text("{}\n")

        def fake(tree: Path) -> tuple[cab.Counts, list[cab.Violation]]:
            if (tree / cab.SOURCE_DIR / "canary.py").exists():
                return {f"{cab.SOURCE_DIR}/canary.py": 2}, []
            return {}, []

        assert cab.main([], baseline, fake) == 0

    def test_ruff_that_reports_nothing_fails(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        baseline.write_text("{}\n")
        assert cab.main([], baseline, self._measure({})) == 2

    def test_canary_is_flagged_by_real_ruff(self) -> None:
        assert cab.ruff_reports_ann401()

    def test_update_writes_the_measurement(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        baseline.write_text(cab.render_baseline({"a.py": 5}))
        assert cab.main(["--update"], baseline, self._measure({"a.py": 1})) == 0
        assert cab.parse_baseline(baseline.read_text()) == {"a.py": 1}

    def test_base_sha_raise_fails_even_when_counts_match(self, tmp_path: Path) -> None:
        baseline = tmp_path / "b.json"
        baseline.write_text(cab.render_baseline({"a.py": 2}))
        git = _Git(
            {
                "ls-tree": _git_result(stdout="100644 blob x\tpath\n"),
                "show": _git_result(stdout='{"a.py": 1}\n'),
            }
        )
        argv = ["--base-sha", "abc"]
        assert cab.main(argv, baseline, self._measure({"a.py": 2}), git) == 1


# ── wiring ──────────────────────────────────────────────────────────────────


class TestWiring:
    def test_pre_commit_runs_the_check(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
        hooks = [h for repo in config["repos"] for h in repo["hooks"]]
        (hook,) = [h for h in hooks if h["id"] == "ann401-baseline"]
        assert "check_ann401_baseline.py" in hook["entry"]
        assert hook["pass_filenames"] is False
        files = re.compile(hook["files"])
        for path in (
            "application_sdk/app/base.py",
            cab.BASELINE_REL,
            "pyproject.toml",
        ):
            assert files.search(path), path

    def test_ci_holds_the_baseline_to_the_base_revision(self) -> None:
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/scripts-tests.yaml").read_text()
        )
        runs = [
            step.get("run", "")
            for job in workflow["jobs"].values()
            for step in job["steps"]
        ]
        assert any(
            "check_ann401_baseline.py" in run and "--base-sha" in run for run in runs
        )
