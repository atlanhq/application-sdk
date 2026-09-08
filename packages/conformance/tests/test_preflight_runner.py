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
                "P062",
                "--static",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert (
        report["runs"][0]["properties"]["atlan/preflightTests"]["P062"]["execution"]
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
                "P062",
                "--with-tests",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert report["runs"][0]["results"]
    assert not report["runs"][0]["properties"]["atlan/preflightTests"]["P062"][
        "complete"
    ]


def test_new_static_rule_reaches_sarif(tmp_path: Path):
    (tmp_path / "handler.py").write_text(
        "from application_sdk.handler import Handler\nclass H(Handler):\n    async def preflight_check(self, input):\n        return True\n"
    )
    output = tmp_path / "report.json"
    main(["--repo", str(tmp_path), "--rule", "P052", "--output", str(output)])
    assert {
        row["ruleId"] for row in json.loads(output.read_text())["runs"][0]["results"]
    } == {"P052"}


def test_unresolved_analysis_is_machine_identifiable(tmp_path):
    source = tmp_path / "handler.py"
    source.write_text(
        'from application_sdk.handler import PreflightCheck\ndef check(ok):\n return PreflightCheck(name="probe", passed=ok)\n'
    )
    output = tmp_path / "report.sarif"
    main(["--repo", str(tmp_path), "--rule", "P034,P065", "--output", str(output)])
    results = json.loads(output.read_text())["runs"][0]["results"]
    assert results[0]["ruleId"] == "P065"
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
            "P034,P065",
            "--output",
            str(tmp_path / "report.sarif"),
        ]
    )
