"""The pyright baseline check on .github/scripts (FND-4245).

pyright is stubbed for the comparison logic, and run for real once against a
throwaway repo, so a change in how pyright reads the generated config (the
reason it once analysed zero files) fails here rather than as a check that
silently passes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_pyright_baseline as cpb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

_PYPROJECT = """\
[project]
name = "x"
requires-python = ">=3.11"

[tool.pyright]
typeCheckingMode = "standard"
exclude = [".github/**"]
reportArgumentType = "warning"
"""


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    return tmp_path


def _diag(file: Path, severity: str, rule: str | None) -> dict[str, object]:
    d: dict[str, object] = {"file": str(file), "severity": severity, "message": "m"}
    if rule is not None:
        d["rule"] = rule
    return d


def _report(diags: list[dict[str, object]], files_analysed: int = 3) -> str:
    return json.dumps(
        {"generalDiagnostics": diags, "summary": {"filesAnalyzed": files_analysed}}
    )


class _Stub:
    """Stand-in for pyright that records the config it was handed."""

    def __init__(self, stdout: str, returncode: int = 1) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.config: dict[str, object] | None = None
        self.config_path: Path | None = None

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.config_path = Path(cmd[cmd.index("--project") + 1])
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(list(cmd), self.returncode, self.stdout, "")


class TestPins:
    def test_dev_pin_equals_the_pre_commit_hook_rev(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
        (hook,) = [
            r
            for r in config["repos"]
            if r["repo"] == "https://github.com/RobertCraigie/pyright-python"
        ]
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        pins = [
            d for d in pyproject["dependency-groups"]["dev"] if d.startswith("pyright")
        ]
        assert pins == [f"pyright=={hook['rev'].removeprefix('v')}"]

    def test_workflow_job_name_is_the_required_context(self) -> None:
        # The job name is the status-check context the `main` ruleset requires;
        # renaming it would leave the ruleset waiting on a check that never runs.
        wf = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/scripts-tests.yaml").read_text()
        )
        job = wf["jobs"]["pyright"]
        assert job["name"] == "pyright .github/scripts"
        runs = [s.get("run", "") for s in job["steps"]]
        assert any(".github/scripts/check_pyright_baseline.py" in r for r in runs)
        assert not any("--update" in r for r in runs)

    def test_committed_baseline_is_in_canonical_form(self) -> None:
        # Hand edits that desync totals from the per-file counts show up here.
        text = cpb.BASELINE.read_text(encoding="utf-8")
        assert cpb.render_baseline(cpb.load_baseline(cpb.BASELINE)) == text


class TestBuildConfig:
    def test_scopes_to_scripts_and_keeps_repo_rules(self, tmp_path: Path) -> None:
        config = cpb.build_config(_repo(tmp_path) / "pyproject.toml")
        assert config["include"] == [".github/scripts"]
        assert config["exclude"] == ["**/__pycache__"]
        assert config["typeCheckingMode"] == "standard"
        assert config["reportArgumentType"] == "warning"
        assert config["pythonPlatform"] == "Linux"
        assert config["pythonVersion"] == "3.11"

    @pytest.mark.parametrize(
        ("spec", "floor"),
        [(">=3.11", "3.11"), (">= 3.12.1", "3.12"), (">=3.11,<4", "3.11")],
    )
    def test_requires_python_floor(self, spec: str, floor: str) -> None:
        assert cpb.requires_python_floor(spec) == floor

    @pytest.mark.parametrize("spec", ["~=3.11", "==3.12.*", ">3.11"])
    def test_unpinnable_requires_python_is_an_error(self, spec: str) -> None:
        with pytest.raises(cpb.PyrightRunError):
            cpb.requires_python_floor(spec)


class TestParseReport:
    def test_counts_per_file_and_rule(self, tmp_path: Path) -> None:
        a = tmp_path / ".github/scripts/a.py"
        stdout = _report(
            [
                _diag(a, "error", "reportOptionalMemberAccess"),
                _diag(a, "error", "reportOptionalMemberAccess"),
                _diag(a, "warning", "reportArgumentType"),
                _diag(a, "error", None),
                _diag(a, "information", "reportPossiblyUnbound"),
            ],
            files_analysed=7,
        )
        counts, analysed = cpb.parse_report(stdout, tmp_path)
        assert analysed == 7
        assert counts == {
            ".github/scripts/a.py": {
                "error:reportOptionalMemberAccess": 2,
                "error:unruled": 1,
                "warning:reportArgumentType": 1,
            }
        }

    def test_non_json_output_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(cpb.PyrightRunError):
            cpb.parse_report("Installing pyright...", tmp_path)


class TestCompare:
    def test_equal_is_clean(self) -> None:
        counts = {"a.py": {"error:x": 2}}
        assert cpb.compare(counts, counts) == ([], [])

    def test_new_finding_in_a_baselined_file_regresses(self) -> None:
        regressions, stale = cpb.compare(
            {"a.py": {"error:x": 2}}, {"a.py": {"error:x": 3}}
        )
        assert regressions == ["a.py: error:x 2 -> 3"] and stale == []

    def test_new_file_with_findings_regresses(self) -> None:
        regressions, _ = cpb.compare({}, {"new.py": {"warning:y": 1}})
        assert regressions == ["new.py: warning:y 0 -> 1"]

    def test_fixing_one_rule_does_not_pay_for_another(self) -> None:
        regressions, stale = cpb.compare(
            {"a.py": {"error:x": 1}}, {"a.py": {"error:y": 1}}
        )
        assert regressions == ["a.py: error:y 0 -> 1"]
        assert stale == ["a.py: error:x 1 -> 0"]

    def test_fixed_finding_is_stale_until_baseline_lowered(self) -> None:
        regressions, stale = cpb.compare({"gone.py": {"error:x": 1}}, {})
        assert regressions == [] and stale == ["gone.py: error:x 1 -> 0"]


class TestMain:
    def _run(
        self,
        repo: Path,
        stub: _Stub,
        baseline: dict[str, dict[str, int]] | None,
        argv: Sequence[str] = (),
    ) -> tuple[int, Path]:
        path = repo / ".github/scripts/pyright_baseline.json"
        if baseline is not None:
            path.write_text(cpb.render_baseline(baseline), encoding="utf-8")
        rc = cpb.main(list(argv), runner=stub, repo_root=repo, baseline_path=path)
        return rc, path

    def test_matching_baseline_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        a = repo / ".github/scripts/a.py"
        stub = _Stub(_report([_diag(a, "error", "x")]))
        rc, _ = self._run(repo, stub, {".github/scripts/a.py": {"error:x": 1}})
        assert rc == 0
        # The config lived at the repo root (pyright ignores an absolute include)
        # and is gone afterwards.
        assert stub.config_path is not None and stub.config_path.parent == repo
        assert not stub.config_path.exists()
        assert stub.config is not None and stub.config["include"] == [".github/scripts"]

    def test_regression_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo(tmp_path)
        a = repo / ".github/scripts/a.py"
        stub = _Stub(_report([_diag(a, "error", "x"), _diag(a, "error", "x")]))
        rc, _ = self._run(repo, stub, {".github/scripts/a.py": {"error:x": 1}})
        assert rc == 1
        assert "rose above the baseline" in capsys.readouterr().out

    def test_stale_baseline_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo(tmp_path)
        rc, _ = self._run(
            repo, _Stub(_report([]), 0), {".github/scripts/a.py": {"error:x": 1}}
        )
        assert rc == 1
        assert "--update" in capsys.readouterr().out

    def test_zero_files_analysed_fails_closed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        rc, _ = self._run(repo, _Stub(_report([], files_analysed=0), 0), {})
        assert rc == 2

    def test_fatal_pyright_exit_fails(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        rc, _ = self._run(repo, _Stub("", returncode=3), {})
        assert rc == 2

    def test_update_writes_canonical_baseline(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        a = repo / ".github/scripts/a.py"
        stub = _Stub(_report([_diag(a, "warning", "y")]))
        rc, path = self._run(repo, stub, None, ["--update"])
        assert rc == 0
        doc = json.loads(path.read_text())
        assert doc["totals"] == {"error": 0, "warning": 1}
        assert doc["files"] == {".github/scripts/a.py": {"warning:y": 1}}

    def test_summary_written_when_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        self._run(repo, _Stub(_report([]), 0), {})
        assert re.search(r"\| error \| 0 \| 0 \|", summary.read_text())


@pytest.mark.skipif(shutil.which("pyright") is None, reason="pyright not installed")
def test_real_pyright_sees_excluded_scripts(tmp_path: Path) -> None:
    """Red-green against real pyright: the repo-level exclude of .github/** must
    not hide the scripts, and a real finding must be counted."""
    repo = _repo(tmp_path)
    (repo / ".github/scripts/bad.py").write_text(
        "import re\n\nre.match('a', 'b').group(0)\n", encoding="utf-8"
    )
    counts = cpb.run_pyright(repo, runner=_real_runner(repo))
    assert counts == {".github/scripts/bad.py": {"error:reportOptionalMemberAccess": 1}}


def _real_runner(cwd: Path) -> cpb.Runner:
    def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, cwd=cwd, timeout=300, check=False
        )

    return run
