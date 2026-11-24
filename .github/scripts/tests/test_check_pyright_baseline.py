"""The pyright baseline check on .github/scripts (FND-4245) and the SDK tree (FND-4295).

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
import tomllib
from pathlib import Path
from typing import Sequence

import check_pyright_baseline as cpb
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

_PYPROJECT = """\
[project]
name = "x"
requires-python = ">=3.11"

[dependency-groups]
dev = ["pyright==1.2.3"]

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


def _pre_commit_hook(hook_id: str) -> dict[str, object]:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    (hook,) = [h for r in config["repos"] for h in r["hooks"] if h["id"] == hook_id]
    return hook


class TestPins:
    def test_no_pyright_runs_outside_the_baseline_check(self) -> None:
        # A plain pyright hook fails on the baselined errors, and a second
        # pyright install could drift from the dev pin the baselines record.
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
        repos = [r["repo"] for r in config["repos"]]
        assert not [r for r in repos if "pyright" in r.lower()]
        pyright_hooks = [
            h
            for r in config["repos"]
            for h in r["hooks"]
            if "pyright" in str(h.get("entry", "")) or h["id"] == "pyright"
        ]
        assert {h["id"] for h in pyright_hooks} == {
            "pyright",
            "pyright-scripts-baseline",
        }
        for hook in pyright_hooks:
            assert "check_pyright_baseline.py" in str(hook["entry"])
            assert "--update" not in str(hook["entry"])

    def test_sdk_hook_runs_the_sdk_scope_in_the_ci_venv(self) -> None:
        hook = _pre_commit_hook("pyright")
        entry = str(hook["entry"])
        assert entry.startswith("uv run --frozen --all-extras --all-groups ")
        assert entry.endswith("check_pyright_baseline.py --scope sdk")
        assert hook["pass_filenames"] is False
        files = re.compile(str(hook["files"]))
        for path in ("application_sdk/main.py", "pyright_baseline.json", "uv.lock"):
            assert files.search(path), path

    def test_pre_commit_job_passes_the_base_sha(self) -> None:
        # Without it the SDK hook cannot tell a same-PR baseline raise.
        wf = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yaml").read_text())
        (step,) = [
            s
            for s in wf["jobs"]["lint"]["steps"]
            if str(s.get("uses", "")).startswith("pre-commit/action@")
        ]
        assert step["env"][cpb.BASE_SHA_ENV] == (
            "${{ github.event.pull_request.base.sha || "
            "github.event.merge_group.base_sha }}"
        )

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

    @pytest.mark.parametrize("scope", sorted(cpb.SCOPES))
    def test_committed_baseline_is_in_canonical_form(self, scope: str) -> None:
        # Hand edits that desync totals from the per-file counts show up here.
        sc = cpb.SCOPES[scope]
        path = REPO_ROOT / sc.baseline_rel
        text = path.read_text(encoding="utf-8")
        baseline = cpb.load_baseline(path)
        assert baseline.pyright_version is not None
        assert cpb.render_baseline(baseline.files, baseline.pyright_version, sc) == text

    @pytest.mark.parametrize("scope", sorted(cpb.SCOPES))
    def test_committed_baseline_records_the_dev_pin(self, scope: str) -> None:
        pin = cpb.pyright_pin(REPO_ROOT / "pyproject.toml")
        path = REPO_ROOT / cpb.SCOPES[scope].baseline_rel
        assert cpb.load_baseline(path).pyright_version == pin

    def test_sdk_baseline_tracks_errors_only(self) -> None:
        baseline = cpb.load_baseline(REPO_ROOT / cpb.SCOPES["sdk"].baseline_rel)
        keys = {k for rules in baseline.files.values() for k in rules}
        assert keys and all(k.startswith("error:") for k in keys)
        assert not any(f.startswith(".github/") for f in baseline.files)

    def test_bug_catching_rules_are_errors_and_type_ignore_is_off(self) -> None:
        # FND-4295: these were warnings, so an argument-type or attribute bug in
        # new code passed CI; and `# type: ignore` silenced every rule on its
        # line whatever its brackets said.
        settings = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        pyright = settings["tool"]["pyright"]
        for rule in (
            "reportArgumentType",
            "reportAttributeAccessIssue",
            "reportFunctionMemberAccess",
            "reportUnnecessaryTypeIgnoreComment",
        ):
            assert pyright[rule] == "error", rule
        assert pyright["enableTypeIgnoreComments"] is False

    def test_workflow_passes_the_pr_base_sha(self) -> None:
        wf = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/scripts-tests.yaml").read_text()
        )
        (step,) = [
            s
            for s in wf["jobs"]["pyright"]["steps"]
            if "check_pyright_baseline.py" in s.get("run", "")
        ]
        assert '--base-sha "$BASE_SHA"' in step["run"]
        assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"


class TestBuildConfig:
    def test_sdk_scope_keeps_include_and_exclude(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        config = cpb.build_config(repo / "pyproject.toml", repo, cpb.SCOPES["sdk"])
        assert config["exclude"] == [".github/**"]
        assert "include" not in config
        assert config["pythonPlatform"] == "Linux"
        assert config["pythonVersion"] == "3.11"

    def test_scopes_to_scripts_and_keeps_repo_rules(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        config = cpb.build_config(repo / "pyproject.toml", repo)
        assert config["include"] == ["."]
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

    def test_sdk_severities_drop_warnings(self, tmp_path: Path) -> None:
        a = tmp_path / "application_sdk/a.py"
        stdout = _report(
            [_diag(a, "error", "reportArgumentType"), _diag(a, "warning", "w")]
        )
        counts, _ = cpb.parse_report(stdout, tmp_path, cpb.SCOPES["sdk"].severities)
        assert counts == {"application_sdk/a.py": {"error:reportArgumentType": 1}}

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
            path.write_text(cpb.render_baseline(baseline, "1.2.3"), encoding="utf-8")
        rc = cpb.main(list(argv), runner=stub, repo_root=repo, baseline_path=path)
        return rc, path

    def test_matching_baseline_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        a = repo / ".github/scripts/a.py"
        stub = _Stub(_report([_diag(a, "error", "x")]))
        rc, _ = self._run(repo, stub, {".github/scripts/a.py": {"error:x": 1}})
        assert rc == 0
        # The config lived inside .github/scripts (pyright auto-excludes dot
        # directories below a config's root) and is gone afterwards.
        assert stub.config_path is not None
        assert stub.config_path.parent == repo / ".github/scripts"
        assert not stub.config_path.exists()
        assert stub.config is not None and stub.config["include"] == ["."]

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
        assert doc["pyright_version"] == "1.2.3"
        assert doc["files"] == {".github/scripts/a.py": {"warning:y": 1}}

    def test_sdk_scope_writes_config_at_root_and_root_baseline(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        a = repo / "pkg/a.py"
        stub = _Stub(_report([_diag(a, "error", "x"), _diag(a, "warning", "w")]))
        rc = cpb.main(["--scope", "sdk", "--update"], runner=stub, repo_root=repo)
        assert rc == 0
        assert stub.config_path is not None and stub.config_path.parent == repo
        assert not stub.config_path.exists()
        doc = json.loads((repo / "pyright_baseline.json").read_text())
        assert doc["totals"] == {"error": 1}
        assert doc["files"] == {"pkg/a.py": {"error:x": 1}}
        assert "--scope sdk --update" in doc["_comment"]

    def test_summary_written_when_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        self._run(repo, _Stub(_report([]), 0), {})
        assert re.search(r"\| error \| 0 \| 0 \|", summary.read_text())


class _TreeStub:
    """Stand-in for pyright that answers per analysed tree: ``head`` findings
    (``{relative path: count}``, rule ``x``) when the config sits in ``repo``,
    ``base`` findings for any other tree (the base revision's checkout)."""

    def __init__(self, repo: Path, head: dict[str, int], base: dict[str, int]) -> None:
        self.repo, self.head, self.base = repo, head, base
        self.configs: list[tuple[Path, dict[str, object]]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        config_path = Path(cmd[cmd.index("--project") + 1])
        self.configs.append(
            (config_path, json.loads(config_path.read_text(encoding="utf-8")))
        )
        tree = config_path.parent
        findings = self.head if tree == self.repo else self.base
        diags = [
            _diag(tree / rel, "error", "x")
            for rel, n in findings.items()
            for _ in range(n)
        ]
        return subprocess.CompletedProcess(list(cmd), 1, _report(diags), "")


def _git_in(cwd: Path) -> cpb.Runner:
    def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *cmd], capture_output=True, text=True, cwd=cwd, check=False
        )

    return run


class TestBaseRevision:
    """``--base-sha``: the PR's baseline may not rise above the base's.

    Real git against a throwaway repo that is its own ``origin``, so the fetch,
    the existence probe and rename detection all run for real.
    """

    BASELINE_REL = ".github/scripts/pyright_baseline.json"

    def _init(self, tmp_path: Path) -> tuple[Path, cpb.Runner]:
        repo = _repo(tmp_path)
        git = _git_in(repo)
        for cmd in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
            ["config", "uploadpack.allowAnySHA1InWant", "true"],
            ["remote", "add", "origin", str(repo)],
        ):
            assert git(cmd).returncode == 0
        return repo, git

    def _commit(
        self,
        repo: Path,
        git: cpb.Runner,
        baseline: dict[str, dict[str, int]] | None,
        version: str = "1.2.3",
    ) -> str:
        path = repo / self.BASELINE_REL
        if baseline is not None:
            path.write_text(cpb.render_baseline(baseline, version), encoding="utf-8")
        assert git(["add", "-A"]).returncode == 0
        assert git(["commit", "-q", "--allow-empty", "-m", "c"]).returncode == 0
        return git(["rev-parse", "HEAD"]).stdout.strip()

    def _main(
        self,
        repo: Path,
        git: cpb.Runner,
        head: dict[str, dict[str, int]],
        base_sha: str,
    ) -> int:
        a = repo / ".github/scripts/a.py"
        diags = [
            _diag(Path(str(a).replace("a.py", f)), key.split(":")[0], key.split(":")[1])
            for f_path, rules in head.items()
            for key, n in rules.items()
            for f in [Path(f_path).name]
            for _ in range(n)
        ]
        return cpb.main(
            ["--base-sha", base_sha],
            runner=_Stub(_report(diags)),
            repo_root=repo,
            baseline_path=repo / self.BASELINE_REL,
            git=git,
        )

    def _introduce_sdk_baseline(
        self, tmp_path: Path, base_findings: int, head_findings: int
    ) -> tuple[int, _TreeStub, Path]:
        """A PR that adds the sdk baseline, holding ``head_findings`` of rule
        ``x`` in pkg/a.py, on a base whose tree really has ``base_findings``."""
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, None)
        sdk = cpb.SCOPES["sdk"]
        head = {"pkg/a.py": {"error:x": head_findings}} if head_findings else {}
        (repo / sdk.baseline_rel).write_text(
            cpb.render_baseline(head, "1.2.3", sdk), encoding="utf-8"
        )
        self._commit(repo, git, None)
        stub = _TreeStub(repo, {"pkg/a.py": head_findings}, {"pkg/a.py": base_findings})
        rc = cpb.main(
            ["--scope", "sdk", "--base-sha", base], runner=stub, repo_root=repo, git=git
        )
        return rc, stub, repo

    def test_introducing_pr_matching_the_base_tree_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, stub, repo = self._introduce_sdk_baseline(tmp_path, 1, 1)
        assert rc == 0
        assert "this PR introduces it" in capsys.readouterr().out
        # The base tree was a real checkout of the base, and it is gone again.
        (base_config,) = [c for c in stub.configs if c[0].parent != repo]
        assert base_config[0].parent.name == "base"
        assert not base_config[0].parent.exists()

    def test_introducing_pr_cannot_grandfather_its_own_finding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # lens F-3a899a: with no base baseline, a PR could add a finding and
        # bake it into the baseline it introduces. The base tree has none.
        rc, _, _ = self._introduce_sdk_baseline(tmp_path, 0, 1)
        assert rc == 1
        assert "pkg/a.py: x 0 -> 1" in capsys.readouterr().out

    def test_base_tree_reads_its_comments_by_the_base_config(
        self, tmp_path: Path
    ) -> None:
        # The base's `# type: ignore` comments were written for the base's
        # settings: those two follow the base, every rule level the head.
        _, stub, repo = self._introduce_sdk_baseline(tmp_path, 1, 1)
        (head_cfg,) = [c for p, c in stub.configs if p.parent == repo]
        (base_cfg,) = [c for p, c in stub.configs if p.parent != repo]
        for key in ("enableTypeIgnoreComments", "reportUnnecessaryTypeIgnoreComment"):
            assert key not in base_cfg
        assert base_cfg["reportArgumentType"] == head_cfg["reportArgumentType"]

    def test_raising_the_baseline_with_the_finding_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bug lens found: head baseline == head findings, both raised.
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {".github/scripts/a.py": {"error:x": 1}})
        head = {".github/scripts/a.py": {"error:x": 2}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base) == 1
        out = capsys.readouterr().out
        assert "rose above the base revision" in out
        assert ".github/scripts/a.py: x 1 -> 2" in out

    def test_promoting_a_rule_to_error_is_not_a_new_finding(
        self, tmp_path: Path
    ) -> None:
        # FND-4295 moved reportArgumentType from warning to error: the same
        # findings change key, and the base comparison must not read that as new.
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {".github/scripts/a.py": {"warning:x": 2}})
        head = {".github/scripts/a.py": {"error:x": 2}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base) == 0

    def _sdk_promotion(self, tmp_path: Path, base_level_line: str, rule: str) -> int:
        """Base: ``rule`` untracked by the errors-only sdk baseline. Head: it
        is an error, and its two pre-existing findings enter the baseline."""
        repo, git = self._init(tmp_path)
        pyproject = repo / "pyproject.toml"
        sdk = cpb.SCOPES["sdk"]
        baseline = repo / sdk.baseline_rel
        pyproject.write_text(_PYPROJECT + base_level_line, encoding="utf-8")
        baseline.write_text(cpb.render_baseline({}, "1.2.3", sdk), encoding="utf-8")
        base = self._commit(repo, git, None)
        pyproject.write_text(_PYPROJECT + f'{rule} = "error"\n', encoding="utf-8")
        head = {"pkg/a.py": {f"error:{rule}": 2}}
        baseline.write_text(cpb.render_baseline(head, "1.2.3", sdk), encoding="utf-8")
        self._commit(repo, git, None)
        return cpb.main(
            ["--scope", "sdk", "--base-sha", base],
            runner=_Stub(_report([_diag(repo / "pkg/a.py", "error", rule)] * 2)),
            repo_root=repo,
            git=git,
        )

    def test_sdk_promotion_from_an_explicit_warning_is_not_a_new_finding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # F-61b341: the sdk baseline drops warnings, so the base has no entry
        # for a rule promoted from warning; its findings are new keys only.
        rc = self._sdk_promotion(
            tmp_path, 'reportUnknownMemberType = "warning"\n', "reportUnknownMemberType"
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "::warning::this PR promotes reportUnknownMemberType" in out
        assert "pkg/a.py: reportUnknownMemberType 0 -> 2" in out

    def test_sdk_rule_at_pyright_default_on_base_stays_strict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Unset on the base, the rule may already have been an error there
        # (reportCallIssue is, in standard mode): not a promotion.
        rc = self._sdk_promotion(tmp_path, "", "reportCallIssue")
        assert rc == 1
        assert "rose above the base revision" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("base", "head", "expected"),
        [
            ({"reportX": "warning"}, {"reportX": "error"}, {"reportX"}),
            ({"reportX": False}, {"reportX": True}, {"reportX"}),
            ({}, {"reportX": "error"}, set()),
            ({"reportX": "error"}, {"reportX": "error"}, set()),
            ({"reportX": "warning"}, {"reportX": "warning"}, set()),
            ({"typeCheckingMode": "basic"}, {"typeCheckingMode": "error"}, set()),
        ],
    )
    def test_promoted_rules(
        self,
        base: dict[str, object],
        head: dict[str, object],
        expected: set[str],
    ) -> None:
        assert cpb.promoted_rules(base, head, ("error",)) == expected

    def test_unreadable_base_pyproject_fails_closed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {".github/scripts/a.py": {"error:x": 1}})
        head = {".github/scripts/a.py": {"error:x": 1}}
        self._commit(repo, git, head)

        def no_pyproject(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
            if list(cmd) == ["show", f"{base}:pyproject.toml"]:
                return subprocess.CompletedProcess(list(cmd), 128, "", "fatal")
            return git(cmd)

        assert self._main(repo, no_pyproject, head, base) == 1
        assert "could not read pyproject.toml" in capsys.readouterr().out

    def _main_env(self, repo: Path, git: cpb.Runner, n: int) -> int:
        a = repo / ".github/scripts/a.py"
        return cpb.main(
            [],
            runner=_Stub(_report([_diag(a, "error", "x")] * n)),
            repo_root=repo,
            baseline_path=repo / self.BASELINE_REL,
            git=git,
        )

    def test_base_sha_from_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The pre-commit hook cannot take --base-sha; CI passes it by env.
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {".github/scripts/a.py": {"error:x": 1}})
        self._commit(repo, git, {".github/scripts/a.py": {"error:x": 2}})
        monkeypatch.setenv(cpb.BASE_SHA_ENV, base)
        assert self._main_env(repo, git, 2) == 1
        assert "rose above the base revision" in capsys.readouterr().out

    def test_empty_base_sha_env_means_no_base_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A push to main has no base: the workflow expression renders empty.
        repo, git = self._init(tmp_path)
        self._commit(repo, git, {".github/scripts/a.py": {"error:x": 1}})
        monkeypatch.setenv(cpb.BASE_SHA_ENV, "")
        assert self._main_env(repo, git, 1) == 0

    def test_shrinking_the_baseline_passes(self, tmp_path: Path) -> None:
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {".github/scripts/a.py": {"error:x": 2}})
        head = {".github/scripts/a.py": {"error:x": 1}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base) == 0

    def test_pyright_version_change_may_raise_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo, git = self._init(tmp_path)
        base = self._commit(
            repo, git, {".github/scripts/a.py": {"error:x": 1}}, version="1.0.0"
        )
        head = {".github/scripts/a.py": {"error:x": 2}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base) == 0
        assert "::warning::pyright changed 1.0.0 -> 1.2.3" in capsys.readouterr().out

    def test_rename_carries_counts_to_the_new_path(self, tmp_path: Path) -> None:
        repo, git = self._init(tmp_path)
        script = repo / ".github/scripts/a.py"
        script.write_text("x = 1\n" * 20, encoding="utf-8")
        base = self._commit(repo, git, {".github/scripts/a.py": {"error:x": 1}})
        assert (
            git(["mv", ".github/scripts/a.py", ".github/scripts/b.py"]).returncode == 0
        )
        head = {".github/scripts/b.py": {"error:x": 1}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base) == 0

    def test_new_file_cannot_add_findings_via_the_baseline(
        self, tmp_path: Path
    ) -> None:
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {})
        (repo / ".github/scripts/b.py").write_text("y = 2\n", encoding="utf-8")
        head = {".github/scripts/b.py": {"error:x": 1}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base) == 1

    @pytest.mark.parametrize("base_sha", ["", "0" * 40])
    def test_unreadable_base_fails_closed(self, tmp_path: Path, base_sha: str) -> None:
        repo, git = self._init(tmp_path)
        head = {".github/scripts/a.py": {"error:x": 1}}
        self._commit(repo, git, head)
        assert self._main(repo, git, head, base_sha) == 1

    def test_unreadable_base_tree_is_not_an_absent_baseline(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The commit object exists but its tree cannot be listed (a partial or
        # corrupt fetch). That must fail, not read as "this PR introduces it",
        # which would skip the base comparison entirely.
        repo, git = self._init(tmp_path)
        base = self._commit(repo, git, {".github/scripts/a.py": {"error:x": 1}})
        head = {".github/scripts/a.py": {"error:x": 2}}
        self._commit(repo, git, head)

        def broken_tree(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "ls-tree":
                return subprocess.CompletedProcess(
                    list(cmd), 128, "", "fatal: not a tree object"
                )
            return git(cmd)

        assert self._main(repo, broken_tree, head, base) == 1
        out = capsys.readouterr().out
        assert "could not list" in out
        assert "this PR introduces it" not in out

    def test_baseline_from_another_pyright_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo, git = self._init(tmp_path)
        head = {".github/scripts/a.py": {"error:x": 1}}
        base = self._commit(repo, git, head, version="9.9.9")
        assert self._main(repo, git, head, base) == 1
        assert "but the dev pin is 1.2.3" in capsys.readouterr().out


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


_SDK_PYPROJECT = """\
[project]
name = "x"
requires-python = ">=3.11"

[dependency-groups]
dev = ["pyright==1.2.3"]

[tool.pyright]
typeCheckingMode = "standard"
exclude = [".github/**"]
enableTypeIgnoreComments = false
reportUnnecessaryTypeIgnoreComment = "error"
reportArgumentType = "error"
"""


@pytest.mark.skipif(shutil.which("pyright") is None, reason="pyright not installed")
def test_real_pyright_fails_a_new_argument_type_error(tmp_path: Path) -> None:
    """Red-green against real pyright (FND-4295's done-when): with an empty sdk
    baseline a new argument-type error fails, a ``# type: ignore`` does not
    hide it, and a ``# pyright: ignore`` that suppresses nothing is itself an
    error."""
    (tmp_path / "pyproject.toml").write_text(_SDK_PYPROJECT, encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    code = tmp_path / "pkg/a.py"
    runner = _real_runner(tmp_path)
    sdk = cpb.SCOPES["sdk"]
    (tmp_path / sdk.baseline_rel).write_text(
        cpb.render_baseline({}, "1.2.3", sdk), encoding="utf-8"
    )

    def check() -> int:
        return cpb.main(["--scope", "sdk"], runner=runner, repo_root=tmp_path)

    header = "def f(x: int) -> None: ...\n\n"
    code.write_text(header + "f(1)\n", encoding="utf-8")
    assert check() == 0

    code.write_text(header + "f('s')  # type: ignore[arg-type]\n", encoding="utf-8")
    assert check() == 1
    assert cpb.run_pyright(tmp_path, runner, sdk) == {
        "pkg/a.py": {"error:reportArgumentType": 1}
    }

    code.write_text(
        header + "f(1)  # pyright: ignore[reportArgumentType]\n", encoding="utf-8"
    )
    assert cpb.run_pyright(tmp_path, runner, sdk) == {
        "pkg/a.py": {"error:reportUnnecessaryTypeIgnoreComment": 1}
    }


def _real_runner(cwd: Path) -> cpb.Runner:
    def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, cwd=cwd, timeout=300, check=False
        )

    return run
