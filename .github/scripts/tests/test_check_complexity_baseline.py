"""The C901 / PLR0915 function-size baseline check (FND-4266).

The comparison and base-revision logic run against stubs; ruff runs for real
against throwaway trees, and once against this repo, so the committed baseline
is held to the code it describes. Every way a ruff config can suppress a
finding is tried against a throwaway tree, to prove ``--isolated`` ignores it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Sequence

import check_complexity_baseline as ccb
import pytest
import yaml
from check_pyright_baseline import BaseRevisionError

REPO_ROOT = Path(__file__).resolve().parents[3]
_M = f"{ccb.SOURCE_DIR}/m.py"
_BOTH = {rule: ["f"] for rule in ccb.RULES}

_SMALL = "def f(x: int) -> int:\n    return x\n"


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


def _base_has(baseline: ccb.Baseline) -> _Git:
    return _Git(
        {
            "ls-tree": _git_result(stdout="100644 blob x\tpath\n"),
            "show": _git_result(stdout=ccb.render_baseline(baseline)),
        }
    )


def _tree(tmp_path: Path, source: str, name: str = "m.py") -> Path:
    (tmp_path / ccb.SOURCE_DIR).mkdir(exist_ok=True)
    (tmp_path / ccb.SOURCE_DIR / name).write_text(source)
    return tmp_path


# ── the committed baseline, measured for real ──────────────────────────────


class TestRepoBaseline:
    def test_matches_ruff_on_this_tree(self) -> None:
        measured, _ = ccb.measure()
        baseline = ccb.parse_baseline((REPO_ROOT / ccb.BASELINE_REL).read_text())
        above, below = ccb.compare(baseline, measured)
        assert not above, "New over-limit function; split it:\n" + "\n".join(above)
        assert not below, (
            f"Baseline has paid-down functions; run {ccb.UPDATE_COMMAND}\n"
            + "\n".join(below)
        )

    def test_is_canonically_written(self) -> None:
        text = (REPO_ROOT / ccb.BASELINE_REL).read_text()
        assert text == ccb.render_baseline(ccb.parse_baseline(text))

    def test_target_version_is_requires_python(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        floor = project["project"]["requires-python"]
        assert floor == ">=3." + ccb.TARGET_VERSION.removeprefix("py3")

    def test_root_ruff_config_does_not_select_the_rules(self) -> None:
        # The baseline is the only suppression: a select would make every
        # pre-commit red, and per-file-ignores would be a second, unbounded one.
        lint = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"][
            "ruff"
        ]["lint"]
        selected = set(lint.get("select", [])) | set(lint.get("extend-select", []))
        assert not selected & {"C", "C9", "C90", "C901", "PL", "PLR", "PLR0915"}
        for codes in lint.get("per-file-ignores", {}).values():
            assert not set(codes) & set(ccb.RULES)


#: Every config-file route ruff has to switch a rule off, or raise a threshold.
#: Each is written into a throwaway tree; the check must still find both.
_SUPPRESSING_CONFIGS = {
    "ignore": '[tool.ruff.lint]\nignore = ["C901", "PLR0915"]\n',
    "per-file-ignores": (
        f'[tool.ruff.lint.per-file-ignores]\n"{_M}" = ["C901", "PLR0915"]\n'
    ),
    "extend-per-file-ignores": (
        f'[tool.ruff.lint.extend-per-file-ignores]\n"{_M}" = ["ALL"]\n'
    ),
    "thresholds": (
        "[tool.ruff.lint.mccabe]\nmax-complexity = 99\n"
        "[tool.ruff.lint.pylint]\nmax-statements = 999\n"
    ),
    "exclude": '[tool.ruff]\nexclude = ["application_sdk"]\n',
}


class TestBaselineIsTheOnlySuppression:
    """``Done when``: the baseline file is the only way a long function passes."""

    @pytest.mark.parametrize("setting", sorted(_SUPPRESSING_CONFIGS))
    def test_no_root_config_hides_a_finding(self, tmp_path: Path, setting: str) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUPPRESSING_CONFIGS[setting])
        _tree(tmp_path, ccb.CANARY)
        assert ccb.measure(tmp_path)[0] == {_M: _BOTH}

    def test_no_nested_config_hides_a_finding(self, tmp_path: Path) -> None:
        _tree(tmp_path, ccb.CANARY)
        (tmp_path / ccb.SOURCE_DIR / "ruff.toml").write_text(
            '[lint]\nignore = ["C901", "PLR0915"]\n'
        )
        assert ccb.measure(tmp_path)[0] == {_M: _BOTH}

    def test_noqa_comments_do_not_hide_a_finding(self, tmp_path: Path) -> None:
        source = ccb.CANARY.replace("def f(x):", "def f(x):  # noqa: C901, PLR0915")
        _tree(tmp_path, source)
        assert ccb.measure(tmp_path)[0] == {_M: _BOTH}

    def test_small_function_has_no_findings(self, tmp_path: Path) -> None:
        assert ccb.measure(_tree(tmp_path, _SMALL)) == ({}, [])


# ── naming the function ─────────────────────────────────────────────────────


class TestQualnames:
    def test_methods_and_nested_functions_are_dotted(self) -> None:
        source = (
            "class A:\n"
            "    @staticmethod\n"
            "    def m():\n"
            "        async def inner():\n"
            "            pass\n"
            "def top():\n"
            "    pass\n"
        )
        assert ccb.qualnames(source) == {3: "A.m", 4: "A.m.inner", 6: "top"}

    def test_a_real_method_is_named_by_class(self, tmp_path: Path) -> None:
        body = ccb.CANARY.replace("\n    ", "\n        ").replace(
            "def f(x):", "    def f(self, x):"
        )
        _tree(tmp_path, "class A:\n" + body)
        assert ccb.measure(tmp_path)[0] == {_M: {r: ["A.f"] for r in ccb.RULES}}


class TestParseReport:
    def _report(self, tmp_path: Path, row: int, code: str = "C901") -> str:
        return json.dumps(
            [
                {
                    "code": code,
                    "filename": str(tmp_path / _M),
                    "location": {"row": row, "column": 5},
                    "message": "m",
                },
                {
                    "code": "F401",
                    "filename": str(tmp_path / _M),
                    "location": {"row": 1, "column": 1},
                    "message": "other",
                },
            ]
        )

    def test_keeps_only_the_rules_and_names_the_function(self, tmp_path: Path) -> None:
        _tree(tmp_path, "import os\n\n\ndef g():\n    pass\n")
        found, violations = ccb.parse_report(self._report(tmp_path, 4), tmp_path)
        assert found == {_M: {"C901": ["g"]}}
        assert violations == [(_M, 4, "C901", "g", "m")]

    def test_finding_off_a_def_line_fails_closed(self, tmp_path: Path) -> None:
        _tree(tmp_path, "import os\n\n\ndef g():\n    pass\n")
        with pytest.raises(ccb.RuffRunError, match="not on a function"):
            ccb.parse_report(self._report(tmp_path, 5), tmp_path)

    def test_non_json_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ccb.RuffRunError):
            ccb.parse_report("not json", tmp_path)


# ── comparison ──────────────────────────────────────────────────────────────


class TestCompare:
    def test_new_function_is_above(self) -> None:
        assert ccb.compare(
            {"a.py": {"C901": ["f"]}}, {"a.py": {"C901": ["f", "g"]}}
        ) == (
            ["a.py C901 g"],
            [],
        )

    def test_paid_down_function_is_below(self) -> None:
        assert ccb.compare({"a.py": {"C901": ["f"]}}, {}) == ([], ["a.py C901 f"])

    def test_swapping_one_function_for_another_fails_both_ways(self) -> None:
        # A per-file count would net this to zero; names do not.
        assert ccb.compare({"a.py": {"C901": ["f"]}}, {"a.py": {"C901": ["g"]}}) == (
            ["a.py C901 g"],
            ["a.py C901 f"],
        )

    def test_one_rule_paying_down_does_not_buy_the_other(self) -> None:
        above, below = ccb.compare(
            {"a.py": {"C901": ["f"]}}, {"a.py": {"PLR0915": ["f"]}}
        )
        assert above == ["a.py PLR0915 f"] and below == ["a.py C901 f"]

    def test_duplicate_names_are_counted(self) -> None:
        # Two over-limit functions sharing a qualname (e.g. overloads).
        above, _ = ccb.compare(
            {"a.py": {"C901": ["f"]}}, {"a.py": {"C901": ["f", "f"]}}
        )
        assert above == ["a.py C901 f"]


class TestParseBaseline:
    @pytest.mark.parametrize(
        "text",
        [
            "[]",
            '{"a.py": {}}',
            '{"a.py": {"E501": ["f"]}}',
            '{"a.py": {"C901": []}}',
            '{"a.py": {"C901": "f"}}',
            '{"a.py": {"C901": [1]}}',
            '{"a.py": {"C901": 1}}',
        ],
    )
    def test_rejects_malformed(self, text: str) -> None:
        with pytest.raises(ValueError):
            ccb.parse_baseline(text)

    def test_round_trips_sorted(self) -> None:
        baseline = {
            "b.py": {"PLR0915": ["z", "a"], "C901": ["q"]},
            "a.py": {"C901": ["f"]},
        }
        rendered = ccb.render_baseline(baseline)
        assert list(json.loads(rendered)) == ["a.py", "b.py"]
        assert ccb.parse_baseline(rendered) == {
            "a.py": {"C901": ["f"]},
            "b.py": {"C901": ["q"], "PLR0915": ["a", "z"]},
        }


# ── the base revision ───────────────────────────────────────────────────────


class TestRaisedSinceBase:
    def test_unchanged_and_lowered_pass(self) -> None:
        base = {"a.py": {"C901": ["f", "g"]}}
        assert ccb.raised_since_base(base, base) == []
        assert ccb.raised_since_base(base, {"a.py": {"C901": ["f"]}}) == []

    def test_new_function_fails(self) -> None:
        assert ccb.raised_since_base({}, {"b.py": {"PLR0915": ["h"]}}) == [
            "b.py PLR0915 h"
        ]

    def test_a_module_split_carries_the_function(self) -> None:
        base = {"big.py": {"C901": ["A.run"], "PLR0915": ["A.run"]}}
        head = {"big/_runner.py": {"C901": ["A.run"], "PLR0915": ["A.run"]}}
        assert ccb.raised_since_base(base, head) == []

    def test_one_base_entry_carries_only_one_head_entry(self) -> None:
        base = {"a.py": {"C901": ["run"]}}
        head = {"a.py": {"C901": ["run"]}, "b.py": {"C901": ["run"]}}
        assert ccb.raised_since_base(base, head) == ["b.py C901 run"]

    def test_renamed_function_fails(self) -> None:
        assert ccb.raised_since_base(
            {"a.py": {"C901": ["f"]}}, {"a.py": {"C901": ["g"]}}
        ) == ["a.py C901 g"]


class TestReadBaseBaseline:
    def test_empty_sha_fails_closed(self) -> None:
        with pytest.raises(BaseRevisionError):
            ccb.read_base_baseline("", _Git({}))

    def test_missing_file_is_none(self) -> None:
        assert ccb.read_base_baseline("abc", _Git({"ls-tree": _git_result()})) is None

    def test_reads_the_base_file(self) -> None:
        base = {"a.py": {"C901": ["f"]}}
        assert ccb.read_base_baseline("abc", _base_has(base)) == base

    def test_unfetchable_base_fails_closed(self) -> None:
        git = _Git(
            {
                "cat-file": _git_result(returncode=1),
                "rev-parse": _git_result(stdout="true\n"),
                "fetch": _git_result(returncode=128, stderr="no such ref"),
            }
        )
        with pytest.raises(BaseRevisionError, match="could not fetch"):
            ccb.read_base_baseline("abc", git)

    def test_unreadable_listing_fails_closed(self) -> None:
        git = _Git({"ls-tree": _git_result(returncode=128, stderr="bad tree")})
        with pytest.raises(BaseRevisionError):
            ccb.read_base_baseline("abc", git)

    def test_malformed_base_baseline_fails_closed(self) -> None:
        git = _Git(
            {
                "ls-tree": _git_result(stdout="100644 blob x\tpath\n"),
                "show": _git_result(stdout="[]"),
            }
        )
        with pytest.raises(BaseRevisionError, match="not a baseline"):
            ccb.read_base_baseline("abc", git)


class TestCheckBase:
    def test_added_function_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        git = _base_has({"a.py": {"C901": ["f"]}})
        assert ccb.check_base("abc", {"a.py": {"C901": ["f", "g"]}}, git) is True
        assert "a.py C901 g" in capsys.readouterr().out

    def test_introducing_pr_is_held_to_the_base_tree(self) -> None:
        # No baseline at base: ruff's findings on the base tree stand in, so
        # the introducing PR cannot bake in a function it adds itself.
        git = _Git({"ls-tree": _git_result()})
        measured_trees: list[Path] = []

        def fake_measure(tree: Path) -> tuple[ccb.Baseline, list[ccb.Violation]]:
            measured_trees.append(tree)
            return {"a.py": {"C901": ["f"]}}, []

        assert ccb.check_base("abc", {"a.py": {"C901": ["f", "g"]}}, git, fake_measure)
        assert not ccb.check_base("abc", {"a.py": {"C901": ["f"]}}, git, fake_measure)
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
        assert ccb.check_base("abc", {"a.py": {"C901": ["f"]}}, git) is True


# ── the CLI ─────────────────────────────────────────────────────────────────


def _fake(found: ccb.Baseline) -> ccb.Measure:
    def measure(_tree: Path) -> tuple[ccb.Baseline, list[ccb.Violation]]:
        return found, [
            (f, 1, r, n, "m")
            for f, rules in found.items()
            for r, ns in rules.items()
            for n in ns
        ]

    return measure


class TestMain:
    def _baseline(self, tmp_path: Path, baseline: ccb.Baseline) -> Path:
        path = tmp_path / "b.json"
        path.write_text(ccb.render_baseline(baseline))
        return path

    def test_equal_passes(self, tmp_path: Path) -> None:
        found = {"a.py": {"C901": ["f"]}}
        assert ccb.main([], self._baseline(tmp_path, found), _fake(found)) == 0

    def test_above_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._baseline(tmp_path, {"a.py": {"C901": ["f"]}})
        assert ccb.main([], path, _fake({"a.py": {"C901": ["f", "g"]}})) == 1
        out = capsys.readouterr().out
        assert "a.py C901 g" in out and "a.py:1 g: m" in out

    def test_below_fails(self, tmp_path: Path) -> None:
        path = self._baseline(tmp_path, {"a.py": {"C901": ["f", "g"]}})
        assert ccb.main([], path, _fake({"a.py": {"C901": ["f"]}})) == 1

    def test_a_paid_down_tree_passes(self, tmp_path: Path) -> None:
        # Nothing measured, and the canary proves ruff still reports both
        # rules: the ratchet has reached zero, which must not fail.
        def fake(tree: Path) -> tuple[ccb.Baseline, list[ccb.Violation]]:
            if (tree / ccb.SOURCE_DIR / "canary.py").exists():
                return {f"{ccb.SOURCE_DIR}/canary.py": _BOTH}, []
            return {}, []

        assert ccb.main([], self._baseline(tmp_path, {}), fake) == 0

    def test_ruff_that_reports_nothing_fails(self, tmp_path: Path) -> None:
        assert ccb.main([], self._baseline(tmp_path, {}), _fake({})) == 2

    def test_canary_is_flagged_by_real_ruff(self) -> None:
        assert ccb.ruff_reports_both_rules()

    def test_update_locks_in_a_reduction(self, tmp_path: Path) -> None:
        path = self._baseline(tmp_path, {"a.py": {"C901": ["f", "g"]}})
        assert ccb.main(["--update"], path, _fake({"a.py": {"C901": ["f"]}})) == 0
        assert ccb.parse_baseline(path.read_text()) == {"a.py": {"C901": ["f"]}}

    def test_update_refuses_to_add_a_function(self, tmp_path: Path) -> None:
        before = {"a.py": {"C901": ["f"]}}
        path = self._baseline(tmp_path, before)
        grown = _fake({"a.py": {"C901": ["f", "g"]}})
        assert ccb.main(["--update"], path, grown) == 1
        assert ccb.parse_baseline(path.read_text()) == before
        assert ccb.main(["--update", "--allow-increase"], path, grown) == 0
        assert ccb.parse_baseline(path.read_text()) == {"a.py": {"C901": ["f", "g"]}}

    def test_base_sha_add_fails_even_when_the_baseline_matches(
        self, tmp_path: Path
    ) -> None:
        found = {"a.py": {"C901": ["f", "g"]}}
        git = _base_has({"a.py": {"C901": ["f"]}})
        path = self._baseline(tmp_path, found)
        assert ccb.main(["--base-sha", "abc"], path, _fake(found), git) == 1


# ── wiring ──────────────────────────────────────────────────────────────────


class TestWiring:
    def test_pre_commit_runs_the_check(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
        hooks = [h for repo in config["repos"] for h in repo["hooks"]]
        (hook,) = [h for h in hooks if h["id"] == "complexity-baseline"]
        assert "check_complexity_baseline.py" in hook["entry"]
        assert hook["pass_filenames"] is False
        files = re.compile(hook["files"])
        for path in (
            "application_sdk/app/base.py",
            ccb.BASELINE_REL,
            ".github/scripts/check_complexity_baseline.py",
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
            "check_complexity_baseline.py" in run and "--base-sha" in run
            for run in runs
        )
