import json
from pathlib import Path

from conformance.suite.runner import main


def test_static_run_explicitly_reports_behavior_not_evaluated(tmp_path: Path):
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--repo",
                str(tmp_path),
                "--rule",
                "F016",
                "--static",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert (
        report["runs"][0]["properties"]["atlan/preflightTests"]["F016"]["execution"]
        == "not_evaluated"
    )


def test_full_run_reports_missing_behavior_scenarios(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-connector"\nversion="1.0.0"\n'
    )
    (tmp_path / "tests").mkdir()
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--repo",
                str(tmp_path),
                "--rule",
                "F016",
                "--with-tests",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text())
    assert report["runs"][0]["results"]
    assert not report["runs"][0]["properties"]["atlan/preflightTests"]["F016"][
        "complete"
    ]


def test_new_static_rule_reaches_sarif(tmp_path: Path):
    (tmp_path / "handler.py").write_text(
        "from application_sdk.handler import Handler\nclass H(Handler):\n    async def preflight_check(self, input):\n        return True\n"
    )
    output = tmp_path / "report.json"
    main(["--repo", str(tmp_path), "--rule", "F006", "--output", str(output)])
    assert {
        row["ruleId"] for row in json.loads(output.read_text())["runs"][0]["results"]
    } == {"F006"}


def test_unresolved_analysis_is_machine_identifiable(tmp_path):
    source = tmp_path / "handler.py"
    source.write_text(
        'from application_sdk.handler import PreflightCheck\ndef check(ok):\n return PreflightCheck(name="probe", passed=ok)\n'
    )
    output = tmp_path / "report.sarif"
    main(["--repo", str(tmp_path), "--rule", "F003,F019", "--output", str(output)])
    results = json.loads(output.read_text())["runs"][0]["results"]
    assert results[0]["ruleId"] == "F019"
    assert results[0]["properties"]["atlan/analysisStatus"] == "unresolved"


def test_preflight_selection_does_not_run_unrelated_p_checks(tmp_path, monkeypatch):
    from conformance.suite import runner

    def unexpected(root):
        raise AssertionError("Unrelated P-series discovery was invoked")

    trap = runner.CheckRegistration(
        series="P", discover=unexpected, scan_path=lambda path, root: []
    )
    monkeypatch.setattr(runner, "_CHECKS", [*runner._CHECKS, trap])
    runner.main(
        [
            "--repo",
            str(tmp_path),
            "--rule",
            "F003,F019",
            "--output",
            str(tmp_path / "report.sarif"),
        ]
    )


def test_typed_failure_errors_preserve_soft_exit_mode(tmp_path):
    (tmp_path / "handler.py").write_text(
        'from application_sdk.handler import PreflightCheck\ndef check():\n return PreflightCheck(name="probe", passed=False)\n'
    )
    output = tmp_path / "report.sarif"
    args = ["--repo", str(tmp_path), "--rule", "F003", "--output", str(output)]
    assert main(args) == 1
    assert main([*args, "--exit-zero"]) == 0
    run = json.loads(output.read_text())["runs"][0]
    assert run["results"][0]["level"] == "error"
    assert run["invocations"][0]["exitCode"] == 1


def _missing_f016_entrypoints(tmp_path: Path, *extra: str) -> set[str]:
    """Run F016 with tests and return the entrypoints it expected scenarios for."""
    output = tmp_path / "report.json"
    main(
        [
            "--repo",
            str(tmp_path),
            "--rule",
            "F016",
            "--with-tests",
            "--output",
            str(output),
            *extra,
        ]
    )
    report = json.loads(output.read_text())
    missing = report["runs"][0]["properties"]["atlan/preflightTests"]["F016"]["missing"]
    return {key.split(":", 1)[0] for key in missing}


def test_behavior_entrypoints_honour_exclude(tmp_path: Path):
    """An excluded subtree's @entrypoints must not join the expected matrix.

    /remediate used to clone reference apps under ``remediation/refs/``; a
    multi-entrypoint clone there used to make a single-entrypoint app owe
    F016 scenarios for the clone's entrypoints even with
    ``--exclude remediation/``.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-connector"\nversion="1.0.0"\n'
    )
    (tmp_path / "tests").mkdir()
    ref = tmp_path / "remediation" / "refs" / "other-app" / "app"
    ref.mkdir(parents=True)
    (ref / "connector.py").write_text(
        "from application_sdk.app import App, entrypoint\n"
        "class Other(App):\n"
        "    @entrypoint\n"
        "    async def extract_metadata(self, input): pass\n"
        "    @entrypoint\n"
        "    async def extract_lineage(self, input): pass\n"
    )

    # Unexcluded, the clone's entrypoints are picked up — the precondition
    # that makes the excluded assertion below meaningful.
    assert _missing_f016_entrypoints(tmp_path) == {
        "extract_metadata",
        "extract_lineage",
    }
    assert _missing_f016_entrypoints(tmp_path, "--exclude", "remediation/") == {
        "default"
    }
